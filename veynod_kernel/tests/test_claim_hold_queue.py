"""Claim hold queue: zero-DB waiters and wake semantics."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.engine.hold_queue import ClaimHoldQueue


@pytest.mark.asyncio
async def test_waiter_is_woken_by_signal() -> None:
    queue = ClaimHoldQueue()
    workspace = uuid4()
    task = asyncio.create_task(queue.wait_for_opportunity(workspace, 2.0))
    await asyncio.sleep(0.05)
    await queue.notify_available(workspace, n=1)
    assert await asyncio.wait_for(task, timeout=1.0) is True


@pytest.mark.asyncio
async def test_waiter_times_out_without_holding_resources() -> None:
    queue = ClaimHoldQueue()
    workspace = uuid4()
    assert await queue.wait_for_opportunity(workspace, 0.1) is False
    assert queue._waiter_counts[workspace] == 0
