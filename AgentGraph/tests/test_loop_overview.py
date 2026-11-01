"""The Loop page in one read (v3.24): lanes as planned, one state per piece."""

from __future__ import annotations

import uuid

import pytest

from app.services import flow

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-horse-battery"


async def _workspace(client) -> dict:
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"flow-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    wid = created["workspace"]["id"]
    email = f"flow-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(f"/v1/admin/workspaces/{wid}/human-users", headers=ADMIN, json={
        "email": email, "display_name": "Flow Owner", "role": "OWNER", "password": PASSWORD})
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"id": wid, "human": {"Authorization": f"Bearer {login.json()['token']}"}}


async def _agent(client, ws: dict, name: str, role: str = "EXECUTOR") -> dict:
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"role_scope": role, "allowed_namespaces": ["*"]})
    assert minted.status_code == 201, minted.text
    headers = {"Authorization": f"Bearer {minted.json()['api_key']}"}
    named = await client.post("/v1/agents/register", headers=headers, json={
        "agent_id": name, "introduction": {"name": name, "function": "code"}})
    assert named.status_code in (200, 201), named.text
    return {"headers": headers, "agent_id": named.json()["agent_id"]}


def test_one_state_for_every_kind_of_piece():
    assert [flow.task_state({"status": status}) for status in
            ("TODO", "READY", "IN_PROGRESS", "REVIEWING", "WAITING_HUMAN", "DONE")] == [
        "not_ready", "ready", "doing", "review", "you", "done"]
    # Approved and held for a deploy, or handed over to whoever is next.
    held = {"status": "HANDOFF", "handoff_brief": {"approval": {"awaiting_deploy": True}}}
    assert flow.task_state(held) == "deploy"
    assert flow.task_state({"status": "HANDOFF", "reviewed_by": None}) == "ready"
    assert flow.task_state({"status": "HANDOFF", "reviewed_by": "auditor",
                            "review_feedback": "fix the test"}) == "ready"
    assert [flow.chunk_state({"status": status}) for status in
            ("pending", "blocked", "dispatched", "awaiting_ci", "awaiting_review", "merged")] == [
        "not_ready", "not_ready", "ready", "review", "review", "done"]
    assert flow.chunk_state({"status": "dispatched", "task_status": "IN_PROGRESS"}) == "doing"
    assert flow._family_title(["联邦 02/24 F-01：域", "联邦 24/24 P5-4：合规"], "fedv2") == "联邦"
    assert flow._family_title(["R2 console", "R8 readmes"], "playtest") == "playtest"


async def test_the_loop_overview_groups_work_the_way_it_was_planned(client):
    ws = await _workspace(client)
    suffix = uuid.uuid4().hex[:5]
    lead = await _agent(client, ws, f"lead-{suffix}")
    seat = await _agent(client, ws, f"desk-{suffix}")
    assert (await client.post("/v1/runner/hello", headers=seat["headers"], json={"host": {
        "os": "linux", "arch": "x64", "hostname": "desk.local", "max_concurrency": 1,
        "runtimes": [{"id": "codex", "available": True}]}})).status_code == 200

    umbrella = (await client.post("/v1/tasks", headers=ws["human"], json={
        "task_key": f"big-{suffix}", "title": "Big job"})).json()["task_key"]
    await client.post(f"/v1/tasks/{umbrella}/claim", headers=lead["headers"], json={})
    split = await client.post(f"/v1/tasks/{umbrella}/subtasks", headers=lead["headers"], json={
        "tasks": [{"title": "Part one", "task_key": f"part1-{suffix}"},
                  {"title": "Part two", "task_key": f"part2-{suffix}"}]})
    assert split.status_code == 201, split.text
    family = f"fam{suffix}"
    for index in range(3):
        made = await client.post("/v1/tasks", headers=ws["human"], json={
            "task_key": f"{family}-step-{index}", "title": f"Series {index}/3: step",
            "status": "TODO" if index else "READY"})
        assert made.status_code == 201, made.text
    loose = (await client.post("/v1/tasks", headers=ws["human"], json={
        "task_key": f"alone-{suffix}", "title": "On its own"})).json()["task_key"]
    claimed = await client.post(f"/v1/tasks/{loose}/claim", headers=lead["headers"], json={})
    assert claimed.status_code == 200, claimed.text

    read = await client.get("/v1/loop/overview", headers=ws["human"])
    assert read.status_code == 200, read.text
    body = read.json()
    lanes = {lane["lane_id"]: lane for lane in body["lanes"]}
    big = lanes[f"task:{umbrella}"]
    assert big["kind"] == "umbrella" and big["title"] == "Big job"
    assert {item["key"] for item in big["items"]} == {
        umbrella, f"part1-{suffix}", f"part2-{suffix}"}
    assert big["counts"]["doing"] == 1 and big["counts"]["ready"] == 2
    series = lanes[f"family:{family}"]
    assert series["title"] == "Series"
    assert (series["counts"]["ready"], series["counts"]["not_ready"]) == (1, 2)
    inbox = lanes["inbox"]
    alone = next(item for item in inbox["items"] if item["key"] == loose)
    assert alone["state"] == "doing" and alone["holder"] == lead["agent_id"]
    assert inbox["stuck"]["key"] == loose
    assert body["open"] == 7
    assert body["needs_you"] == {"deploy": [], "waiting": []}
    assert len(body["today"]["hours"]) == 24

    # The computer's seat is not on the agent roster.
    roster = {agent["agent_id"]: agent for agent in body["agents"]}
    assert seat["agent_id"] not in roster
    assert set(roster[lead["agent_id"]]["holding"]) == {umbrella, loose}
    assert any(event["type"] == "TASK_CLAIMED" and event["task_key"] == loose
               for event in body["timeline"])
