"""Runner hosts: the computers that run agents (full-managed Loop, runner-2)."""

from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-horse-battery"

HOST = {
    "os": "linux", "arch": "aarch64", "hostname": "pi-kitchen.local",
    "runner_version": "0.4.0", "max_concurrency": 2,
    "repos": ["MuadibUsul/veynod", "not a repo", "acme/site"],
    "runtimes": [
        {"id": "claude", "version": "2.1.3", "available": True,
         "models": ["sonnet", {"id": "opus"}],
         "usage": {"plan": "max", "windows": [
             {"name": "5h", "used_percent": 41.5, "resets_at": "2026-10-28T23:00:00Z"}]},
         "account_email": "dou@example.com", "oauth_token": "sk-ant-abcdefghijk"},
        {"id": "codex", "version": "0.80.0", "available": False},
        {"id": "Bad Runtime!", "version": "1"},
    ],
    "api_key": "ag_should_never_be_stored_1234",
}


async def _workspace(client) -> dict:
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"runners-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    wid = created["workspace"]["id"]
    email = f"runners-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(f"/v1/admin/workspaces/{wid}/human-users", headers=ADMIN, json={
        "email": email, "display_name": "Runner Owner", "role": "OWNER",
        "password": PASSWORD})
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"id": wid, "human": {"Authorization": f"Bearer {login.json()['token']}"}}


async def _runner(client, ws: dict) -> dict:
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"role_scope": "EXECUTOR", "allowed_namespaces": ["*"]})
    assert minted.status_code == 201, minted.text
    headers = {"Authorization": f"Bearer {minted.json()['api_key']}"}
    named = await client.post("/v1/agents/register", headers=headers, json={
        "introduction": {"name": "Veynod runner", "function": "code", "runs_on": "a Pi"}})
    assert named.status_code in (200, 201), named.text
    return {"headers": headers, "agent_id": named.json()["agent_id"]}


async def test_a_runner_says_hello_and_nothing_secret_is_kept(client):
    ws = await _workspace(client)
    runner = await _runner(client, ws)

    hello = await client.post("/v1/runner/hello", headers=runner["headers"],
                              json={"host": HOST})
    assert hello.status_code == 200, hello.text
    host = hello.json()
    assert host["created"] is True
    assert host["permanent_address"] == f"host:{host['host_id']}"
    assert host["host_id"].startswith("hst_")
    assert host["agent_id"] == runner["agent_id"]
    assert (host["os"], host["arch"]) == ("linux", "arm64")
    assert host["hostname_digest"] and "pi-kitchen" not in json.dumps(host)
    assert host["repos"] == ["MuadibUsul/veynod", "acme/site"]
    assert host["max_concurrency"] == 2 and host["free_slots"] == 2
    assert host["state"] == "online"
    claude, codex = host["runtimes"]
    assert claude == {"id": "claude", "available": True, "version": "2.1.3",
                      "models": ["sonnet", "opus"],
                      "usage": {"plan": "max", "windows": [
                          {"name": "5h", "used_percent": 41.5,
                           "resets_at": "2026-10-28T23:00:00Z"}]}}
    assert codex["available"] is False
    stored = json.dumps(host)
    for secret in ("dou@example.com", "sk-ant", "ag_should_never", "oauth_token", "api_key"):
        assert secret not in stored

    # Reconnecting is the same host, not a second one.
    again = await client.post("/v1/runner/hello", headers=runner["headers"],
                              json={"host": {**HOST, "max_concurrency": 3}})
    assert again.status_code == 200, again.text
    assert again.json()["created"] is False
    assert again.json()["host_id"] == host["host_id"]
    assert again.json()["max_concurrency"] == 3

    listed = await client.get("/v1/runner/hosts", headers=ws["human"])
    assert listed.status_code == 200, listed.text
    assert [item["host_id"] for item in listed.json()["items"]] == [host["host_id"]]
    assert listed.json()["counts"]["online"] == 1

    presence = await client.get(f"/v1/agents/{runner['agent_id']}", headers=ws["human"])
    assert presence.json()["presence"]["online"] is True


