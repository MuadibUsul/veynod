"""A change in a workspace schedules that workspace at once, not at the next sweep.

After a pass, a workspace with nothing going on is next swept ten minutes
later (``_next_pass``). A task placed within seconds of being created after
that was placed by its own kick, whatever the other workspaces are doing.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from tests.conftest import run_server
from tests.test_scheduler import _host, _task, _task_row, _workspace

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
async def kicked(api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url
    async with run_server({"LEASE_REAPER_INTERVAL_SECONDS": "3600"},
                          ".server-kicks.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as http:
            yield http


async def _placed_within(client, ws: dict, key: str, host: dict, seconds: float) -> float:
    started = time.perf_counter()
    while time.perf_counter() - started < seconds:
        if (await _task_row(client, ws, key))["assignee"] == host["assignee"]:
            return time.perf_counter() - started
        await asyncio.sleep(0.1)
    raise AssertionError(f"{key} was not placed within {seconds} s")


async def test_a_new_task_is_placed_without_waiting_for_the_sweep(kicked):
    ws = await _workspace(kicked, "full")
    host = await _host(kicked, ws, f"kk-{uuid.uuid4().hex[:4]}", slots=4)
    await asyncio.sleep(1.5)  # the hello's own kick is over
    for _ in range(3):
        key = await _task(kicked, ws)
        assert await _placed_within(kicked, ws, key, host, 8) < 8


async def test_a_computer_coming_online_takes_the_waiting_task(kicked):
    ws = await _workspace(kicked, "full")
    key = await _task(kicked, ws)
    await asyncio.sleep(1.5)  # kicked once already: no computer then
    host = await _host(kicked, ws, f"kh-{uuid.uuid4().hex[:4]}")
    await _placed_within(kicked, ws, key, host, 8)


async def test_freeing_an_undelivered_placement_waits_for_a_running_pass() -> None:
    """A failed delivery and a pass of the same workspace take turns.

    A pass locks a task and then ends its placement; freeing an undelivered
    placement locked them the other way round, and the two deadlocked (one
    request came back as a 503). It now takes the workspace's lock first.
    """
    import os

    import asyncpg

    from app.database import async_session_factory
    from app.services import scheduler

    workspace_id = str(uuid.uuid4())
    dsn = os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    holder = await asyncpg.connect(dsn)
    try:
        tx = holder.transaction()
        await tx.start()
        # A pass of this workspace is running.
        await holder.execute(scheduler._WORKSPACE_LOCK_SQL.replace(":ws", "$1"), workspace_id)

        async def free() -> None:
            async with async_session_factory() as session:
                await scheduler._undeliverable(
                    session, {"placement_id": str(uuid.uuid4()), "workspace_id": workspace_id,
                              "step": "work", "task_id": None, "agent_id": "x"}, "nerve:test")

        freeing = asyncio.create_task(free())
        await asyncio.sleep(0.5)
        assert not freeing.done(), "it must wait for the pass to finish"
        await tx.rollback()
        await asyncio.wait_for(freeing, timeout=5)
    finally:
        await holder.close()
