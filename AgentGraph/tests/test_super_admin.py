"""SUPER_ADMIN role: full execution, no sovereignty (spec v1, A-01..A-10)."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
BRIEF = {"summary": "ready for review", "key_metrics": {},
         "unverified_hypotheses": [], "risk_flag": False}
ACCEPTANCE = [{"id": "a1", "kind": "MANUAL", "text": "the change is durable and visible"}]


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _sql(query: str, *args):
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


async def _workspace(client) -> dict:
    created = (await client.post(
        "/v1/admin/workspaces", headers=ADMIN,
        json={"name": f"sa-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    return {"id": created["workspace"]["id"],
            "key": {"Authorization": f"Bearer {created['api_key']}"}}


async def _agent(client, ws: dict, role: str = "EXECUTOR", **extra) -> dict:
    signature = f"{role.lower()}-{uuid.uuid4().hex[:6]}"
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"agent_signature": signature, "display_name": signature,
              "role_scope": role, "allowed_namespaces": ["*"], **extra})
    assert minted.status_code == 201, minted.text
    data = minted.json()
    return {"headers": {"Authorization": f"Bearer {data['api_key']}"},
            "signature": signature, "agent_id": data["token"].get("agent_id"),
            "token_id": data["token"]["canonical_id"]}


async def _task(client, headers, **extra) -> dict:
    key = f"SA-{uuid.uuid4().hex[:6]}"
    response = await client.post("/v1/tasks", headers=headers, json={
        "task_key": key, "title": "super admin task", "assigned_role": "EXECUTOR",
        "namespace": "agentgraph", "acceptance": ACCEPTANCE, **extra})
    assert response.status_code == 201, response.text
    return response.json()


async def test_super_admin_can_be_minted_and_the_cli_offers_it(client):
    """A-01: the role exists, a token can be minted, the CLI offers it."""
    ws = await _workspace(client)
    super_admin = await _agent(client, ws, "SUPER_ADMIN")
    entry = await client.get(f"/v1/agents/{super_admin['agent_id']}",
                             headers=ws["key"])
    assert entry.status_code == 200, entry.text
    assert entry.json()["role"] == "SUPER_ADMIN"
    from pathlib import Path

    cli = (Path(__file__).resolve().parent.parent / "app" / "cli.py").read_text(encoding="utf-8")
    assert "SUPER_ADMIN" in cli


async def test_super_admin_reassigns_a_task_and_the_new_assignee_claims(client):
    """A-02: reassigning another agent's task makes it claimable by the target."""
    ws = await _workspace(client)
    super_admin = await _agent(client, ws, "SUPER_ADMIN")
    other = await _agent(client, ws)
    target = await _agent(client, ws)
    task = await _task(client, ws["key"], assignee=f"agent:{other['agent_id']}")

    updated = await client.patch(f"/v1/tasks/{task['task_key']}",
                                 headers=super_admin["headers"],
                                 json={"assignee": f"agent:{target['agent_id']}",
                                       "reason": "owner asked to move this task"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["assignee"] == f"agent:{target['agent_id']}"

    claimed = await client.post(f"/v1/tasks/{task['task_key']}/claim",
                                headers=target["headers"], json={})
    assert claimed.status_code == 200, claimed.text


async def test_super_admin_cancels_another_agents_task_with_a_reason(client):
    """A-03: cancel anyone; reason required; the audit row records it."""
    ws = await _workspace(client)
    super_admin = await _agent(client, ws, "SUPER_ADMIN")
    creator = await _agent(client, ws)
    task = await _task(client, creator["headers"])

    no_reason = await client.post(f"/v1/tasks/{task['task_key']}/cancel",
                                  headers=super_admin["headers"], json={"reason": ""})
    assert no_reason.status_code == 422, no_reason.text

    cancelled = await client.post(f"/v1/tasks/{task['task_key']}/cancel",
                                  headers=super_admin["headers"],
                                  json={"reason": "duplicate of the v2 task"})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"
    rows = await _sql(
        "SELECT agent_signature, operation FROM state_audit_diffs "
        "WHERE workspace_id = $1::uuid AND agent_signature = $2 ORDER BY created_at DESC",
        ws["id"], super_admin["signature"])
    assert rows and rows[0]["operation"] == "UPDATE"


async def test_super_admin_edits_task_fields_but_not_locked_acceptance(client):
    """A-04: title/description/priority persist; acceptance stays locked."""
    ws = await _workspace(client)
    super_admin = await _agent(client, ws, "SUPER_ADMIN")
    task = await _task(client, ws["key"])

    edited = await client.patch(f"/v1/tasks/{task['task_key']}",
                                headers=super_admin["headers"],
                                json={"title": "renamed by the super admin",
                                      "description": "a clearer description",
                                      "priority": 42,
                                      "reason": "owner asked for a clearer title"})
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["title"] == "renamed by the super admin"
    assert body["description"] == "a clearer description"
    assert body["priority"] == 42

    locked = await client.patch(f"/v1/tasks/{task['task_key']}",
                                headers=super_admin["headers"],
                                json={"acceptance": [], "reason": "try to unlock"})
    assert locked.status_code == 422, locked.text
    assert locked.json()["error"]["code"] == "validation_error"


async def test_super_admin_reads_every_namespace_and_unverified_documents(client):
    """A-05: namespace and trust filters do not hide documents from it."""
    ws = await _workspace(client)
    super_admin = await _agent(client, ws, "SUPER_ADMIN", allowed_namespaces=["agentgraph"])
    created = await client.put("/v1/documents/ops/sa-hidden-note",
                               headers=ws["key"],
                               json={"content": "draft in ops", "expected_version": 0,
                                     "namespace": "ops"})
    assert created.status_code in (200, 201), created.text
    read = await client.get("/v1/documents/ops/sa-hidden-note", headers=super_admin["headers"])
    assert read.status_code == 200, read.text
    assert read.json()["verification_status"] == "DRAFT"


async def test_super_admin_manages_tokens_and_namespaces(client):
    """A-06: issue, re-scope, rotate, revoke a normal token; identity stable."""
    ws = await _workspace(client)
    super_admin = await _agent(client, ws, "SUPER_ADMIN")
    agent_id = f"ops-{uuid.uuid4().hex[:6]}"
    before = len((await client.get("/v1/agents", headers=ws["key"])).json()["items"])

    issued = await client.post("/v1/tokens/issue", headers=super_admin["headers"], json={
        "role_scope": "EXECUTOR", "agent_id": agent_id, "display_name": "Ops Runner",
        "allowed_namespaces": ["agentgraph"], "reason": "owner asked for an ops runner"})
    assert issued.status_code == 200, issued.text
    token = issued.json()["token"]
    assert token["agent_id"] == agent_id

    namespaced = await client.patch(f"/v1/tokens/{token['canonical_id']}/namespaces",
                                    headers=super_admin["headers"],
                                    json={"allowed_namespaces": ["agentgraph", "ops"],
                                          "reason": "owner widened the scope"})
    assert namespaced.status_code == 200, namespaced.text
    assert namespaced.json()["allowed_namespaces"] == ["agentgraph", "ops"]

    rotated = await client.post(f"/v1/tokens/{token['canonical_id']}/rotate",
                                headers=super_admin["headers"])
    assert rotated.status_code == 200, rotated.text
    after = (await client.get("/v1/agents", headers=ws["key"])).json()["items"]
    assert len(after) == before + 1
    entry = next(item for item in after if item["agent_id"] == agent_id)
    assert entry["display_name"] == "Ops Runner"

    revoked = await client.post(f"/v1/tokens/{token['canonical_id']}/revoke",
                                headers=super_admin["headers"],
                                json={"reason": "test cleanup"})
    assert revoked.status_code == 200, revoked.text


async def test_super_admin_cannot_use_sovereign_actions(client):
    """A-08: no DONE, no trust changes, no self-issued SUPER_ADMIN tokens."""
    ws = await _workspace(client)
    super_admin = await _agent(client, ws, "SUPER_ADMIN")
    executor = await _agent(client, ws)
    task = await _task(client, executor["headers"])
    claimed = await client.post(f"/v1/tasks/{task['task_key']}/claim",
                                headers=executor["headers"], json={})
    assert claimed.status_code == 200, claimed.text

    done = await client.post(f"/v1/tasks/{task['task_key']}/handoff",
                             headers=super_admin["headers"],
                             json={"next_status": "DONE", "brief": BRIEF})
    assert done.status_code == 403, done.text

    await client.put("/v1/documents/agentgraph/sa-trust-note", headers=ws["key"],
                     json={"content": "note", "expected_version": 0})
    trust = await client.post("/v1/documents/agentgraph/sa-trust-note/verify",
                              headers=super_admin["headers"],
                              json={"expected_version": 1})
    assert trust.status_code == 403, trust.text

    escalation = await client.post("/v1/tokens/issue", headers=super_admin["headers"],
                                   json={"role_scope": "SUPER_ADMIN",
                                         "agent_id": "shadow-admin",
                                         "reason": "self escalation"})
    assert escalation.status_code == 403, escalation.text


async def test_revoking_the_super_admin_token_stops_it_at_once(client):
    """A-10: revoke the credential and the next call is refused."""
    ws = await _workspace(client)
    super_admin = await _agent(client, ws, "SUPER_ADMIN")
    task = await _task(client, ws["key"])

    revoked = await client.post(f"/v1/tokens/{super_admin['token_id']}/revoke",
                                headers=ws["key"], json={"reason": "owner revoked it"})
    assert revoked.status_code == 200, revoked.text
    after = await client.patch(f"/v1/tasks/{task['task_key']}",
                               headers=super_admin["headers"],
                               json={"title": "should not land", "reason": "late write"})
    assert after.status_code in (401, 403), after.text
