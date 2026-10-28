"""Stable agent identity, token generations and rename migration (TASK-8012EB76)."""

from __future__ import annotations

import asyncio
import uuid

import pytest

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-horse-battery"


async def _workspace(client) -> dict:
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"naming-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    wid = created["workspace"]["id"]
    email = f"naming-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(f"/v1/admin/workspaces/{wid}/human-users", headers=ADMIN, json={
        "email": email, "display_name": "Naming Owner", "role": "OWNER",
        "password": PASSWORD})
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"id": wid, "key": {"Authorization": f"Bearer {created['api_key']}"},
            "human": {"Authorization": f"Bearer {login.json()['token']}"}}


async def _mint(client, ws: dict, **body) -> dict:
    response = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"role_scope": "EXECUTOR", "allowed_namespaces": ["*"], **body})
    assert response.status_code == 201, response.text
    return response.json()


async def test_new_token_uses_subject_function_generation(client):
    ws = await _workspace(client)
    created = await _mint(client, ws, agent_id="zcode-deploy",
                          display_name="ZCode · Deploy")
    token = created["token"]
    assert token["agent_id"] == "zcode-deploy"
    assert token["generation"] == 1
    assert token["agent_signature"] == "zcode-deploy-v1"

    listed = (await client.get("/v1/agents?limit=200", headers=ws["human"])).json()["items"]
    entry = next(item for item in listed if item["agent_id"] == "zcode-deploy")
    assert entry["display_name"] == "ZCode · Deploy"
    assert entry["address"] == "agent:zcode-deploy"


async def test_generation_keeps_agent_id_and_old_assignments(client):
    ws = await _workspace(client)
    first = await _mint(client, ws, agent_id="zcode-deploy",
                        display_name="ZCode · Deploy")
    agent = {"Authorization": f"Bearer {first['api_key']}"}
    task = await client.post("/v1/tasks", headers=ws["key"], json={
        "title": "work for the stable identity", "namespace": "default",
        "assignee": "agent:zcode-deploy"})
    assert task.status_code == 201, task.text
    task_key = task.json()["task_key"]

    rotated = await client.post("/v1/agents/zcode-deploy/generations",
                                headers=agent, json={})
    assert rotated.status_code == 200, rotated.text
    second = rotated.json()
    assert second["agent_id"] == "zcode-deploy"
    assert second["generation"] == 2
    assert second["token"]["agent_signature"] == "zcode-deploy-v2"

    assert (await client.get("/v1/whoami", headers=agent)).status_code == 401
    fresh = {"Authorization": f"Bearer {second['api_key']}"}
    me = await client.get("/v1/whoami", headers=fresh)
    assert me.status_code == 200
    assert me.json()["agent_signature"] == "zcode-deploy-v2"

    mine = await client.get("/v1/tasks", headers=fresh, params={"claimable_by_me": "true"})
    assert mine.status_code == 200
    assert task_key in {item["task_key"] for item in mine.json()["items"]}
    claimed = await client.post(f"/v1/tasks/{task_key}/claim", headers=fresh, json={})
    assert claimed.status_code == 200, claimed.text


async def test_display_name_change_does_not_change_matching(client):
    ws = await _workspace(client)
    created = await _mint(client, ws, agent_id="zcode-deploy",
                          display_name="Old display")
    agent = {"Authorization": f"Bearer {created['api_key']}"}
    task = await client.post("/v1/tasks", headers=ws["key"], json={
        "title": "display name test", "namespace": "default",
        "assignee": "agent:zcode-deploy"})
    assert task.status_code == 201, task.text
    task_key = task.json()["task_key"]

    renamed = await client.patch("/v1/agents/zcode-deploy", headers=agent,
                                 json={"fields": {"display_name": "中文显示名"},
                                       "expected_version": (await client.get(
                                           "/v1/agents/zcode-deploy", headers=agent
                                       )).json()["version"]})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["display_name"] == "中文显示名"
    mine = await client.get("/v1/tasks", headers=agent, params={"claimable_by_me": "true"})
    assert task_key in {item["task_key"] for item in mine.json()["items"]}


async def test_rename_lists_and_migrates_task_references(client):
    ws = await _workspace(client)
    created = await _mint(client, ws, agent_id="zcode-deploy",
                          display_name="ZCode · Deploy")
    agent = {"Authorization": f"Bearer {created['api_key']}"}
    task = await client.post("/v1/tasks", headers=ws["key"], json={
        "title": "rename me", "namespace": "default",
        "assignee": "agent:zcode-deploy",
        "mentions": ["agent:zcode-deploy-v1"]})
    assert task.status_code == 201, task.text
    task_key = task.json()["task_key"]
    doc_key = f"notes/rename-ref-{uuid.uuid4().hex[:6]}"
    written = await client.put(f"/v1/documents/{doc_key}", headers=ws["key"], json={
        "content": "# Handoff\n\nAsk agent:zcode-deploy-v1 to review.",
        "expected_version": 0})
    assert written.status_code == 200, written.text

    refs = await client.get("/v1/agents/zcode-deploy/references", headers=ws["human"])
    assert refs.status_code == 200, refs.text
    body = refs.json()
    assert task_key in {item["task_key"] for item in body["tasks"]}
    assert doc_key in {item["doc_key"] for item in body["documents"]}

    renamed = await client.patch("/v1/agents/zcode-deploy", headers=ws["human"],
                                 json={"fields": {"agent_id": "zcode-runner"},
                                       "expected_version": (await client.get(
                                           "/v1/agents/zcode-deploy", headers=ws["human"]
                                       )).json()["version"]})
    assert renamed.status_code == 200, renamed.text
    report = renamed.json()["migration"]
    assert report["assignee_updated"] >= 1
    assert report["document_count"] >= 1

    current = await client.get(f"/v1/tasks/{task_key}", headers=ws["human"])
    assert current.status_code == 200
    assert current.json()["assignee"] == "agent:zcode-runner"
    old = await client.get("/v1/agents/zcode-deploy", headers=ws["human"])
    assert old.status_code == 200 and old.json()["agent_id"] == "zcode-runner"
    mine = await client.get("/v1/tasks", headers=agent, params={"claimable_by_me": "true"})
    assert task_key in {item["task_key"] for item in mine.json()["items"]}


