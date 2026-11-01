"""RBAC gateway and CommanderGrant validation (role rules v0.2 §2.5-§2.6).

acc-rbac-gate: a non-owner approval returns 403 role_permission_denied.
acc-commander-grant: COMMANDER writes need a live, in-scope X-Commander-Grant-Id.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest

from tests.conftest import run_server

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-horse-battery"
BRIEF = {"summary": "ready", "key_metrics": {}, "unverified_hypotheses": [],
         "risk_flag": False}
ACCEPTANCE = [{"id": "a1", "kind": "MANUAL", "text": "the report cites its sources"}]


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.fixture(scope="module")
async def grants(api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url
    async with run_server({"OWNER_ONLY_DONE": "true"}, ".server-commander-grants.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as http:
            yield http


async def _workspace(client: httpx.AsyncClient) -> dict:
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"cg-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    wid = created["workspace"]["id"]
    ws: dict = {"id": wid, "key": {"Authorization": f"Bearer {created['api_key']}"}}
    for role in ("OWNER", "REVIEWER"):
        email = f"{role.lower()}-{uuid.uuid4().hex[:8]}@example.test"
        made = await client.post(f"/v1/admin/workspaces/{wid}/human-users", headers=ADMIN,
                                 json={"email": email, "display_name": role,
                                       "role": role, "password": PASSWORD})
        assert made.status_code in (200, 201), made.text
        login = await client.post("/v1/auth/login",
                                  json={"email": email, "password": PASSWORD})
        assert login.status_code == 200, login.text
        ws[role.lower()] = {"Authorization": f"Bearer {login.json()['token']}"}
    ws["executor"] = await _agent(client, ws, "EXECUTOR")
    ws["commander"] = await _agent(client, ws, "COMMANDER")
    return ws


async def _agent(client: httpx.AsyncClient, ws: dict, role: str) -> dict:
    signature = f"{role.lower()}-{uuid.uuid4().hex[:6]}"
    minted = await client.post(f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
                               json={"agent_signature": signature, "role_scope": role,
                                     "allowed_namespaces": ["*"]})
    assert minted.status_code == 201, minted.text
    return {"headers": {"Authorization": f"Bearer {minted.json()['api_key']}"},
            "signature": signature}


async def _task(client: httpx.AsyncClient, ws: dict, namespace: str = "agentgraph") -> dict:
    key = f"CG-{uuid.uuid4().hex[:6]}"
    created = await client.post("/v1/tasks", headers=ws["key"], json={
        "task_key": key, "title": "commander grant target", "assigned_role": "EXECUTOR",
        "namespace": namespace, "acceptance": ACCEPTANCE})
    assert created.status_code == 201, created.text
    return created.json()


async def _reviewing_task(client: httpx.AsyncClient, ws: dict) -> dict:
    task = await _task(client, ws)
    claimed = await client.post(f"/v1/tasks/{task['task_key']}/claim",
                                headers=ws["executor"]["headers"], json={})
    assert claimed.status_code == 200, claimed.text
    handed = await client.post(f"/v1/tasks/{task['task_key']}/handoff",
                               headers=ws["executor"]["headers"],
                               json={"next_status": "REVIEWING", "brief": BRIEF})
    assert handed.status_code == 200, handed.text
    return task


async def _grant(client: httpx.AsyncClient, ws: dict, **overrides) -> dict:
    body = {
        "grantee": f"agent:{ws['commander']['signature']}",
        "allowed_actions": ["task:cancel"],
        "scope_namespace": "agentgraph",
        "reason": "owner approved the cancel sweep",
        "expires_in_seconds": 3_600,
        **overrides,
    }
    response = await client.post("/v1/human/commander-grants", headers=ws["owner"], json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _commander(ws: dict, grant_id: str | None = None) -> dict:
    headers = dict(ws["commander"]["headers"])
    if grant_id:
        headers["X-Commander-Grant-Id"] = grant_id
    return headers


async def test_a_non_owner_cannot_approve_done(grants):
    """acc-rbac-gate: the matrix denies task:approve_done to every non-owner."""
    ws = await _workspace(grants)
    task = await _reviewing_task(grants, ws)
    grant = await _grant(grants, ws, allowed_actions=["task:review"],
                         scope_task_keys=[task["task_key"]])

    commander = await grants.post(
        f"/v1/tasks/{task['task_key']}/review",
        headers={**_commander(ws, grant["grant_id"]), "Content-Type": "application/json"},
        json={"decision": "approve",
                                            "items": [{"item_id": "a1", "result": "PASS"}]})
    assert commander.status_code == 403, commander.text
    assert commander.json()["error"]["code"] == "role_permission_denied"
    assert commander.json()["error"]["details"]["action"] == "task:approve_done"

    human_admin = await grants.post(
        f"/v1/human/tasks/{task['task_key']}/review",
        headers=ws["reviewer"], json={"decision": "approve",
                                            "items": [{"item_id": "a1", "result": "PASS"}]})
    assert human_admin.status_code == 403, human_admin.text
    assert human_admin.json()["error"]["code"] == "role_permission_denied"

    owner = await grants.post(f"/v1/human/tasks/{task['task_key']}/review",
                              headers=ws["owner"], json={"decision": "approve",
                                            "items": [{"item_id": "a1", "result": "PASS"}]})
    assert owner.status_code == 200, owner.text
    assert owner.json()["status"] == "DONE"


async def test_commander_writes_require_a_live_in_scope_grant(grants):
    """acc-commander-grant: missing, revoked, expired and out-of-scope grants 403."""
    ws = await _workspace(grants)
    inside = await _task(grants, ws)
    outside = await _task(grants, ws)

    missing = await grants.post(f"/v1/tasks/{inside['task_key']}/cancel",
                                headers=ws["commander"]["headers"],
                                json={"reason": "sweep"})
    assert missing.status_code == 403, missing.text
    assert missing.json()["error"]["code"] == "commander_grant_required"

    scoped = await _grant(grants, ws, scope_task_keys=[inside["task_key"]])
    out_of_scope = await grants.post(
        f"/v1/tasks/{outside['task_key']}/cancel",
        headers=_commander(ws, scoped["grant_id"]), json={"reason": "sweep"})
    assert out_of_scope.status_code == 403, out_of_scope.text
    assert out_of_scope.json()["error"]["code"] == "commander_grant_out_of_scope"

    revoked = await grants.post(
        f"/v1/human/commander-grants/{scoped['grant_id']}/revoke", headers=ws["owner"])
    assert revoked.status_code == 200, revoked.text
    after_revoke = await grants.post(
        f"/v1/tasks/{inside['task_key']}/cancel",
        headers=_commander(ws, scoped["grant_id"]), json={"reason": "sweep"})
    assert after_revoke.status_code == 403
    assert after_revoke.json()["error"]["code"] == "commander_grant_invalid"

    expired = await _grant(grants, ws, scope_task_keys=[inside["task_key"]])
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            "UPDATE commander_grants SET created_at = NOW() - INTERVAL '2 hours', "
            "expires_at = NOW() - INTERVAL '1 second' WHERE grant_id = $1::uuid",
            expired["grant_id"])
    finally:
        await conn.close()
    stale = await grants.post(
        f"/v1/tasks/{inside['task_key']}/cancel",
        headers=_commander(ws, expired["grant_id"]), json={"reason": "sweep"})
    assert stale.status_code == 403, stale.text
    assert stale.json()["error"]["code"] == "commander_grant_expired"

    live = await _grant(grants, ws, scope_task_keys=[inside["task_key"]])
    done = await grants.post(
        f"/v1/tasks/{inside['task_key']}/cancel",
        headers=_commander(ws, live["grant_id"]), json={"reason": "sweep"})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "FAILED"


async def test_grants_are_issued_by_the_owner_only(grants):
    """grant:issue is a sovereignty capability: a reviewer account is refused."""
    ws = await _workspace(grants)
    body = {"grantee": f"agent:{ws['commander']['signature']}",
            "allowed_actions": ["task:review"], "scope_namespace": "agentgraph",
            "reason": "review the queue", "expires_in_seconds": 600}
    denied = await grants.post("/v1/human/commander-grants", headers=ws["reviewer"], json=body)
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "role_permission_denied"

    issued = await grants.post("/v1/human/commander-grants", headers=ws["owner"], json=body)
    assert issued.status_code == 201, issued.text
    listed = await grants.get("/v1/human/commander-grants", headers=ws["owner"])
    assert listed.status_code == 200, listed.text
    assert [row["grant_id"] for row in listed.json()] == [issued.json()["grant_id"]]
