"""Kernel -> Runtime wakeups (durable event delivery).

The Agent Runtime never polls a submission status as its primary sync
mechanism.  Instead this watcher reads each Kernel instance's durable event
feed with a persisted cursor and writes idempotent wakeup rows; the worker
applies them before claiming the next Run.

Multi-instance routing: sources are discovered from the live Runs' Kernel
bindings (`base_url` + `workspace_id`), and each instance has its own cursor
and its own service credential.  A process-wide default is only a fallback for
a single-instance deployment -- it can never silently redirect another
instance's events.

Event mapping:

* ``SUBMISSION_COMMITTED`` / ``SUBMISSION_FAILED`` -> ``SUBMISSION_FINAL`` keyed
  by ``submission_id``; the run parked on that submission resumes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .store import RunStore

logger = logging.getLogger("agent_runtime.wakeups")

SUBMISSION_EVENTS = frozenset({"SUBMISSION_COMMITTED", "SUBMISSION_FAILED"})


@dataclass
class KernelEventSource:
    """Default Kernel instance (fallback when a Run carries no binding)."""

    base_url: str
    service_key: str
    interval_seconds: float = 2.0
    limit: int = 200


def parse_service_keys(raw: str | None) -> dict[str, str]:
    """``RUNTIME_KERNEL_SERVICE_KEYS``: {"https://kernel-b:8001": "key-b"}."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.error("RUNTIME_KERNEL_SERVICE_KEYS is not valid JSON; ignoring")
        return {}
    if not isinstance(parsed, dict):
        logger.error("RUNTIME_KERNEL_SERVICE_KEYS must be an object")
        return {}
    return {
        str(base).rstrip("/"): str(key)
        for base, key in parsed.items()
        if base and key
    }


class KernelEventWatcher:
    def __init__(
        self,
        store: RunStore,
        source: KernelEventSource | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        service_keys: dict[str, str] | None = None,
        discover: bool = True,
    ) -> None:
        self.store = store
        self.source = source
        self.service_keys = {
            str(base).rstrip("/"): key
            for base, key in (service_keys or {}).items()
        }
        self.discover = discover
        self._client = httpx.AsyncClient(timeout=30.0, transport=transport)
        self._stopping = False
        self.stats: dict[str, int] = {
            "polls": 0,
            "sources": 0,
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
            interval = (
                self.source.interval_seconds if self.source is not None else 2.0
            )
            await asyncio.sleep(max(0.5, interval))

    # -------------------------------------------------------------- discovery
    async def _sources(self) -> list[tuple[str, str]]:
        """(base_url, workspace_id) pairs to poll, from Run bindings."""
        pairs: set[tuple[str, str]] = set()
        if self.discover:
            for row in await self.store.list_kernel_sources():
                pairs.add((row["base_url"], row["workspace_id"]))
        if self.source is not None and self.source.base_url:
            # Single-instance fallback: runs created before bindings were
            # stored, or a deployment that pins one Kernel.
            base = self.source.base_url.rstrip("/")
            for workspace_id in await self.store.list_kernel_workspaces():
                pairs.add((base, workspace_id))
        return sorted(pairs)

    def _service_key(self, base_url: str) -> str | None:
        base = base_url.rstrip("/")
        if base in self.service_keys:
            return self.service_keys[base]
        if self.source is not None and base == self.source.base_url.rstrip("/"):
            return self.source.service_key
        return None

    # ----------------------------------------------------------------- poll
    async def poll_once(self) -> dict[str, int]:
        """One pass over every discovered instance; counters are per pass."""
        self.stats = {key: 0 for key in self.stats}
        sources = await self._sources()
        self.stats["sources"] = len(sources)
        for base_url, workspace_id in sources:
            key = self._service_key(base_url)
            if not key:
                # Never post another instance's events with the wrong
                # credential: skip loudly instead.
                logger.warning(
                    "no service key for Kernel %s; skipping workspace %s",
                    base_url,
                    workspace_id,
                )
                continue
            cursor_key = f"kernel:{base_url}:{workspace_id}"
            cursor = await self.store.wakeup_cursor(cursor_key)
            try:
                payload = await self._fetch(base_url, key, workspace_id, cursor)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one instance must not stop others
                self.stats["errors"] += 1
                logger.exception(
                    "kernel event poll failed for %s workspace %s",
                    base_url,
                    workspace_id,
                )
                continue
            events = payload.get("events") or []
            self.stats["polls"] += 1
            self.stats["events"] += len(events)
            for event in events:
                await self._ingest(base_url, workspace_id, event)
            next_seq = int(payload.get("next_after_seq") or cursor)
            if next_seq > cursor:
                await self.store.set_wakeup_cursor(cursor_key, next_seq)
        return dict(self.stats)

    async def _fetch(
        self, base_url: str, service_key: str, workspace_id: str, cursor: int
    ) -> dict[str, Any]:
        response = await self._client.get(
            base_url.rstrip("/") + "/v1/runtime/events",
            params={
                "workspace_id": workspace_id,
                "after_seq": cursor,
                "limit": self.source.limit if self.source is not None else 200,
            },
            headers={
                "X-Veynod-Service-Key": service_key,
                "X-Workspace-Id": workspace_id,
            },
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"kernel events HTTP {response.status_code}: {response.text[:300]}"
            )
        return response.json()

    async def _ingest(
        self, base_url: str, workspace_id: str, event: dict[str, Any]
    ) -> None:
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
            payload={
                **body,
                "workspace_id": workspace_id,
                "kernel_base_url": base_url,
            },
        )
        if inserted:
            self.stats["wakeups"] += 1


__all__ = [
    "KernelEventSource",
    "KernelEventWatcher",
    "SUBMISSION_EVENTS",
    "parse_service_keys",
]
