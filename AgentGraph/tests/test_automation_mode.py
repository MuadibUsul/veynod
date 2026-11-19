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


async def _mode(client: httpx.AsyncClient, ws: dict, mode: str) -> None:
    changed = await client.put("/v1/human/automation", headers=ws["owner"], json={
        "mode": mode, "auto_deploy_allowed": False})
    assert changed.status_code == 200, changed.text


BRIEF = {"summary": "built it", "key_metrics": {}, "unverified_hypotheses": [],
         "risk_flag": False}
REPORT = {"what_changed": "The report page has an export button.",
          "how_to_try": "Open Reports and press Export.", "not_yet_done": ""}


async def _claimed_task(client: httpx.AsyncClient, ws: dict, **extra) -> str:
    key = f"LOOP-{uuid.uuid4().hex[:6].upper()}"
    created = await client.post("/v1/tasks", headers=ws["key"], json={
        "task_key": key, "title": "add an export button", "assigned_role": "EXECUTOR",
        **extra})
    assert created.status_code == 201, created.text
    claimed = await client.post(f"/v1/tasks/{key}/claim", headers=ws["agent"], json={})
    assert claimed.status_code == 200, claimed.text
    return key


async def test_automation_keeps_technical_questions_from_the_owner(loop):
    ws = await _workspace(loop)
    question = {"question": "Should the export use CSV or JSON?"}
    manual = await loop.post("/v1/escalations", headers=ws["agent"], json=question)
    assert manual.status_code == 201, manual.text

    await _mode(loop, ws, "semi")
    refused = await loop.post("/v1/escalations", headers=ws["agent"], json=question)
    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["code"] == "decide_with_safe_default"
    assert error["action"] == "decide_with_safe_default_and_continue"
    assert "money" in error["details"]["owner_topics"]
    money = await loop.post("/v1/escalations", headers=ws["agent"], json={
        "question": "This needs a paid plan. Buy it?", "topic": "money"})
    assert money.status_code == 201, money.text
    # People are never screened.
    person = await loop.post("/v1/escalations", headers=ws["owner"], json=question)
    assert person.status_code == 201, person.text

    await _mode(loop, ws, "full")
    orientation = (await loop.get("/v1/orientation", headers=ws["agent"])).json()
    rules = orientation["automation"]["rules"]
    assert rules["owner_questions_block"] is False
    assert rules["after_max_rejections"] == "handoff to the next executor"
    key = await _claimed_task(loop, ws)
    whole = await loop.post(f"/v1/tasks/{key}/escalate", headers=ws["agent"], json={
        "reason": "unsure which library to use"})
    assert whole.status_code == 409, whole.text
    asked = await loop.post(f"/v1/tasks/{key}/escalate", headers=ws["agent"], json={
        "reason": "legal check", "question": "Can we store customer emails?",
        "topic": "legal"})
    assert asked.status_code == 200, asked.text
    # Full automation: the agent keeps working; the owner answers at acceptance.
    assert asked.json()["escalation"]["blocking"] is False
    assert asked.json()["task"]["status"] == "IN_PROGRESS"

    conn = await asyncpg.connect(_dsn())
    try:
        declined = await conn.fetchval(
            "SELECT count(*) FROM timeline_streams WHERE workspace_id = $1::uuid "
            "AND event_type = 'AUTOMATION_QUESTION_DECLINED'", ws["id"])
    finally:
        await conn.close()
    assert declined == 2


async def _reject_until_limit(client: httpx.AsyncClient, ws: dict, **extra) -> dict:
    key = await _claimed_task(client, ws, max_retries=1, **extra)
    handed = await client.post(f"/v1/tasks/{key}/handoff", headers=ws["agent"], json={
        "next_status": "REVIEWING", "brief": BRIEF})
    assert handed.status_code == 200, handed.text
    rejected = await client.post(f"/v1/tasks/{key}/review", headers=ws["key"], json={
        "decision": "reject", "feedback": "the button does nothing"})
    assert rejected.status_code == 200, rejected.text
    return {"key": key, "handoff": handed.json(), "review": rejected.json()}


async def test_full_automation_hands_on_instead_of_escalating(loop):
    manual_ws = await _workspace(loop)
    manual = await _reject_until_limit(loop, manual_ws)
    assert manual["review"]["task"]["status"] == "ESCALATED_TO_HUMAN"

    ws = await _workspace(loop)
    await _mode(loop, ws, "full")
    # A task with a repository keeps its review under full automation (work
    # without one is finished on trust at the hand-off, so nothing is rejected).
    full = await _reject_until_limit(loop, ws, repo="acme/site")
    assert full["review"]["task"]["status"] == "HANDOFF"
    assert full["review"]["escalated"] is False
    warnings = {item["code"] for item in full["handoff"].get("warnings", [])}
    assert "owner_report_missing" in warnings
    conn = await asyncpg.connect(_dsn())
    try:
        relayed = await conn.fetchval(
            "SELECT count(*) FROM timeline_streams WHERE workspace_id = $1::uuid "
            "AND event_type = 'TASK_RELAYED'", ws["id"])
    finally:
        await conn.close()
    assert relayed == 1


async def test_semi_automation_sends_the_owner_questions_together(loop):
    from app.database import async_session_factory
    from app.services.collab import send_batches

    ws = await _workspace(loop)
    await _mode(loop, ws, "semi")
    asked = []
    for question in ("This needs a paid plan. Buy it?", "Renew the domain for a year?"):
        made = await loop.post("/v1/escalations", headers=ws["agent"], json={
            "question": question, "topic": "money"})
        assert made.status_code == 201, made.text
        assert made.json()["batched"] is True
        asked.append(made.json()["escalation_id"])
    rules = (await loop.get("/v1/orientation", headers=ws["agent"])).json()[
        "automation"]["rules"]
    assert "batch" in rules["owner_questions_sent"]

    async def sent() -> list:
        conn = await asyncpg.connect(_dsn())
        try:
            return await conn.fetch(
                "SELECT type, targets, payload FROM workspace_events "
                "WHERE workspace_id = $1::uuid AND type IN "
                "('escalation.created', 'escalation.batch')", ws["id"])
        finally:
            await conn.close()

    assert await sent() == []  # nothing reaches the owner one by one
    conn = await asyncpg.connect(_dsn())
    try:  # the oldest question has waited past the batch window
        await conn.execute("UPDATE escalations SET created_at = NOW() - INTERVAL '31 minutes' "
                           "WHERE id = ANY($1::text[])", asked)
    finally:
        await conn.close()
    async with async_session_factory() as session:
        await send_batches(session)
    events = await sent()
    assert [row["type"] for row in events] == ["escalation.batch"]
    payload = (json.loads(events[0]["payload"]) if isinstance(events[0]["payload"], str)
               else events[0]["payload"])
    assert payload["details"]["count"] == 2
    assert sorted(item["escalation_id"] for item in payload["details"]["questions"]) == sorted(
        asked)
    targets = (json.loads(events[0]["targets"]) if isinstance(events[0]["targets"], str)
               else events[0]["targets"])
    assert targets and all(target.startswith("human:") for target in targets)

    # A person's question, and any question under full automation, goes out at once.
    person = await loop.post("/v1/escalations", headers=ws["owner"], json={
        "question": "Should we keep the old plan?", "topic": "money"})
    assert "batched" not in person.json()
