"""Merge duplicate directory identities (token naming cleanup)."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-horse-battery"


async def _workspace(client) -> dict:
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"merge-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    wid = created["workspace"]["id"]
    email = f"merge-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(f"/v1/admin/workspaces/{wid}/human-users", headers=ADMIN, json={
        "email": email, "display_name": "Merge Owner", "role": "OWNER",
        "password": PASSWORD})
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"id": wid, "key": {"Authorization": f"Bearer {created['api_key']}"},
            "human": {"Authorization": f"Bearer {login.json()['token']}"}}


async def _agent(client, ws: dict, agent_id: str) -> dict:
    response = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"agent_id": agent_id, "display_name": agent_id,
              "role_scope": "EXECUTOR", "allowed_namespaces": ["*"]})
    assert response.status_code == 201, response.text
    data = response.json()
    return {"headers": {"Authorization": f"Bearer {data['api_key']}"},
            "token": data["token"]}


async def test_merge_identities_migrates_refs_and_keeps_one_credential(client):
    ws = await _workspace(client)
    target = await _agent(client, ws, "claude")
    source = await _agent(client, ws, "claude-2")
    task = await client.post("/v1/tasks", headers=ws["key"], json={
        "title": "old claude task", "namespace": "default",
        "assignee": "agent:claude-2",
        "mentions": ["agent:claude-2"]})
    assert task.status_code == 201, task.text
    task_key = task.json()["task_key"]

    dry = await client.post("/v1/agents/merge", headers=ws["human"], json={
        "target": "claude", "sources": ["claude-2"], "dry_run": True})
    assert dry.status_code == 200, dry.text
    assert dry.json()["sources"] == ["claude-2"]
    # Dry run must not revoke anything.
    assert (await client.get("/v1/whoami", headers=source["headers"])).status_code == 200

    merged = await client.post("/v1/agents/merge", headers=ws["human"], json={
        "target": "claude", "sources": ["claude-2"], "dry_run": False})
    assert merged.status_code == 200, merged.text
    report = merged.json()
    assert report["target"] == "claude"
    assert report["assignee_updated"] >= 1
    assert report["mentions_updated"] >= 1

    # The newer source credential is kept; the target's old credential is gone.
    assert (await client.get("/v1/whoami", headers=target["headers"])).status_code == 401
    assert (await client.get("/v1/whoami", headers=source["headers"])).status_code == 200
    current = await client.get(f"/v1/tasks/{task_key}", headers=ws["human"])
    assert current.status_code == 200
    assert current.json()["assignee"] == "agent:claude"
    assert current.json()["mentions"] == ["agent:claude"]

    old = await client.get("/v1/agents/claude-2", headers=ws["human"])
    assert old.status_code == 200 and old.json()["agent_id"] == "claude"
    mine = await client.get("/v1/tasks", headers=source["headers"],
                            params={"claimable_by_me": "true"})
    assert task_key in {item["task_key"] for item in mine.json()["items"]}