async def test_an_unnamed_token_names_itself_on_first_connect(client):
    """The console mints role + namespaces; the agent picks its own rule id."""
    ws = await _workspace(client)
    created = await _mint(client, ws)
    token = created["token"]
    assert token["agent_id"].startswith("unnamed-")
    assert token["agent_signature"].endswith("-v1")
    agent = {"Authorization": f"Bearer {created['api_key']}"}

    unnamed = await client.post("/v1/agents/register", headers=agent, json={})
    assert unnamed.status_code == 422, unnamed.text
    assert unnamed.json()["error"]["details"]["field"] == "agent_id"

    registered = await client.post("/v1/agents/register", headers=agent, json={
        "agent_id": "self-named", "display_name": "Self Named",
        "capabilities": ["code"]})
    assert registered.status_code in (200, 201), registered.text
    assert registered.json()["agent_id"] == "self-named"

    me = await client.get("/v1/whoami", headers=agent)
    assert me.status_code == 200
    assert me.json()["agent_id"] == "self-named"
    assert me.json()["agent_signature"] == "self-named-v1"

    rotated = await client.post("/v1/agents/self-named/generations",
                                headers=agent, json={})
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["agent_id"] == "self-named"
    assert rotated.json()["token"]["agent_signature"] == "self-named-v2"


async def test_first_handshake_lets_the_platform_name_the_agent(client):
    """An unnamed agent introduces itself; the platform derives name and address."""
    ws = await _workspace(client)
    first = await _mint(client, ws)
    agent = {"Authorization": f"Bearer {first['api_key']}"}

    missing = await client.post("/v1/agents/register", headers=agent, json={
        "introduction": {"name": "Claude Code"}})
    assert missing.status_code == 422, missing.text

    hello = await client.post("/v1/agents/register", headers=agent, json={
        "introduction": {"name": "Claude Code", "function": "code review",
                         "maker": "Anthropic", "runs_on": "a cloud session"}})
    assert hello.status_code in (200, 201), hello.text
    entry = hello.json()
    assert entry["agent_id"] == "claude-code"
    assert entry["named_by"] == "platform"
    assert entry["display_name"] == "Claude Code · code review"
    assert entry["description"].startswith("Claude Code (Anthropic) · code review")
    assert entry["capabilities"] == ["code"]
    assert entry["permanent_address"].startswith("agent:prn_")

    me = (await client.get("/v1/whoami", headers=agent)).json()
    assert me["agent_id"] == "claude-code"
    assert me["agent_signature"] == "claude-code-v1"

    # A second Claude doing the same job gets its own id; the address is unique.
    second = await _mint(client, ws)
    other = {"Authorization": f"Bearer {second['api_key']}"}
    again = await client.post("/v1/agents/register", headers=other, json={
        "introduction": {"name": "Claude Code", "function": "代码"}})
    assert again.status_code in (200, 201), again.text
    assert again.json()["agent_id"] == "claude-code-2"
    assert again.json()["permanent_address"] != entry["permanent_address"]

    third = await _mint(client, ws)
    chinese = await client.post("/v1/agents/register", headers={
        "Authorization": f"Bearer {third['api_key']}"}, json={
        "introduction": {"name": "豆包", "function": "审计"}})
    assert chinese.status_code in (200, 201), chinese.text
    assert chinese.json()["agent_id"] == "agent-audit"
    assert chinese.json()["display_name"] == "豆包 · 审计"

    # The permanent address finds the agent, and survives a rename.
    principal = entry["permanent_address"].split(":", 1)[1]
    found = await client.get(f"/v1/agents/{principal}", headers=ws["human"])
    assert found.status_code == 200 and found.json()["agent_id"] == "claude-code"
    renamed = await client.patch("/v1/agents/claude-code", headers=ws["human"], json={
        "expected_version": found.json()["version"], "fields": {"agent_id": "claude-review"}})
    assert renamed.status_code == 200, renamed.text
    after = await client.get(f"/v1/agents/{principal}", headers=ws["human"])
    assert after.json()["agent_id"] == "claude-review"
    assert after.json()["permanent_address"] == entry["permanent_address"]


async def test_simultaneous_handshakes_get_different_names(client):
    """Two agents with the same introduction at the same moment never share an id."""
    ws = await _workspace(client)
    minted = [await _mint(client, ws) for _ in range(4)]
    results = await asyncio.gather(*(
        client.post("/v1/agents/register",
                    headers={"Authorization": f"Bearer {item['api_key']}"},
                    json={"introduction": {"name": "Codex", "function": "deploy"}})
        for item in minted))
    assert all(r.status_code in (200, 201) for r in results), [r.text for r in results]
    ids = sorted(r.json()["agent_id"] for r in results)
    assert ids == ["codex-deploy", "codex-deploy-2", "codex-deploy-3", "codex-deploy-4"]
    assert len({r.json()["permanent_address"] for r in results}) == 4