async def test_heartbeat_reports_running_tasks_and_quota(client):
    ws = await _workspace(client)
    runner = await _runner(client, ws)

    early = await client.post("/v1/runner/heartbeat", headers=runner["headers"], json={})
    assert early.status_code == 409, early.text
    assert early.json()["error"]["code"] == "runner_hello_required"

    hello = (await client.post("/v1/runner/hello", headers=runner["headers"],
                               json={"host": HOST})).json()
    beat = await client.post("/v1/runner/heartbeat", headers=runner["headers"], json={
        "running": ["task-a"],
        "runtimes": [{"id": "claude", "usage": {"windows": [
            {"name": "5h", "used_percent": 97}]}}],
        # The runner sends its capability report with every beat, so the strict
        # schema has to accept it; without the field every beat 422s and the
        # host's lease/worktree card freezes at hello time.
        "capabilities_report": {"delivery": "kernel", "mcp_pinning": True,
                                "leases": [{"task_key": "task-a", "worktree": "leased",
                                            "lease": "held"}]}})
    assert beat.status_code == 200, beat.text
    assert beat.json()["running"] == ["task-a"]
    assert beat.json()["free_slots"] == 1
    assert beat.json()["runtimes"][0]["usage"]["windows"][0]["used_percent"] == 97
    assert beat.json()["capabilities_report"]["delivery"] == "kernel"
    assert beat.json()["capabilities_report"]["leases"][0]["worktree"] == "leased"

    for ref in (hello["host_id"], hello["permanent_address"], runner["agent_id"]):
        one = await client.get(f"/v1/runner/hosts/{ref}", headers=runner["headers"])
        assert one.status_code == 200, (ref, one.text)
        assert one.json()["host_id"] == hello["host_id"]
    missing = await client.get("/v1/runner/hosts/hst_nothing", headers=ws["human"])
    assert missing.status_code == 404


async def test_people_read_hosts_over_mcp(client):
    ws = await _workspace(client)
    runner = await _runner(client, ws)
    hello = (await client.post("/v1/runner/hello", headers=runner["headers"],
                               json={"host": HOST})).json()

    response = await client.post("/mcp/", headers={
        **runner["headers"], "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "list_hosts", "arguments": {}}})
    assert response.status_code == 200, response.text
    body = response.text
    if body.startswith("event:") or "data:" in body[:20]:
        body = next(line[5:] for line in body.splitlines() if line.startswith("data:"))
    result = json.loads(body)["result"]
    assert not result.get("isError"), result
    assert hello["host_id"] in result["content"][0]["text"]


async def test_the_owner_revokes_a_host_and_its_task_goes_back(client):
    ws = await _workspace(client)
    runner = await _runner(client, ws)
    hello = (await client.post("/v1/runner/hello", headers=runner["headers"],
                               json={"host": HOST})).json()
    task_key = f"runner-task-{uuid.uuid4().hex[:6]}"
    made = await client.post("/v1/tasks", headers=ws["human"], json={
        "task_key": task_key, "title": "Something for the runner",
        "assigned_role": "EXECUTOR", "namespace": "default"})
    assert made.status_code == 201, made.text
    claimed = await client.post(f"/v1/tasks/{task_key}/claim", headers=runner["headers"],
                                json={})
    assert claimed.status_code == 200, claimed.text

    refused = await client.post(f"/v1/runner/hosts/{hello['host_id']}/revoke",
                                headers=runner["headers"], json={})
    assert refused.status_code == 403, refused.text

    revoked = await client.post(f"/v1/runner/hosts/{hello['permanent_address']}/revoke",
                                headers=ws["human"], json={"reason": "lost the Pi"})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["state"] == "revoked"
    assert revoked.json()["released_leases"] >= 1

    task = await client.get(f"/v1/tasks/{task_key}", headers=ws["human"])
    assert task.json()["status"] == "READY", task.text
    dead = await client.post("/v1/runner/heartbeat", headers=runner["headers"], json={})
    assert dead.status_code == 401, dead.text
    listed = await client.get("/v1/runner/hosts", headers=ws["human"])
    assert listed.json()["items"] == []
    everything = await client.get("/v1/runner/hosts", headers=ws["human"],
                                  params={"include_revoked": "true"})
    assert everything.json()["counts"]["revoked"] == 1


async def _seat(client, ws: dict, role: str, name: str) -> dict:
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"role_scope": role, "allowed_namespaces": ["*"]})
    assert minted.status_code == 201, minted.text
    headers = {"Authorization": f"Bearer {minted.json()['api_key']}"}
    named = await client.post("/v1/agents/register", headers=headers, json={
        "agent_id": name, "introduction": {"name": name, "function": "code"}})
    assert named.status_code in (200, 201), named.text
    return {"headers": headers, "agent_id": named.json()["agent_id"],
            "token_id": minted.json()["token"]["canonical_id"]}


