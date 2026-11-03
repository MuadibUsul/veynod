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
                used: float = 10.0) -> dict:
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
                      "usage": {"windows": [{"name": "5h", "used_percent": used}]}}]}})
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

    inbox = (await client.get("/v1/inbox", headers=fits["headers"])).json()["items"]
    placed = [item for item in inbox if item["kind"] == "task.placed"]
    assert placed and key in (placed[0]["task_key"], placed[0]["doc_key"])
    assert placement["placement_id"] in placed[0]["note"]
    other_inbox = (await client.get("/v1/inbox", headers=elsewhere["headers"])).json()["items"]
    assert not [item for item in other_inbox if item["kind"] == "task.placed"]

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


async def test_a_chunk_sent_back_by_its_gate_is_offered_again(client):
    """A failed gate leaves the chunk in `rework` and its task in HANDOFF — the
    work pass only offers READY tasks, so the scheduler has to reopen it, or the
    chunk waits for a person who was never asked."""
    ws = await _workspace(client, "full")
    host = await _host(client, ws, f"aa-{uuid.uuid4().hex[:4]}")
    architect = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"role_scope": "ANALYST", "allowed_namespaces": ["*"]})
    analyst = {"Authorization": f"Bearer {architect.json()['api_key']}"}
    diagram_key = f"rw-{uuid.uuid4().hex[:6]}"
    made = await client.post("/v1/diagrams", headers=analyst, json={
        "diagram_key": diagram_key, "title": "x", "chunks": [{
            "chunk_key": "a", "title": "a", "scope": "build a", "layer": "interface",
            "icd": {"exports": ["a()"]}, "done_criteria": ["pytest tests/test_a.py"],
            "allowed_write_paths": ["src/a.py"]}]})
    assert made.status_code == 201, made.text
    confirmed = await client.post(f"/v1/diagrams/{diagram_key}/confirm", headers=ws["owner"])
    assert confirmed.status_code == 200, confirmed.text
    frozen = await client.post(f"/v1/diagrams/{diagram_key}/freeze", headers=ws["owner"], json={})
    assert frozen.status_code == 200, frozen.text
    # Full automation dispatches the first chunks with the freeze itself.
    chunk_task = next(item["task_key"] for item in frozen.json()["chunks"]
                      if item["chunk_key"] == "a")
    assert chunk_task

    # The gate fails the chunk: state rework, task HANDOFF — nothing holding it.
    # Three failed attempts from before the gate must not count against the new
    # round; only failures after the gate do.
    await _sql("UPDATE diagram_chunks SET state = 'rework', gate = "
               "'{\"result\": \"fail\", \"at\": \"2999-01-01T00:00:00+00:00\"}'::jsonb "
               "WHERE task_key = $1", chunk_task)
    await _sql("UPDATE collab_tasks SET status = 'HANDOFF', assignee = NULL WHERE task_key = $1",
               chunk_task)
    for index in range(3):
        await _sql(
            "INSERT INTO runner_placements (workspace_id, task_id, task_key, host_id, agent_id, "
            "step, mode, status, reason, created_at) SELECT workspace_id, id, task_key, $2, $3, "
            "'work', 'assign', 'failed', 'reported_failed', '2000-01-01'::timestamptz + $4 * "
            "interval '1 minute' FROM collab_tasks WHERE task_key = $1",
            chunk_task, host["host_id"], host["agent_id"], index)

    result = await _schedule(client, ws)
    assert result["reopened"] == 1, result
    task = await _task_row(client, ws, chunk_task)
    assert task["status"] in ("READY", "IN_PROGRESS"), task
    assert task["assignee"] == host["assignee"], (
        {k: result.get(k) for k in ("mode", "placed", "unplaced", "suggested", "reopened", "stopped")},
        [item["reason"] for item in await _placements(client, ws, chunk_task)])
    offered = [item for item in await _placements(client, ws, chunk_task)
               if item["status"] == "offered"]
    assert offered and offered[0]["host_id"] == host["host_id"]

    # The task is held again: the next pass must not open a second placement.
    again = await _schedule(client, ws)
    assert again["reopened"] == 0, again


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
