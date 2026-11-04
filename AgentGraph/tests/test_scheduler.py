"""The scheduler puts tasks on runner hosts (full-managed Loop, runner-3)."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-horse-battery"
REPO = "MuadibUsul/veynod"


def _db() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _sql(query: str, *args):
    conn = await asyncpg.connect(_db())
    try:
        return await conn.execute(query, *args)
    finally:
        await conn.close()


async def _workspace(client, mode: str) -> dict:
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"sched-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    wid = created["workspace"]["id"]
    email = f"sched-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(f"/v1/admin/workspaces/{wid}/human-users", headers=ADMIN, json={
        "email": email, "display_name": "Sched Owner", "role": "OWNER", "password": PASSWORD})
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    owner = {"Authorization": f"Bearer {login.json()['token']}"}
    changed = await client.put("/v1/human/automation", headers=owner, json={"mode": mode})
    assert changed.status_code == 200, changed.text
    return {"id": wid, "owner": owner}


async def _host(client, ws: dict, name: str, *, repos=(REPO,), slots: int = 1,
                used: float = 10.0, version: str | None = None) -> dict:
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"role_scope": "EXECUTOR", "allowed_namespaces": ["*"]})
    assert minted.status_code == 201, minted.text
    headers = {"Authorization": f"Bearer {minted.json()['api_key']}"}
    named = await client.post("/v1/agents/register", headers=headers, json={"agent_id": name})
    assert named.status_code in (200, 201), named.text
    hello = await client.post("/v1/runner/hello", headers=headers, json={"host": {
        "os": "linux", "arch": "x64", "max_concurrency": slots, "repos": list(repos),
        "runtimes": [{"id": "claude", "available": True,
                      "usage": {"windows": [{"name": "5h", "used_percent": used}]}}],
        **({"runner_version": version} if version else {})}})
    assert hello.status_code == 200, hello.text
    return {"headers": headers, "agent_id": name, "host_id": hello.json()["host_id"],
            "assignee": f"agent:{name}"}


async def _task(client, ws: dict, *, repo: str | None = REPO) -> str:
    key = f"sched-{uuid.uuid4().hex[:8]}"
    made = await client.post("/v1/tasks", headers=ws["owner"], json={
        "task_key": key, "title": "Placed by the scheduler", "assigned_role": "EXECUTOR",
        "namespace": "default", **({"repo": repo} if repo else {})})
    assert made.status_code == 201, made.text
    return key


async def _schedule(client, ws: dict) -> dict:
    ran = await client.post("/v1/runner/schedule", headers=ws["owner"])
    assert ran.status_code == 200, ran.text
    return ran.json()


async def _task_row(client, ws: dict, key: str) -> dict:
    return (await client.get(f"/v1/tasks/{key}", headers=ws["owner"])).json()


async def _placements(client, ws: dict, key: str) -> list[dict]:
    listed = await client.get("/v1/runner/placements", headers=ws["owner"],
                              params={"task_key": key})
    assert listed.status_code == 200, listed.text
    return listed.json()["items"]


async def _claim(client, host: dict, key: str) -> dict:
    claimed = await client.post(f"/v1/tasks/{key}/claim", headers=host["headers"], json={})
    assert claimed.status_code == 200, claimed.text
    return claimed.json()


async def test_full_mode_places_on_a_fitting_host_and_pokes_it(client):
    ws = await _workspace(client, "full")
    elsewhere = await _host(client, ws, f"other-{uuid.uuid4().hex[:4]}", repos=["acme/site"])
    fits = await _host(client, ws, f"fits-{uuid.uuid4().hex[:4]}")
    key = await _task(client, ws)

    await _schedule(client, ws)
    task = await _task_row(client, ws, key)
    assert task["assignee"] == fits["assignee"], task
    placement = next(item for item in await _placements(client, ws, key)
                     if item["status"] == "offered")
    assert placement["host_id"] == fits["host_id"]
    assert placement["placement_id"].startswith("plc_")

    # Work reaches a computer only over its nerve socket (this host has none
    # connected, so nothing was delivered yet): no agent inbox carries placements.
    for host in (fits, elsewhere):
        inbox = (await client.get("/v1/inbox", headers=host["headers"])).json()["items"]
        assert not [item for item in inbox if item["kind"] == "task.placed"], inbox

    await _claim(client, fits, key)
    await _schedule(client, ws)
    assert [item["status"] for item in await _placements(client, ws, key)] == ["claimed"]


async def test_an_offline_host_loses_its_task_to_another(client):
    ws = await _workspace(client, "full")
    first = await _host(client, ws, f"aa-{uuid.uuid4().hex[:4]}", slots=3)
    second = await _host(client, ws, f"bb-{uuid.uuid4().hex[:4]}", slots=1)
    key = await _task(client, ws)
    await _schedule(client, ws)
    assert (await _task_row(client, ws, key))["assignee"] == first["assignee"]
    await _claim(client, first, key)

    await _sql("UPDATE runner_hosts SET last_seen_at = NOW() - interval '10 minutes' "
               "WHERE host_id = $1", first["host_id"])
    await _schedule(client, ws)
    task = await _task_row(client, ws, key)
    assert task["assignee"] == second["assignee"], task
    assert task["status"] == "READY"
    history = {item["host_id"]: item for item in await _placements(client, ws, key)}
    assert history[first["host_id"]]["status"] == "moved"
    assert history[first["host_id"]]["reason"] == "host_offline"
    assert history[second["host_id"]]["status"] == "offered"


async def test_a_host_out_of_quota_hands_its_task_on(client):
    ws = await _workspace(client, "full")
    busy = await _host(client, ws, f"aa-{uuid.uuid4().hex[:4]}", slots=3)
    spare = await _host(client, ws, f"bb-{uuid.uuid4().hex[:4]}", slots=1)
    key = await _task(client, ws)
    await _schedule(client, ws)
    assert (await _task_row(client, ws, key))["assignee"] == busy["assignee"]

    beat = await client.post("/v1/runner/heartbeat", headers=busy["headers"], json={
        "runtimes": [{"id": "claude", "usage": {"windows": [
            {"name": "5h", "used_percent": 99.5}]}}]})
    assert beat.status_code == 200, beat.text
    await _schedule(client, ws)
    assert (await _task_row(client, ws, key))["assignee"] == spare["assignee"]
    history = {item["host_id"]: item for item in await _placements(client, ws, key)}
    assert history[busy["host_id"]]["reason"] == "host_out_of_quota"


async def test_two_failures_change_host_and_three_rounds_stop(client):
    ws = await _workspace(client, "full")
    first = await _host(client, ws, f"aa-{uuid.uuid4().hex[:4]}", slots=3)
    second = await _host(client, ws, f"bb-{uuid.uuid4().hex[:4]}", slots=1)
    key = await _task(client, ws)
    hosts = {first["assignee"]: first, second["assignee"]: second}

    seen = []
    for _ in range(3):
        await _schedule(client, ws)
        task = await _task_row(client, ws, key)
        holder = hosts[task["assignee"]]
        seen.append(holder["agent_id"])
        claimed = await _claim(client, holder, key)
        released = await client.post(f"/v1/tasks/{key}/release", headers=holder["headers"],
                                     json={"lease_epoch": claimed["task"]["lease_epoch"],
                                           "reason": "could not finish"})
        assert released.status_code == 200, released.text
    assert seen == [first["agent_id"], first["agent_id"], second["agent_id"]]

    result = await _schedule(client, ws)
    assert result["stopped"] == 1
    task = await _task_row(client, ws, key)
    assert task["status"] == "HANDOFF"
    assert task["assignee"] is None
    assert [item["status"] for item in await _placements(client, ws, key)] == ["failed"] * 3


async def test_manual_and_semi_only_suggest(client):
    for mode in ("manual", "semi"):
        ws = await _workspace(client, mode)
        host = await _host(client, ws, f"aa-{uuid.uuid4().hex[:4]}")
        key = await _task(client, ws)
        result = await _schedule(client, ws)
        assert result["mode"] == mode
        task = await _task_row(client, ws, key)
        assert task["assignee"] is None and task["status"] == "READY"
        open_items = await _placements(client, ws, key)
        assert [(item["status"], item["host_id"]) for item in open_items] == [
            ("suggested", host["host_id"])]
        # A second pass keeps the same suggestion instead of piling up rows.
        await _schedule(client, ws)
        assert len(await _placements(client, ws, key)) == 1


async def test_no_fitting_host_says_why_in_plain_words(client):
    ws = await _workspace(client, "full")
    await _host(client, ws, f"aa-{uuid.uuid4().hex[:4]}")
    key = await _task(client, ws, repo="acme/payments")
    await _schedule(client, ws)
    task = await _task_row(client, ws, key)
    assert task["assignee"] is None and task["status"] == "READY"
    waiting = await client.get("/v1/runner/placements", headers=ws["owner"],
                               params={"open_only": "true"})
    # The supervisor notice (no supervisor computer) is covered in test_loop_steps.
    needs = [item for item in waiting.json()["needs_you"] if item["step"] != "supervise"]
    assert [(item["task_key"], item["reason"]) for item in needs] == [
        (key, "no_host_for_repo")]
    assert needs[0]["note"] == "No online runner can reach acme/payments."


async def test_a_workspace_without_runners_is_left_alone(client):
    ws = await _workspace(client, "full")
    key = await _task(client, ws)
    result = await _schedule(client, ws)
    assert result["skipped"] == "no runner hosts"
    assert await _placements(client, ws, key) == []


async def test_the_reaper_places_tasks_without_being_asked(client):
    import asyncio

    ws = await _workspace(client, "full")
    host = await _host(client, ws, f"aa-{uuid.uuid4().hex[:4]}")
    key = await _task(client, ws)
    for _ in range(40):
        if (await _task_row(client, ws, key))["assignee"] == host["assignee"]:
            break
        await asyncio.sleep(0.5)
    assert (await _task_row(client, ws, key))["assignee"] == host["assignee"]


async def test_a_paused_host_gives_its_task_away_and_can_resume(client):
    ws = await _workspace(client, "full")
    first = await _host(client, ws, f"aa-{uuid.uuid4().hex[:4]}", slots=3)
    second = await _host(client, ws, f"bb-{uuid.uuid4().hex[:4]}", slots=1)
    key = await _task(client, ws)
    await _schedule(client, ws)
    assert (await _task_row(client, ws, key))["assignee"] == first["assignee"]

    refused = await client.post(f"/v1/runner/hosts/{first['host_id']}/pause",
                                headers=first["headers"])
    assert refused.status_code == 403, refused.text
    paused = await client.post(f"/v1/runner/hosts/{first['host_id']}/pause",
                               headers=ws["owner"])
    assert paused.status_code == 200, paused.text
    assert paused.json()["state"] == "paused"
    # Heartbeats and a fresh hello keep it paused.
    beat = await client.post("/v1/runner/heartbeat", headers=first["headers"], json={})
    assert beat.json()["status"] == "paused"

    await _schedule(client, ws)
    assert (await _task_row(client, ws, key))["assignee"] == second["assignee"]
    history = {item["host_id"]: item for item in await _placements(client, ws, key)}
    assert history[first["host_id"]]["reason"] == "host_paused"

    resumed = await client.post(f"/v1/runner/hosts/host:{first['host_id']}/resume",
                                headers=ws["owner"])
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "online"


async def test_a_cli_older_than_nerve_dispatch_is_named_and_never_offered_work(client):
    """0.5.x says hello and heartbeats but only listens on SSE: offering it work
    would time out again and again. It is left out and the doctor says why."""
    ws = await _workspace(client, "full")
    old = await _host(client, ws, f"old-{uuid.uuid4().hex[:4]}", version="0.5.2")
    key = await _task(client, ws)
    result = await _schedule(client, ws)
    assert result["placed"] == 0, result
    assert (await _task_row(client, ws, key))["assignee"] is None
    doctor = (await client.get("/v1/runner/doctor", headers=ws["owner"])).json()
    check = {item["id"]: item for item in doctor["checks"]}["cli_version"]
    assert check["ok"] is False and old["agent_id"] in check["text"]
    assert "npm install -g veynod@latest" in check["fix"]
    card = (await client.get("/v1/runner/hosts", headers=ws["owner"])).json()["computers"]
    assert card[0]["outdated"] is True

    # The upgraded computer says hello again and gets the task.
    new = await _host(client, ws, f"new-{uuid.uuid4().hex[:4]}", version="0.6.0")
    await _schedule(client, ws)
    assert (await _task_row(client, ws, key))["assignee"] == new["assignee"]


async def test_concurrent_passes_on_one_workspace_do_not_collide(client, monkeypatch):
    """A pass by hand (/v1/runner/schedule) and the background pass used to run
    on the same workspace at once: the later one ended a placement from its
    stale read and broke uq_runner_placements_open (503 database_unavailable).
    Passes on one workspace now take turns."""
    import asyncio

    from app.database import async_session_factory
    from app.services import scheduler

    ws = await _workspace(client, "full")
    first = await _host(client, ws, f"aa-{uuid.uuid4().hex[:4]}", slots=3)
    await _host(client, ws, f"bb-{uuid.uuid4().hex[:4]}", slots=1)
    key = await _task(client, ws)
    await _schedule(client, ws)
    claimed = await _claim(client, first, key)
    released = await client.post(f"/v1/tasks/{key}/release", headers=first["headers"],
                                 json={"lease_epoch": claimed["task"]["lease_epoch"],
                                       "reason": "could not finish"})
    assert released.status_code == 200, released.text

    # The slow pass reads the open placements, then stalls before acting on them.
    stalled = asyncio.Event()
    resume = asyncio.Event()
    real_chunk_states = scheduler._chunk_states

    async def chunk_states(session, workspace_id):
        if asyncio.current_task().get_name() == "slow-pass":
            stalled.set()
            await resume.wait()
        return await real_chunk_states(session, workspace_id)

    monkeypatch.setattr(scheduler, "_chunk_states", chunk_states)

    async def one_pass():
        async with async_session_factory() as session:
            return await scheduler.run_pass(session, ws["id"])

    slow = asyncio.create_task(one_pass(), name="slow-pass")
    await asyncio.wait_for(stalled.wait(), timeout=10)
    fast = asyncio.create_task(one_pass(), name="fast-pass")
    # Without turns the fast pass finishes here; with them it waits for the slow one.
    await asyncio.wait([fast], timeout=2)
    resume.set()
    results = await asyncio.gather(slow, fast, return_exceptions=True)
    assert not [r for r in results if isinstance(r, BaseException)], results

    open_rows = [item for item in await _placements(client, ws, key)
                 if item["status"] in ("suggested", "offered", "claimed", "unplaced")]
    assert len(open_rows) <= 1
