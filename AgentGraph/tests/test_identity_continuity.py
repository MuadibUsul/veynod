"""Credential-generation identity continuity (spec v1, A-01 to A-07)."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}


PASSWORD = "correct-horse-battery"


async def _workspace(client) -> dict:
    created = (await client.post(
        "/v1/admin/workspaces", headers=ADMIN,
        json={"name": f"ident-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    wid = created["workspace"]["id"]
    email = f"owner-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(f"/v1/admin/workspaces/{wid}/human-users", headers=ADMIN,
                      json={"email": email, "display_name": "Owner", "role": "OWNER",
                            "password": PASSWORD})
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"id": wid,
            "key": {"Authorization": f"Bearer {created['api_key']}"},
            "human": {"Authorization": f"Bearer {login.json()['token']}"}}


async def _agent(client, ws: dict, signature: str, agent_id: str | None = None) -> dict:
    body = {"agent_signature": signature, "display_name": signature,
            "role_scope": "EXECUTOR", "allowed_namespaces": ["*"]}
    if agent_id:
        body["agent_id"] = agent_id
    minted = await client.post(f"/v1/admin/workspaces/{ws['id']}/agent-tokens",
                               headers=ADMIN, json=body)
    assert minted.status_code == 201, minted.text
    data = minted.json()
    return {"headers": {"Authorization": f"Bearer {data['api_key']}"},
            "signature": data["token"]["agent_signature"],
            "token_id": data["token"]["canonical_id"],
            "agent_id": data["token"].get("agent_id") or agent_id}


async def _task(client, headers, key: str, **extra) -> dict:
    body = {"task_key": key, "title": "identity continuity task",
            "assigned_role": "EXECUTOR", "namespace": "agentgraph", **extra}
    response = await client.post("/v1/tasks", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _rotate(client, agent: dict) -> dict:
    response = await client.post(f"/v1/agents/{agent['agent_id']}/generations",
                                 headers=agent["headers"], json={})
    assert response.status_code == 200, response.text
    return {"headers": {"Authorization": f"Bearer {response.json()['api_key']}"}, **response.json()}


async def _agents(client, ws: dict) -> list[dict]:
    response = await client.get("/v1/agents", headers=ws["key"])
    assert response.status_code == 200, response.text
    body = response.json()
    return body["items"] if isinstance(body, dict) else body


async def test_rotation_keeps_the_same_directory_identity(client):
    """A-01: a rotation stays on one entry, same agent_id and display name."""
    ws = await _workspace(client)
    agent = await _agent(client, ws, f"rot-{uuid.uuid4().hex[:6]}")
    before = await _agents(client, ws)
    await _rotate(client, agent)
    after = await _agents(client, ws)
    assert len(after) == len(before)
    entry = next(item for item in after if item["agent_id"] == agent["agent_id"])
    assert entry["display_name"]
    generations = await client.get(f"/v1/agents/{agent['agent_id']}/generations",
                                   headers=ws["key"])
    assert generations.status_code == 200, generations.text
    items = generations.json()["items"]
    assert len(items) == 2
    assert [item["status"] for item in items] == ["revoked", "active"]
    assert items[-1]["is_current"] is True


async def test_new_generation_can_cancel_its_own_task(client):
    """A-02: creator rights follow the identity, not the token instance."""
    ws = await _workspace(client)
    first = await _agent(client, ws, f"gen1-{uuid.uuid4().hex[:6]}")
    task = await _task(client, first["headers"], f"IDENT-{uuid.uuid4().hex[:6]}")
    second = await _rotate(client, first)
    cancelled = await client.post(f"/v1/tasks/{task['task_key']}/cancel",
                                  headers=second["headers"],
                                  json={"reason": "cancelled by the next generation"})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"


async def test_revoking_the_old_generation_keeps_the_task_manageable(client):
    """A-03: revoking gen1 after a rotation does not orphan its tasks."""
    ws = await _workspace(client)
    first = await _agent(client, ws, f"gen1-{uuid.uuid4().hex[:6]}")
    task = await _task(client, first["headers"], f"IDENT-{uuid.uuid4().hex[:6]}")
    second = await _rotate(client, first)
    revoked = await client.post(f"/v1/tokens/{first['token_id']}/revoke",
                                headers=ws["key"], json={"reason": "old generation"})
    assert revoked.status_code in (200, 409), revoked.text
    cancelled = await client.post(f"/v1/tasks/{task['task_key']}/cancel",
                                  headers=second["headers"],
                                  json={"reason": "gen2 still owns the task"})
    assert cancelled.status_code == 200, cancelled.text
    detail = await client.get(f"/v1/tasks/{task['task_key']}", headers=ws["key"])
    # A cancel is terminal as CANCELLED (v3_68); FAILED is for execution errors.
    assert detail.json()["status"] == "CANCELLED"


async def test_assignee_survives_a_rotation_and_the_new_generation_claims(client):
    """A-04: agent:X assignees keep resolving across generations."""
    ws = await _workspace(client)
    agent = await _agent(client, ws, f"asg-{uuid.uuid4().hex[:6]}")
    task = await _task(client, ws["key"], f"IDENT-{uuid.uuid4().hex[:6]}",
                       assignee=f"agent:{agent['agent_id']}")
    second = await _rotate(client, agent)
    claimed = await client.post(f"/v1/tasks/{task['task_key']}/claim",
                                headers=second["headers"], json={})
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["task"]["claimed_by"]


async def test_generation_ledger_reports_statuses(client):
    """A-05: the directory exposes the generation history for audit."""
    ws = await _workspace(client)
    agent = await _agent(client, ws, f"led-{uuid.uuid4().hex[:6]}")
    await _rotate(client, agent)
    ledger = await client.get(f"/v1/agents/{agent['agent_id']}/generations",
                              headers=ws["key"])
    assert ledger.status_code == 200, ledger.text
    body = ledger.json()
    assert body["agent_id"] == agent["agent_id"]
    assert [entry["status"] for entry in body["items"]] == ["revoked", "active"]
    assert body["items"][0]["revoked_at"]
    assert body["items"][1]["signature"].endswith("-v2")


async def test_a_reissued_token_reuses_the_directory_id(client):
    """A-06: reissue after revoke reuses the id; a taken id is an explicit error."""
    ws = await _workspace(client)
    agent_id = f"reuse-{uuid.uuid4().hex[:6]}"
    first = await _agent(client, ws, f"{agent_id}-v1", agent_id)
    revoked = await client.post(f"/v1/tokens/{first['token_id']}/revoke",
                                headers=ws["key"], json={"reason": "reissue test"})
    assert revoked.status_code == 200, revoked.text
    count = len(await _agents(client, ws))
    second = await _agent(client, ws, f"{agent_id}-v2", agent_id)
    assert second["agent_id"] == agent_id
    assert len(await _agents(client, ws)) == count
    assert second["agent_id"] == first["agent_id"]

    other = await _agent(client, ws, f"other-{uuid.uuid4().hex[:6]}")
    mine = next(item for item in await _agents(client, ws)
                if item["agent_id"] == other["agent_id"])
    rename = await client.patch(f"/v1/agents/{other['agent_id']}",
                                headers=ws["human"],
                                json={"fields": {"agent_id": agent_id},
                                      "expected_version": mine["version"]})
    assert rename.status_code == 409, rename.text
    assert rename.json()["error"]["code"] == "agent_id_taken"


async def test_orphaned_tasks_are_listed_and_claimed(client):
    """A-07: a creator identity with no active token does not strand its tasks."""
    ws = await _workspace(client)
    agent = await _agent(client, ws, f"orph-{uuid.uuid4().hex[:6]}")
    task = await _task(client, agent["headers"], f"IDENT-{uuid.uuid4().hex[:6]}")
    revoked = await client.post(f"/v1/tokens/{agent['token_id']}/revoke",
                                headers=ws["key"], json={"reason": "retired"})
    assert revoked.status_code == 200, revoked.text

    listed = await client.get("/v1/human/orphan-tasks", headers=ws["human"])
    assert listed.status_code == 200, listed.text
    keys = {item["task_key"] for item in listed.json()["items"]}
    assert task["task_key"] in keys

    target = await _agent(client, ws, f"heir-{uuid.uuid4().hex[:6]}")
    claimed = await client.post("/v1/human/orphan-tasks/claim", headers=ws["human"],
                                json={"task_keys": [task["task_key"]],
                                      "assignee": f"agent:{target['agent_id']}"})
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["claimed"] == [task["task_key"]]

    detail = await client.get(f"/v1/tasks/{task['task_key']}", headers=ws["key"])
    assert detail.json()["assignee"] == f"agent:{target['agent_id']}"
