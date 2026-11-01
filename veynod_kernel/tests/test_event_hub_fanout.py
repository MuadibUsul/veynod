"""EventHub fan-out is per-subscriber and does not rescan the whole ring."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.models import KernelEvent
from app.services.event_hub import EventBufferOverflow, EventHub


def _event(workspace_id, seq: int) -> KernelEvent:
    return KernelEvent(
        workspace_id=workspace_id,
        event_seq=seq,
        event_type="TASK_READY",
        payload={"event_seq": seq},
    )


@pytest.mark.asyncio
async def test_two_subscribers_each_receive_the_event_once():
    hub = EventHub()
    workspace = uuid4()
    first = hub.subscribe(workspace, 0)
    second = hub.subscribe(workspace, 0)
    first_task = asyncio.create_task(anext(first))
    second_task = asyncio.create_task(anext(second))
    await asyncio.sleep(0.05)
    event = _event(workspace, 7)
    hub._fan_out(event)
    assert (await asyncio.wait_for(first_task, timeout=1)).event_seq == 7
    assert (await asyncio.wait_for(second_task, timeout=1)).event_seq == 7
    await first.aclose()
    await second.aclose()


@pytest.mark.asyncio
async def test_cursor_skips_events_already_seen():
    hub = EventHub()
    workspace = uuid4()
    stream = hub.subscribe(workspace, 10)
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.05)
    hub._fan_out(_event(workspace, 9))
    hub._fan_out(_event(workspace, 11))
    assert (await asyncio.wait_for(task, timeout=1)).event_seq == 11
    await stream.aclose()


@pytest.mark.asyncio
async def test_overflow_is_surfaced_to_the_subscriber():
    hub = EventHub()
    workspace = uuid4()
    stream = hub.subscribe(workspace, 0)
    first = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.05)
    queue = hub._subscribers[workspace][0]
    while not queue.full():
        queue.put_nowait(_event(workspace, 1))
    hub._fan_out(_event(workspace, 2))
    assert isinstance(queue.get_nowait(), EventBufferOverflow)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await stream.aclose()
