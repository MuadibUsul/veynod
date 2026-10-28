"""Automation mode: manual / semi / full, immediate, audited, agent-visible."""

from __future__ import annotations

import json
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


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.fixture(scope="module")
async def loop(api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url
    async with run_server({}, ".server-automation.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as http:
            yield http


async def _workspace(client: httpx.AsyncClient) -> dict:
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"loop-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
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
    minted = await client.post(f"/v1/admin/workspaces/{wid}/agent-tokens", headers=ADMIN,
                               json={"agent_signature": f"loop-{uuid.uuid4().hex[:6]}",
                                     "role_scope": "EXECUTOR",
                                     "allowed_namespaces": ["*"]})
    assert minted.status_code == 201, minted.text
    ws["agent"] = {"Authorization": f"Bearer {minted.json()['api_key']}"}
    return ws


async def test_owner_switches_mode_and_agents_see_it(loop):
    ws = await _workspace(loop)
    current = await loop.get("/v1/human/automation", headers=ws["owner"])
    assert current.status_code == 200, current.text
    assert current.json()["mode"] == "manual"
    assert current.json()["auto_deploy_allowed"] is False
    assert current.json()["version"] == 0

    changed = await loop.put("/v1/human/automation", headers=ws["owner"], json={
        "mode": "semi", "auto_deploy_allowed": False,
        "reason": "test the switch"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["mode"] == "semi"
    assert changed.json()["version"] == 1

    orientation = await loop.get("/v1/orientation", headers=ws["agent"])
    assert orientation.status_code == 200, orientation.text
    assert orientation.json()["automation"]["mode"] == "semi"
    assert orientation.json()["automation"]["auto_deploy_allowed"] is False

    full = await loop.put("/v1/human/automation", headers=ws["owner"], json={
        "mode": "full", "auto_deploy_allowed": True,
        "reason": "owner selected full automatic"})
    assert full.status_code == 200, full.text
    assert full.json()["mode"] == "full"
    assert full.json()["auto_deploy_allowed"] is True
    assert full.json()["version"] == 2

    conn = await asyncpg.connect(_dsn())
    try:
        event = await conn.fetchrow(
            "SELECT event_type, payload FROM timeline_streams "
            "WHERE workspace_id = $1::uuid AND event_type = 'AUTOMATION_MODE_CHANGED' "
            "ORDER BY created_at DESC LIMIT 1", ws["id"])
        audit = await conn.fetchrow(
            "SELECT operation FROM state_audit_diffs "
            "WHERE workspace_id = $1::uuid AND target_table = 'automation_modes' "
            "ORDER BY created_at DESC LIMIT 1", ws["id"])
    finally:
        await conn.close()
    assert event is not None and event["event_type"] == "AUTOMATION_MODE_CHANGED"
    payload = (json.loads(event["payload"]) if isinstance(event["payload"], str)
               else event["payload"])
    assert payload["previous_mode"] == "semi"
    assert payload["mode"] == "full"
    assert audit is not None and audit["operation"] == "UPSERT"


async def test_only_the_owner_can_change_the_mode_and_values_are_valid(loop):
    ws = await _workspace(loop)
    denied = await loop.put("/v1/human/automation", headers=ws["reviewer"], json={
        "mode": "full", "auto_deploy_allowed": True})
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "role_permission_denied"

    invalid = await loop.put("/v1/human/automation", headers=ws["owner"], json={
        "mode": "turbo"})
    assert invalid.status_code == 422, invalid.text
