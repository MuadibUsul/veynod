"""Open gaps found 2026-10-25 (docs/PRD_PROGRESS.md) and two playtest leftovers."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

from app.concurrency import STREAM_PATHS

pytestmark = pytest.mark.integration


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _owner(client) -> tuple[dict[str, str], str]:
    registered = await client.post("/v1/auth/register", json={
        "email": f"gap-{uuid.uuid4().hex[:8]}@example.test",
        "password": "correct-horse-battery-staple",
        "display_name": "Owner", "workspace_name": "Gaps"})
    assert registered.status_code == 201, registered.text
    body = registered.json()
    return {"Authorization": f"Bearer {body['token']}"}, body["workspace"]["id"]


async def _agent(client, owner, role="EXECUTOR") -> tuple[dict[str, str], str]:
    signature = f"g_{uuid.uuid4().hex[:6]}"
    made = await client.post("/v1/human/agent-tokens", headers=owner, json={
        "agent_signature": signature, "role_scope": role, "allowed_namespaces": ["*"]})
    assert made.status_code in (200, 201), made.text
    agent = {"Authorization": f"Bearer {made.json()['api_key']}"}
    registered = await client.post("/v1/agents/register", headers=agent, json={})
    assert registered.status_code == 201, registered.text
    return agent, registered.json()["agent_id"]


def test_the_inbox_stream_is_metered_as_a_stream():
    assert any("/v1/inbox/stream".startswith(prefix) for prefix in STREAM_PATHS)


async def test_watermarks_move_with_responsibilities(client):
    owner, _ = await _owner(client)
    old, old_id = await _agent(client, owner)
    new, new_id = await _agent(client, owner)
    cursor = (await client.get("/v1/orientation", headers=old)).json()["inbox"]["event_cursor"]
    stored = await client.put("/v1/watermarks/digest", headers=old, json={"cursor": cursor})
    assert stored.status_code == 200, stored.text

    moved = await client.post("/v1/human/transfer-responsibilities", headers=owner, json={
        "from": f"agent:{old_id}", "to": f"agent:{new_id}", "include": ["watermarks"]})
    assert moved.status_code == 200, moved.text
    assert "digest" in moved.json()["watermarks"]
    assert "watermarks" not in moved.json()["skipped"]
    mine = await client.get("/v1/watermarks/digest", headers=new)
    assert mine.status_code == 200, mine.text
    assert mine.json()["seq"] == stored.json()["seq"]


async def test_include_cold_false_leaves_cold_documents_out(client, workspace):
    owner, workspace_id = workspace["headers"], workspace["workspace"]["id"]
    for key in ("gaps/fresh", "gaps/stale"):
        made = await client.put(f"/v1/documents/{key}", headers=owner,
                                json={"namespace": "default", "content": key,
                                      "expected_version": 0})
        assert made.status_code in (200, 201), made.text
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            "UPDATE shared_documents SET updated_at = NOW() - interval '200 days', "
            "last_read_at = NULL WHERE workspace_id = $1::uuid AND doc_key = 'gaps/stale'",
            workspace_id)
    finally:
        await conn.close()
    keys = lambda response: {item["doc_key"] for item in response.json()["items"]}  # noqa: E731
    everything = await client.get("/v1/documents", headers=owner,
                                  params={"order": "doc_key_asc"})
    assert {"gaps/fresh", "gaps/stale"} <= keys(everything)
    warm = await client.get("/v1/documents", headers=owner,
                            params={"order": "doc_key_asc", "include_cold": "false"})
    assert "gaps/fresh" in keys(warm) and "gaps/stale" not in keys(warm)


async def test_rest_review_checks_permission_before_the_body(client):
    owner, _ = await _owner(client)
    executor, _ = await _agent(client, owner)
    made = await client.post("/v1/tasks", headers=owner, json={"title": "review me"})
    review = await client.post(f"/v1/tasks/{made.json()['task_key']}/review", headers=executor,
                               json={"decision": "approve", "not_a_field": True})
    assert review.status_code == 403, review.text


async def test_unchanged_schema_files_are_not_reapplied():
    from app.database import _schema_fingerprint, apply_schema

    await apply_schema(os.environ["TEST_DATABASE_URL"])
    root = __import__("pathlib").Path(__file__).resolve().parent.parent
    conn = await asyncpg.connect(_dsn())
    try:
        recorded = await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version = $1",
                                       _schema_fingerprint(root))
    finally:
        await conn.close()
    assert recorded == 1
