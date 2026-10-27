"""Stable agent identity, token generations and rename migration (TASK-8012EB76)."""

from __future__ import annotations

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
