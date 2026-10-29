"""The supervisor steers the Loop: agenda, move and withdraw, over REST and MCP."""

from __future__ import annotations

import json
import uuid

import pytest

from tests.test_loop_steps import BRIEF, _host, _open
from tests.test_scheduler import _placements, _schedule, _task, _task_row, _workspace

pytestmark = pytest.mark.integration


async def _mcp(client, headers: dict, arguments: dict) -> dict:
    response = await client.post("/mcp/", headers={
        **headers, "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "loop", "arguments": arguments}})
    assert response.status_code == 200, response.text
    body = response.text
    if body.startswith("event:") or "data:" in body[:20]:
        body = next(line[5:] for line in body.splitlines() if line.startswith("data:"))
    return json.loads(body)["result"]


async def test_the_supervisor_moves_a_task_and_the_old_host_is_not_picked_again(client):
    ws = await _workspace(client, "full")
    first = await _host(client, ws, f"aa-{uuid.uuid4().hex[:4]}", slots=3)
    second = await _host(client, ws, f"bb-{uuid.uuid4().hex[:4]}", slots=1)
    boss = await _host(client, ws, f"sv-{uuid.uuid4().hex[:4]}", role="SUPER_ADMIN")
    key = await _task(client, ws)
    await _schedule(client, ws)
    assert (await _task_row(client, ws, key))["assignee"] == first["assignee"]
    claimed = await client.post(f"/v1/tasks/{key}/claim", headers=first["headers"], json={})
    assert claimed.status_code == 200, claimed.text
    placement = await _open(client, ws, key)

    # A worker does not steer.
    refused = await client.post(f"/v1/runner/placements/{placement['placement_id']}/move",
                                headers=second["headers"], json={})
    assert refused.status_code == 403, refused.text

    moved = await client.post(f"/v1/runner/placements/{placement['placement_id']}/move",
                              headers=boss["headers"], json={"note": "stuck for an hour"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["to_host"] == second["host_id"]
    task = await _task_row(client, ws, key)
    assert task["status"] == "READY" and task["assignee"] == second["assignee"], task

    # The second host gives it back; the first is still not chosen.
    await client.post(f"/v1/runner/hosts/{second['host_id']}/pause", headers=ws["owner"])
    await _schedule(client, ws)
    task = await _task_row(client, ws, key)
    assert task["assignee"] is None, task
    waiting = next(item for item in await _placements(client, ws, key)
                   if item["status"] == "unplaced")
    assert waiting["reason"] == "every_host_failed"


async def test_a_named_host_that_cannot_take_it_is_refused(client):
    ws = await _workspace(client, "full")
    worker = await _host(client, ws, f"wk-{uuid.uuid4().hex[:4]}")
    other_repo = await _host(client, ws, f"or-{uuid.uuid4().hex[:4]}")
    from tests.test_scheduler import _sql

    await _sql("UPDATE runner_hosts SET repos = '{acme/site}' WHERE host_id = $1",
               other_repo["host_id"])
    key = await _task(client, ws)
    await _schedule(client, ws)
    placement = await _open(client, ws, key)
    assert placement["host_id"] == worker["host_id"]

    refused = await client.post(f"/v1/runner/placements/{placement['placement_id']}/move",
                                headers=ws["owner"], json={"host_id": other_repo["host_id"]})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "host_does_not_fit"
    same = await client.post(f"/v1/runner/placements/{placement['placement_id']}/move",
                             headers=ws["owner"], json={"host_id": worker["host_id"]})
    assert same.status_code == 409, same.text
    assert (await _task_row(client, ws, key))["assignee"] == worker["assignee"]


async def test_a_withdrawn_task_stays_off_the_computers_until_moved_back(client):
    ws = await _workspace(client, "full")
    worker = await _host(client, ws, f"wk-{uuid.uuid4().hex[:4]}")
    boss = await _host(client, ws, f"sv-{uuid.uuid4().hex[:4]}", capabilities=["supervisor"])
    key = await _task(client, ws)
    await _schedule(client, ws)
    placement = await _open(client, ws, key)
    assert placement["host_id"] == worker["host_id"]

    pulled = await client.post(f"/v1/runner/placements/{placement['placement_id']}/withdraw",
                               headers=boss["headers"], json={"note": "spec is wrong"})
    assert pulled.status_code == 200, pulled.text
    assert pulled.json()["held"] is True
    await _schedule(client, ws)
    await _schedule(client, ws)
    task = await _task_row(client, ws, key)
    assert task["status"] == "READY" and task["assignee"] is None, task
    assert [item["status"] for item in await _placements(client, ws, key)] == ["withdrawn"]

    again = await client.post(f"/v1/runner/placements/{placement['placement_id']}/withdraw",
                              headers=boss["headers"], json={})
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "placement_not_movable"

    back = await client.post(f"/v1/runner/placements/{placement['placement_id']}/move",
                             headers=boss["headers"], json={"host_id": worker["host_id"]})
    assert back.status_code == 200, back.text
    assert (await _task_row(client, ws, key))["assignee"] == worker["assignee"]


async def test_a_review_moves_to_a_named_reviewer_but_never_to_the_builder(client):
    ws = await _workspace(client, "full")
    worker = await _host(client, ws, f"wk-{uuid.uuid4().hex[:4]}", capabilities=["review"])
    first = await _host(client, ws, f"r1-{uuid.uuid4().hex[:4]}", role="AUDITOR")
    second = await _host(client, ws, f"r2-{uuid.uuid4().hex[:4]}", role="AUDITOR", slots=1)
    key = await _task(client, ws)
    await _schedule(client, ws)
    claimed = await client.post(f"/v1/tasks/{key}/claim", headers=worker["headers"], json={})
    assert claimed.status_code == 200, claimed.text
    handed = await client.post(f"/v1/tasks/{key}/handoff", headers=worker["headers"], json={
        "next_status": "REVIEWING", "brief": BRIEF,
        "lease_epoch": claimed.json()["task"]["lease_epoch"]})
    assert handed.status_code == 200, handed.text
    await _schedule(client, ws)
    review = await _open(client, ws, key)
    assert review["step"] == "review"
    target = second if review["host_id"] == first["host_id"] else first

    builder = await client.post(f"/v1/runner/placements/{review['placement_id']}/move",
                                headers=ws["owner"], json={"host_id": worker["host_id"]})
    assert builder.status_code == 409, builder.text
    held = await client.post(f"/v1/runner/placements/{review['placement_id']}/withdraw",
                             headers=ws["owner"], json={})
    assert held.status_code == 409, held.text

    moved = await client.post(f"/v1/runner/placements/{review['placement_id']}/move",
                              headers=ws["owner"], json={"host_id": target["host_id"]})
    assert moved.status_code == 200, moved.text
    now_open = await _open(client, ws, key)
    assert now_open["step"] == "review" and now_open["host_id"] == target["host_id"]
    placed = (await client.get("/v1/inbox", headers=target["headers"])).json()["items"]
    assert any(now_open["placement_id"] in (item.get("note") or "") for item in placed)


async def test_the_loop_tool_over_mcp(client):
    ws = await _workspace(client, "full")
    worker = await _host(client, ws, f"wk-{uuid.uuid4().hex[:4]}")
    boss = await _host(client, ws, f"sv-{uuid.uuid4().hex[:4]}", role="SUPER_ADMIN")
    key = await _task(client, ws, repo="acme/unreachable")
    await _schedule(client, ws)

    agenda = await _mcp(client, boss["headers"], {"action": "agenda"})
    assert not agenda.get("isError"), agenda
    items = json.loads(agenda["content"][0]["text"])["items"]
    assert any(item["kind"] == "no_host" and item["task_key"] == key for item in items)
    refused = await _mcp(client, worker["headers"], {"action": "agenda"})
    assert refused.get("isError"), refused

    listed = await _mcp(client, boss["headers"], {"action": "placements", "task_key": key,
                                                  "open_only": True})
    placement = json.loads(listed["content"][0]["text"])["items"][0]
    assert placement["status"] == "unplaced"
    pulled = await _mcp(client, boss["headers"], {"action": "withdraw",
                                                  "placement_id": placement["placement_id"]})
    assert not pulled.get("isError"), pulled
    hosts = await _mcp(client, boss["headers"], {"action": "hosts"})
    assert worker["host_id"] in hosts["content"][0]["text"]
    missing = await _mcp(client, boss["headers"], {"action": "move"})
    assert missing.get("isError"), missing