async def test_a_computer_is_a_workstation_with_seats_not_an_agent(client):
    """`veynod up --run-tasks` mints the computer's key and a -review key.

    Both say hello from the same machine: one computer with two seats, which
    the token list, onboarding and the live space do not count as agents.
    """
    ws = await _workspace(client)
    suffix = uuid.uuid4().hex[:5]
    work = await _seat(client, ws, "EXECUTOR", f"desk-{suffix}")
    review = await _seat(client, ws, "AUDITOR", f"desk-{suffix}-review")
    person = await _seat(client, ws, "EXECUTOR", f"helper-{suffix}")
    machine = {**HOST, "hostname": "desk.local", "os": "win32", "arch": "x64"}
    for seat in (work, review):
        hello = await client.post("/v1/runner/hello", headers=seat["headers"],
                                  json={"host": machine})
        assert hello.status_code == 200, hello.text
    elsewhere = await _seat(client, ws, "EXECUTOR", f"pi-{suffix}")
    assert (await client.post("/v1/runner/hello", headers=elsewhere["headers"],
                              json={"host": HOST})).status_code == 200

    listed = (await client.get("/v1/runner/hosts", headers=ws["human"])).json()
    assert len(listed["items"]) == 3
    computers = {item["name"]: item for item in listed["computers"]}
    assert set(computers) == {work["agent_id"], elsewhere["agent_id"]}
    desk = computers[work["agent_id"]]
    assert [(seat["agent_id"], seat["seat"]) for seat in desk["seats"]] == [
        (work["agent_id"], "work"), (review["agent_id"], "review")]
    assert (desk["os"], desk["state"], desk["max_concurrency"]) == ("windows", "online", 4)
    assert listed["computer_counts"]["online"] == 2

    for path in (f"/v1/admin/workspaces/{ws['id']}/agent-tokens", "/v1/human/agent-tokens"):
        headers = ADMIN if path.startswith("/v1/admin") else ws["human"]
        tokens = (await client.get(path, headers=headers)).json()
        ids = {item["canonical_id"] for item in tokens["items"]}
        assert person["token_id"] in ids
        assert not ids & {work["token_id"], review["token_id"], elsewhere["token_id"]}
        assert tokens["computer_keys"] == 3
        every = (await client.get(path, headers=headers,
                                  params={"include_computers": "true"})).json()
        assert {work["token_id"], review["token_id"]} <= {
            item["canonical_id"] for item in every["items"]}

    progress = (await client.get("/v1/human/agent-onboarding", headers=ws["human"])).json()
    assert {item["token_id"] for item in progress["items"]} == {person["token_id"]}

    board = (await client.get("/v1/human/blackboard", headers=ws["human"])).json()
    seats = {avatar["agent_id"]: (avatar["computer_id"], avatar["seat"])
             for avatar in board["agents"]}
    assert seats[work["agent_id"]] == (desk["computer_id"], "work")
    assert seats[review["agent_id"]] == (desk["computer_id"], "review")
    assert seats[person["agent_id"]] == (None, None)
    drawn = {item["computer_id"]: item for item in board["computers"]}
    assert [seat["seat"] for seat in drawn[desk["computer_id"]]["seats"]] == ["work", "review"]
    assert "token_id" not in json.dumps(board["computers"])

    # Its work seat taken off the Loop: the computer stays, says so, and no longer
    # counts what only that seat could run.
    for seat, ready in ((work, True), (review, False)):
        assert (await client.post("/v1/runner/hello", headers=seat["headers"], json={"host": {
            **machine, "runtimes": [{"id": "codex", "available": ready}]}})).status_code == 200
    work_host = next(item["host_id"] for item in listed["items"]
                     if item["agent_id"] == work["agent_id"])
    removed = await client.post(f"/v1/runner/hosts/{work_host}/revoke", headers=ws["human"],
                                json={})
    assert removed.status_code == 200, removed.text
    listed = (await client.get("/v1/runner/hosts", headers=ws["human"])).json()
    assert work["agent_id"] not in {item["agent_id"] for item in listed["items"]}
    desk = next(item for item in listed["computers"] if item["computer_id"] == desk["computer_id"])
    assert [(seat["seat"], seat["status"]) for seat in desk["seats"]] == [
        ("work", "revoked"), ("review", "active")]
    assert desk["state"] == "online" and desk["max_concurrency"] == 2
    assert desk["runtimes"] == [{"id": "codex", "available": False}]
