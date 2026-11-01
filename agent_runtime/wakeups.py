"""Kernel -> Runtime wakeups (durable event delivery).

The Agent Runtime never polls a submission status as its primary sync
mechanism.  Instead this watcher reads the Kernel's durable event feed with a
persisted cursor and writes idempotent wakeup rows; the worker applies them
before claiming the next Run.  A periodic sweep is kept only as a safety net
for missed notifications, not as the delivery path.

Event mapping:

* ``SUBMISSION_COMMITTED`` / ``SUBMISSION_FAILED`` -> ``SUBMISSION_FINAL`` keyed
  by ``submission_id``; the run parked on that submission resumes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .store import RunStore

logger = logging.getLogger("agent_runtime.wakeups")

SUBMISSION_EVENTS = frozenset({"SUBMISSION_COMMITTED", "SUBMISSION_FAILED"})


@dataclass
class KernelEventSource:
    base_url: str
    service_key: str
    interval_seconds: float = 2.0
    limit: int = 200


class KernelEventWatcher:
    def __init__(
        self,
        store: RunStore,
        source: KernelEventSource,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store
        self.source = source
        self._client = httpx.AsyncClient(
            timeout=30.0, transport=transport
        )
        self._stopping = False
        self.stats: dict[str, int] = {
            "polls": 0,
            "events": 0,
            "wakeups": 0,
            "errors": 0,
        }

    async def close(self) -> None:
        await self._client.aclose()

    def stop(self) -> None:
        self._stopping = True

    async def run_forever(self) -> None:
        while not self._stopping:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the watcher must survive
                self.stats["errors"] += 1
                logger.exception("kernel event watcher poll failed")
            await asyncio.sleep(max(0.5, self.source.interval_seconds))

    async def poll_once(self) -> dict[str, int]:
        """Read every live workspace's events after its durable cursor."""
        workspaces = await self.store.list_kernel_workspaces()
        for workspace_id in workspaces:
            cursor_key = f"kernel:{self.source.base_url}:{workspace_id}"
            cursor = await self.store.wakeup_cursor(cursor_key)
            payload = await self._fetch(workspace_id, cursor)
            events = payload.get("events") or []
            self.stats["polls"] += 1
            self.stats["events"] += len(events)
            for event in events:
                await self._ingest(workspace_id, event)
            next_seq = int(payload.get("next_after_seq") or cursor)
            if next_seq > cursor:
                await self.store.set_wakeup_cursor(cursor_key, next_seq)
        return dict(self.stats)

    async def _fetch(self, workspace_id: str, cursor: int) -> dict[str, Any]:
        response = await self._client.get(
            self.source.base_url.rstrip("/") + "/v1/runtime/events",
            params={
                "workspace_id": workspace_id,
                "after_seq": cursor,
                "limit": self.source.limit,
            },
            headers={
                "X-Veynod-Service-Key": self.source.service_key,
                "X-Workspace-Id": workspace_id,
            },
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"kernel events HTTP {response.status_code}: {response.text[:300]}"
            )
        return response.json()

    async def _ingest(self, workspace_id: str, event: dict[str, Any]) -> None:
        event_type = str(event.get("event_type") or "")
        if event_type not in SUBMISSION_EVENTS:
            return
        body = event.get("payload") or {}
        submission_id = str(body.get("submission_id") or "")
        if not submission_id:
            return
        inserted = await self.store.add_wakeup(
            kind="SUBMISSION_FINAL",
            ref_id=submission_id,
            kernel_event_seq=int(event.get("event_seq") or 0),
            payload={**body, "workspace_id": workspace_id},
        )
        if inserted:
            self.stats["wakeups"] += 1


__all__ = ["KernelEventSource", "KernelEventWatcher", "SUBMISSION_EVENTS"]
