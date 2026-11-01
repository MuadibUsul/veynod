"""Transactional outbox dispatcher for kernel event notifications.

``kernel_events`` is the durable SSE log.  ``event_outbox`` is written in the
same transaction as the business mutation; this dispatcher performs the
post-commit ``pg_notify`` and can be restarted without losing events.  SSE
clients read PostgreSQL cursors, so notification failure affects latency only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from sqlalchemy import text

from app.config import settings
from app.database import session_scope
from app.services.event_bus import NOTIFY_CHANNEL, encode_payload

logger = logging.getLogger(__name__)


class OutboxDispatcher:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="veynod-event-outbox")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping = True
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Event outbox dispatch failed")
            await asyncio.sleep(settings.outbox_poll_seconds)

    async def _dispatch_once(self) -> None:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT outbox_id, event_seq, workspace_id, event_type, payload
                        FROM event_outbox
                        WHERE published_at IS NULL
                          AND dead_lettered_at IS NULL
                          AND available_at <= NOW()
                        ORDER BY outbox_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 100
                        """
                    )
                )
            ).mappings().all()
            if not rows:
                await session.commit()
                return
            for row in rows:
                payload = row["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                encoded = encode_payload(
                    {
                        "workspace_id": str(row["workspace_id"]),
                        "event_seq": row["event_seq"],
                        "payload": payload,
                    }
                )
                # PostgreSQL NOTIFY payloads are capped at 8000 bytes.  The
                # durable cursor log is authoritative, so an oversized hint is
                # recorded as skipped instead of blocking the queue forever.
                oversized = len(encoded.encode("utf-8")) > 7900
                if not oversized:
                    await session.execute(
                        text("SELECT pg_notify(:channel, :payload)"),
                        {"channel": NOTIFY_CHANNEL, "payload": encoded},
                    )
                await session.execute(
                    text(
                        """
                        UPDATE event_outbox
                        SET published_at = NOW(), attempts = attempts + 1,
                            last_error = CASE
                                WHEN :oversized
                                THEN 'notification payload exceeded 7900 bytes'
                                ELSE NULL END
                        WHERE outbox_id = :outbox_id
                        """
                    ),
                    {"outbox_id": row["outbox_id"], "oversized": oversized},
                )
            await session.commit()


outbox_dispatcher = OutboxDispatcher()

__all__ = ["OutboxDispatcher", "outbox_dispatcher"]
