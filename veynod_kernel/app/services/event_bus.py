"""Cursor-based event stream (SSE) with PostgreSQL LISTEN/NOTIFY hints.

Events are persisted in ``kernel_events`` with a gapless, workspace-scoped
``event_seq``. The SSE endpoint tails by cursor; ``pg_notify`` merely reduces
latency for in-process/other-process listeners and is not required for
correctness.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KernelEvent

NOTIFY_CHANNEL = "veynod_kernel_events"


def _json_default(value: Any) -> str:
    if isinstance(value, (UUID, datetime)):
        return str(value)
    raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")


def encode_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=_json_default, separators=(",", ":"))


class EventBus:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def publish(
        self,
        workspace_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        *,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        producer: str = "kernel",
    ) -> int:
        """Insert an event. The caller owns the transaction boundary."""
        event = KernelEvent(
            workspace_id=workspace_id,
            event_type=event_type,
            payload=payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
            producer=producer,
        )
        self.session.add(event)
        await self.session.flush()
        event_seq = int(event.event_seq)

        # Transactional outbox: the durable kernel_events row and the
        # notification intent commit (or roll back) together.  A dispatcher
        # performs pg_notify after commit; SSE remains DB-cursor based, so a
        # missed notification only delays, never loses, an event.
        await self.session.execute(
            text(
                """
                INSERT INTO event_outbox (
                    event_seq, workspace_id, event_type, payload
                ) VALUES (
                    :event_seq, :workspace_id, :event_type,
                    CAST(:payload AS JSONB)
                )
                """
            ),
            {
                "event_seq": event_seq,
                "workspace_id": workspace_id,
                "event_type": event_type,
                "payload": encode_payload(payload),
            },
        )
        return event_seq

    async def latest_seq(self, workspace_id: UUID) -> int:
        value = await self.session.scalar(
            select(KernelEvent.event_seq)
            .where(KernelEvent.workspace_id == workspace_id)
            .order_by(KernelEvent.event_seq.desc())
            .limit(1)
        )
        return int(value or 0)

    async def fetch_after(
        self, workspace_id: UUID, after_seq: int, limit: int = 200
    ) -> list[KernelEvent]:
        rows = await self.session.execute(
            select(KernelEvent)
            .where(
                KernelEvent.workspace_id == workspace_id,
                KernelEvent.event_seq > after_seq,
            )
            .order_by(KernelEvent.event_seq.asc())
            .limit(limit)
        )
        return list(rows.scalars().all())

    async def fetch_global_after(
        self, after_seq: int, limit: int = 5000
    ) -> list[KernelEvent]:
        """One query for every workspace, used by the in-process SSE hub."""
        rows = await self.session.execute(
            select(KernelEvent)
            .where(KernelEvent.event_seq > after_seq)
            .order_by(KernelEvent.event_seq.asc())
            .limit(limit)
        )
        return list(rows.scalars().all())


def sse_frame(event: KernelEvent) -> str:
    data = {
        "event_seq": event.event_seq,
        "event_id": str(event.event_id) if event.event_id else None,
        "event_version": event.event_version,
        "producer": event.producer,
        "correlation_id": event.correlation_id,
        "causation_id": event.causation_id,
        "workspace_id": str(event.workspace_id),
        "created_at": event.created_at.astimezone(timezone.utc).isoformat(),
        **event.payload,
    }
    return (
        f"id: {event.event_seq}\n"
        f"event: {event.event_type}\n"
        f"data: {encode_payload(data)}\n\n"
    )


__all__ = ["EventBus", "NOTIFY_CHANNEL", "encode_payload", "sse_frame"]
