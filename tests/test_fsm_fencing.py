"""Lease fencing and explicit renewal (spec v0.2 §4.2 / §5.3)."""

from __future__ import annotations

import os
import uuid

import asyncpg
import httpx
import pytest

from tests.conftest import run_server

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
V2 = {"AgentGraph-Version": "2026-11"}
BRIEF = {"summary": "ready for review", "key_metrics": {},
         "unverified_hypotheses": [], "risk_flag": False}
ACCEPTANCE = [{"id": "a1", "kind": "MANUAL",
               "text": "the lease is enforced at the database clock"}]


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _sql(query: str, *args):
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


async def _workspace(client: httpx.AsyncClient) -> dict:
    created = (await client.post(
        "/v1/admin/workspaces", headers=ADMIN,
        json={"name": f"fsm-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    return {"id": created["workspace"]["id"],
            "key": {"Authorization": f"Bearer {created['api_key']}"}}


async def _agent(client: httpx.AsyncClient, ws: dict, role: str = "EXECUTOR") -> dict:
    signature = f"{role.lower()}-{uuid.uuid4().hex[:6]}"
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"agent_signature": signature, "role_scope": role,
              "allowed_namespaces": ["*"]})
    assert minted.status_code == 201, minted.text
    data = minted.json()
    return {"headers": {"Authorization": f"Bearer {data['api_key']}"},
            "signature": signature, "agent_id": data["token"].get("agent_id")}


async def _task(client: httpx.AsyncClient, ws: dict, **extra) -> dict:
    key = f"FSM-{uuid.uuid4().hex[:6]}"
    response = await client.post("/v1/tasks", headers=ws["key"], json={
        "task_key": key, "title": "lease fencing task", "assigned_role": "EXECUTOR",
        "namespace": "agentgraph", "acceptance": ACCEPTANCE, **extra})
    assert response.status_code == 201, response.text
    return response.json()


async def test_handoff_after_the_lease_expired_is_refused():
    """acc-fencing-sql: the database clock refuses a late hand-off."""
    async with run_server({"LEASE_REAPER_INTERVAL_SECONDS": "3600"},
                          ".server-fsm-fencing.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
            ws = await _workspace(client)
            agent = await _agent(client, ws)
            task = await _task(client, ws)
            claimed = await client.post(f"/v1/tasks/{task['task_key']}/claim",
                                        headers=agent["headers"], json={})
            assert claimed.status_code == 200, claimed.text
            token = claimed.json()["lease"]["fencing_token"]
            await _sql(
                "UPDATE collab_tasks SET lease_expires_at = NOW() - INTERVAL '1 second' "
                "WHERE task_key = $1", task["task_key"])

            late = await client.post(
                f"/v1/tasks/{task['task_key']}/handoff",
                headers={**agent["headers"], **V2},
                json={"next_status": "REVIEWING", "brief": BRIEF, "fencing_token": token})
            assert late.status_code == 409, late.text
            assert late.json()["error"]["code"] == "lease_expired"
            assert late.json()["error"]["details"]["task_key"] == task["task_key"]


async def test_explicit_renewal_window_and_limit():
    """acc-lease-renew: renew only in the last 300 s, at most 12 times."""
    async with run_server({"LEASE_REAPER_INTERVAL_SECONDS": "3600"},
                          ".server-fsm-renew.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
            ws = await _workspace(client)
            agent = await _agent(client, ws)
            task = await _task(client, ws)
            claimed = await client.post(f"/v1/tasks/{task['task_key']}/claim",
                                        headers=agent["headers"], json={})
            assert claimed.status_code == 200, claimed.text
            token = claimed.json()["lease"]["fencing_token"]
            renew_url = f"/v1/tasks/{task['task_key']}/lease/renew"

            early = await client.post(renew_url, headers=agent["headers"],
                                      json={"fencing_token": token})
            assert early.status_code == 409, early.text
            assert early.json()["error"]["code"] == "lease_renew_too_early"

            for _ in range(12):
                await _sql(
                    "UPDATE collab_tasks SET lease_expires_at = NOW() + INTERVAL '100 seconds' "
                    "WHERE task_key = $1", task["task_key"])
                refreshed = await client.post(renew_url, headers=agent["headers"],
                                              json={"fencing_token": token})
                assert refreshed.status_code == 200, refreshed.text
            detail = await client.get(f"/v1/tasks/{task['task_key']}", headers=ws["key"])
            remaining = detail.json()["lease_expires_at"]
            assert detail.json()["lease_active"] is True

            await _sql(
                "UPDATE collab_tasks SET lease_expires_at = NOW() + INTERVAL '100 seconds' "
                "WHERE task_key = $1", task["task_key"])
            over = await client.post(renew_url, headers=agent["headers"],
                                     json={"fencing_token": token})
            assert over.status_code == 409, over.text
            assert over.json()["error"]["code"] == "lease_renew_limit"
            assert over.json()["error"]["details"]["limit"] == 12
            assert remaining


async def test_reject_returns_ready_keeps_assignee_and_counts_a_retry():
    """acc-rework-ready: a FAIL verdict returns to READY and keeps the assignee."""
    async with run_server({}, ".server-fsm-review.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
            ws = await _workspace(client)
            executor = await _agent(client, ws)
            auditor = await _agent(client, ws, "AUDITOR")
            task = await _task(client, ws, assignee=f"agent:{executor['agent_id']}")
            claimed = await client.post(f"/v1/tasks/{task['task_key']}/claim",
                                        headers=executor["headers"], json={})
            assert claimed.status_code == 200, claimed.text
            handed = await client.post(
                f"/v1/tasks/{task['task_key']}/handoff", headers=executor["headers"],
                json={"next_status": "REVIEWING", "brief": BRIEF})
            assert handed.status_code == 200, handed.text

            rejected = await client.post(
                f"/v1/tasks/{task['task_key']}/review", headers=auditor["headers"],
                json={"decision": "reject", "feedback": "the report is missing its sources",
                      "items": [{"item_id": "a1", "result": "FAIL"}]})
            assert rejected.status_code == 200, rejected.text
            body = rejected.json()["task"]
            assert body["status"] == "READY"
            assert body["assignee"] == f"agent:{executor['agent_id']}"
            assert body["retry_count"] == 1
            assert body["ping_pong_count"] == 1
