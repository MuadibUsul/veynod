"""Single-poller SSE fan-out with per-subscriber queues.

Without this hub every SSE client polls kernel_events once per second. At 1,000
runners that is 1,000 queries/s and a connection-pool stampede as soon as a
notify wakes every coroutine. The hub owns exactly one poller per process,
keeps bounded per-workspace ring buffers, and fans out each new event once into
bounded per-subscriber queues (instead of every subscriber rescanning the whole
ring on every notification).

A slow subscriber whose queue fills is cut off with EventBufferOverflow; the
SSE route catches up from PostgreSQL and reconnects its cursor.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from uuid import UUID

from sqlalchemy import func, select

from app.config import settings
from app.database import session_scope
from app.models import KernelEvent
from app.engine.hold_queue import GLOBAL_CLAIM_HOLD_QUEUE
from app.services.event_bus import EventBus

logger = logging.getLogger(__name__)

_RING_SIZE = 10_000
_POLL_LIMIT = 5_000
_HEARTBEAT_SECONDS = 15.0
_SUBSCRIBER_QUEUE = 2_000


class EventBufferOverflow(Exception):
    """The subscriber's cursor fell out of the in-memory ring buffer."""


class EventHub:
    def __init__(self) -> None:
        self._buffers: dict[UUID, deque[KernelEvent]] = {}
        self._latest: dict[UUID, int] = {}
        self._subscribers: dict[UUID, dict[int, asyncio.Queue]] = {}
        self._next_subscriber = 0
        self._cursor = 0
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        self._buffers.clear()
        self._latest.clear()
        async with session_scope() as session:
            self._cursor = int(
                await session.scalar(
                    select(func.coalesce(func.max(KernelEvent.event_seq), 0))
                )
                or 0
            )
            rows = (
                await session.execute(
                    select(
                        KernelEvent.workspace_id,
                        func.max(KernelEvent.event_seq),
                    ).group_by(KernelEvent.workspace_id)
                )
            ).all()
            self._latest = {
                workspace_id: int(event_seq) for workspace_id, event_seq in rows
            }
        self._stopping = False
        self._task = asyncio.create_task(self._poll_loop(), name="veynod-event-hub")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        async with self._lock:
            for queues in self._subscribers.values():
                for queue in queues.values():
                    self._overflow(queue)

    async def _poll_loop(self) -> None:
        while not self._stopping:
            events: list[KernelEvent] = []
            try:
                async with session_scope() as session:
                    events = await EventBus(session).fetch_global_after(
                        self._cursor, _POLL_LIMIT
                    )
                if events:
                    wake_workspaces: dict[UUID, int] = {}
                    async with self._lock:
                        for event in events:
                            buffer = self._buffers.setdefault(
                                event.workspace_id, deque(maxlen=_RING_SIZE)
                            )
                            buffer.append(event)
                            self._cursor = max(self._cursor, event.event_seq)
                            self._latest[event.workspace_id] = max(
                                self._latest.get(event.workspace_id, 0),
                                event.event_seq,
                            )
                            self._fan_out(event)
                            if event.event_type in {
                                "TOKEN_REVOKED",
                                "TOKEN_ROTATED",
                            }:
                                from app.auth import clear_token_cache

                                clear_token_cache()
                            if GLOBAL_CLAIM_HOLD_QUEUE.event_wakes(
                                event.event_type
                            ):
                                wake_workspaces[event.workspace_id] = (
                                    wake_workspaces.get(event.workspace_id, 0) + 1
                                )
                    for workspace_id, count in wake_workspaces.items():
                        await GLOBAL_CLAIM_HOLD_QUEUE.notify_available(
                            workspace_id, min(32, max(2, count * 2))
                        )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the hub alive
                logger.exception("Event hub poll failed")

            # Drain a burst quickly; otherwise stay at one DB query per interval.
            await asyncio.sleep(0.05 if events else settings.sse_poll_seconds)

    def _fan_out(self, event: KernelEvent) -> None:
        for queue in self._subscribers.get(event.workspace_id, {}).values():
            if queue.full():
                self._overflow(queue)
                continue
            queue.put_nowait(event)

    @staticmethod
    def _overflow(queue: asyncio.Queue) -> None:
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            queue.put_nowait(EventBufferOverflow())
        except asyncio.QueueFull:
            pass

    def latest_seq(self, workspace_id: UUID) -> int:
        return self._latest.get(workspace_id, 0)

    def earliest_seq(self, workspace_id: UUID) -> int | None:
        buffer = self._buffers.get(workspace_id)
        return buffer[0].event_seq if buffer else None

    async def catch_up(
        self, workspace_id: UUID, cursor: int, limit: int = 1_000
    ) -> list[KernelEvent]:
        async with session_scope() as session:
            return await EventBus(session).fetch_after(workspace_id, cursor, limit)

    async def subscribe(self, workspace_id: UUID, cursor: int):
        """Yield events after cursor; yield None every 15s as a heartbeat."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE)
        subscriber_id = self._next_subscriber
        self._next_subscriber += 1
        async with self._lock:
            buffer = self._buffers.get(workspace_id)
            if (
                buffer
                and len(buffer) >= _RING_SIZE
                and cursor < buffer[0].event_seq - 1
            ):
                raise EventBufferOverflow
            if buffer:
                for event in buffer:
                    if event.event_seq > cursor:
                        if queue.full():
                            raise EventBufferOverflow
                        queue.put_nowait(event)
            self._subscribers.setdefault(workspace_id, {})[subscriber_id] = queue
        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=_HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield None
                    continue
                if isinstance(item, EventBufferOverflow):
                    raise item
                if item.event_seq <= cursor:
                    continue
                cursor = item.event_seq
                yield item
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(workspace_id)
                if subscribers:
                    subscribers.pop(subscriber_id, None)
                    if not subscribers:
                        self._subscribers.pop(workspace_id, None)


_hub = EventHub()


def get_event_hub() -> EventHub:
    return _hub


__all__ = ["EventBufferOverflow", "EventHub", "get_event_hub"]
