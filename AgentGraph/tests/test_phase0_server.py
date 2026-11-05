"""Phase 0 server fixes for veynod up + hub (board task up-hub-p0-server).

Truthful wakeability, wake webhooks a bare heartbeat no longer holds back, busy
hosts that are not penalised, one scheduler at a time in its own loop, and no
stale deploy rule in the automation guidance.
"""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest

from tests.test_poke_realtime import _agent, _Sink
from tests.test_scheduler import (
    _db,
    _host,
    _placements,
    _schedule,
    _sql,
    _task,
    _workspace,
)

pytestmark = pytest.mark.integration


async def _presence(client, owner: dict, agent_id: str) -> dict:
    items = (await client.get("/v1/presence", headers=owner)).json()["items"]
    return next(item for item in items if item["agent_id"] == agent_id)


async def test_an_agent_seen_only_through_calls_is_not_wakeable(client, admin_headers, workspace):
    owner = workspace["headers"]
    agent, _, agent_id = await _agent(client, admin_headers, workspace)
    # Make the next call count as presence (the directory throttles it).
    await _sql("UPDATE agent_directory SET last_seen_at = NULL WHERE agent_id = $1", agent_id)
    assert (await client.get("/v1/tasks", headers=agent)).status_code == 200

    seen = await _presence(client, owner, agent_id)
    assert seen["state"] == "online" and seen["transport"] == "api", seen
    assert seen["wakeable"] is False
    assert seen["active_via_api"] is True

    beat = await client.post("/v1/presence/heartbeat", headers=agent)
    assert beat.status_code == 200, beat.text
    seen = await _presence(client, owner, agent_id)
    assert seen["wakeable"] is True and seen["active_via_api"] is False
    coverage = (await client.get("/v1/presence", headers=owner)).json()["wake_coverage"]
    assert coverage["wakeable"] >= 1 and "wakeable_rate" in coverage


async def test_a_heartbeat_without_a_stream_does_not_hold_back_the_wake(
        api_base_url, admin_headers, workspace):
    import httpx

    sink = _Sink(200)
    try:
        async with httpx.AsyncClient(base_url=api_base_url, timeout=30) as live:
            owner = workspace["headers"]
            agent, _, agent_id = await _agent(live, admin_headers, workspace)
            wake = await live.put("/v1/agents/me/wake", headers=agent, json={"url": sink.url})
            assert wake.status_code == 200, wake.text
            # A loop heartbeat, no inbox stream: nothing can push to this agent.
            assert (await live.post("/v1/presence/heartbeat", headers=agent)).status_code == 200
            made = await live.post("/v1/tasks", headers=owner, json={
                "title": "wake despite a heartbeat", "assignee": f"agent:{agent_id}",
                "namespace": "default"})
            assert made.status_code == 201, made.text
            for _ in range(40):
                if sink.hits:
                    break
                await asyncio.sleep(0.5)
            assert sink.hits, "the wake webhook was held back by a bare heartbeat"
            assert sink.hits[0]["body"]["task_key"] == made.json()["task_key"]
    finally:
        sink.close()


async def test_a_full_slot_moves_the_task_without_counting_against_the_host(client):
    ws = await _workspace(client, "full")
    first = await _host(client, ws, f"busy-{uuid.uuid4().hex[:4]}", slots=2)
    key = await _task(client, ws)
    await _schedule(client, ws)
    offered = next(item for item in await _placements(client, ws, key)
                   if item["status"] == "offered")
    host = first if offered["host_id"] == first["host_id"] else None
    assert host is not None, offered
    reported = await client.post(f"/v1/runner/placements/{offered['placement_id']}/report",
                                 headers=host["headers"],
                                 json={"outcome": "failed", "note": "no_free_slot"})
    assert reported.status_code == 200, reported.text
    await _schedule(client, ws)
    by_id = {item["placement_id"]: item for item in await _placements(client, ws, key)}
    assert (by_id[offered["placement_id"]]["status"], by_id[offered["placement_id"]]["reason"]) == (
        "moved", "host_busy")

    # Any other failed start counts against the host.
    second = await _task(client, ws)
    await _schedule(client, ws)
    placed = next(item for item in await _placements(client, ws, second)
                  if item["status"] == "offered")
    await client.post(f"/v1/runner/placements/{placed['placement_id']}/report",
                      headers=host["headers"], json={"outcome": "failed", "note": "exited 1"})
    await _schedule(client, ws)
    by_id = {item["placement_id"]: item for item in await _placements(client, ws, second)}
    assert (by_id[placed["placement_id"]]["status"], by_id[placed["placement_id"]]["reason"]) == (
        "failed", "reported_failed")


async def test_one_scheduling_pass_at_a_time():
    from app.services import scheduler

    holder = await asyncpg.connect(_db())
    try:
        # Wait until the server's own loop is not holding the lock, then take it.
        for _ in range(50):
            if await holder.fetchval(
                    "SELECT pg_try_advisory_lock(hashtext('veynod_scheduler')::bigint)"):
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("the scheduler lock never came free")
        assert await scheduler.run_all_locked() is None
        await holder.execute("SELECT pg_advisory_unlock(hashtext('veynod_scheduler')::bigint)")
        ran = None
        for _ in range(50):
            ran = await scheduler.run_all_locked()
            if ran is not None:
                break
            await asyncio.sleep(0.1)
        assert isinstance(ran, int)
    finally:
        await holder.close()


def test_scheduling_is_not_part_of_the_reaper():
    import inspect

    from app.services import reaper

    assert "schedule_runners" not in inspect.getsource(reaper)
    assert "scheduler_loop()" in inspect.getsource(__import__("app.main", fromlist=["main"]))


def test_no_mode_holds_done_for_a_deploy():
    """The v1 deploy gate is gone: a task is done when its work is approved."""
    from app.services import automation

    for mode in ("manual", "semi", "full"):
        assert "done_needs_deploy" not in automation.agent_rules(mode)
