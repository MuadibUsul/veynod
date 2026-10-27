"""Task storage invariants (doc://veynod-role-rules §5.2, schema 3.41).

The platform's table is `collab_tasks`; `is_locked` maps to `acceptance_locked`
and `fencing_token` maps to `lease_epoch`.
"""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest

from app.database import CURRENT_SCHEMA_VERSION

pytestmark = pytest.mark.integration

ACCEPTANCE = [{"id": "acc-one", "kind": "MANUAL",
               "statement": "The task returns the documented payload."}]


def db_dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _workspace(client, admin_headers) -> str:
    response = await client.post(
        "/v1/admin/workspaces", headers=admin_headers,
        json={"name": f"invariants-{uuid.uuid4().hex[:6]}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["workspace"]["id"]


async def _insert_task(conn, workspace_id: str, key: str, *, status: str = "READY",
                       acceptance: list | None = None) -> None:
    await conn.execute(
        "INSERT INTO collab_tasks (workspace_id, task_key, title, description, status, "
        "priority, namespace, created_by, acceptance) "
        "VALUES ($1, $2, $3, $4, $5::task_status, 0, 'agentgraph', 'agent:test', $6::jsonb)",
        workspace_id, key, "Invariant task",
        "A description long enough to describe the invariant under test.",
        status, json.dumps(acceptance if acceptance is not None else ACCEPTANCE),
    )


async def _claim(conn, workspace_id: str, key: str) -> None:
    await conn.execute(
        "UPDATE collab_tasks SET status = 'IN_PROGRESS', claimed_by = 'agent:test', "
        "lease_epoch = 1, lease_expires_at = NOW() + INTERVAL '900 seconds' "
        "WHERE workspace_id = $1 AND task_key = $2",
        workspace_id, key,
    )


def test_schema_includes_the_task_invariants_migration():
    assert tuple(int(part) for part in CURRENT_SCHEMA_VERSION.split(".")) >= (3, 41, 0)


async def test_the_first_claim_locks_acceptance(client, admin_headers):
    workspace_id = await _workspace(client, admin_headers)
    conn = await asyncpg.connect(db_dsn())
    try:
        await _insert_task(conn, workspace_id, "invariant-lock")
        await _claim(conn, workspace_id, "invariant-lock")
        locked = await conn.fetchval(
            "SELECT acceptance_locked FROM collab_tasks WHERE workspace_id = $1 AND task_key = $2",
            workspace_id, "invariant-lock",
        )
        assert locked is True

        with pytest.raises(asyncpg.RaiseError, match="ERR_ACCEPTANCE_LOCKED"):
            await conn.execute(
                "UPDATE collab_tasks SET acceptance = $3::jsonb "
                "WHERE workspace_id = $1 AND task_key = $2",
                workspace_id, "invariant-lock",
                json.dumps([dict(ACCEPTANCE[0], statement="A changed statement.")]),
            )
    finally:
        await conn.close()


async def test_the_creator_path_can_change_acceptance_after_the_lock(client, admin_headers):
    workspace_id = await _workspace(client, admin_headers)
    conn = await asyncpg.connect(db_dsn())
    try:
        await _insert_task(conn, workspace_id, "invariant-authorized")
        await _claim(conn, workspace_id, "invariant-authorized")
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('veynod.acceptance_change', 'authorized', true)")
            await conn.execute(
                "UPDATE collab_tasks SET acceptance = $3::jsonb "
                "WHERE workspace_id = $1 AND task_key = $2",
                workspace_id, "invariant-authorized",
                json.dumps([dict(ACCEPTANCE[0], statement="An authorized change.")]),
            )
        stored = await conn.fetchval(
            "SELECT acceptance->0->>'statement' FROM collab_tasks "
            "WHERE workspace_id = $1 AND task_key = $2",
            workspace_id, "invariant-authorized",
        )
        assert stored == "An authorized change."
    finally:
        await conn.close()


async def test_pinned_context_docs_are_locked_after_the_claim(client, admin_headers):
    workspace_id = await _workspace(client, admin_headers)
    conn = await asyncpg.connect(db_dsn())
    try:
        await _insert_task(conn, workspace_id, "invariant-context")
        await _claim(conn, workspace_id, "invariant-context")
        with pytest.raises(asyncpg.RaiseError, match="ERR_CONTEXT_DOCS_LOCKED"):
            await conn.execute(
                "UPDATE collab_tasks SET context_docs = $3::jsonb "
                "WHERE workspace_id = $1 AND task_key = $2",
                workspace_id, "invariant-context",
                json.dumps([{"uri": "doc://other-spec", "version": 1}]),
            )
    finally:
        await conn.close()


async def test_terminal_tasks_are_immutable(client, admin_headers):
    workspace_id = await _workspace(client, admin_headers)
    conn = await asyncpg.connect(db_dsn())
    try:
        await _insert_task(conn, workspace_id, "invariant-done", status="DONE")
        with pytest.raises(asyncpg.RaiseError, match="ERR_TERMINAL_STATE_IMMUTABLE"):
            await conn.execute(
                "UPDATE collab_tasks SET description = 'A rewritten description.' "
                "WHERE workspace_id = $1 AND task_key = $2",
                workspace_id, "invariant-done",
            )
        with pytest.raises(asyncpg.RaiseError, match="ERR_TERMINAL_STATE_IMMUTABLE"):
            await conn.execute(
                "UPDATE collab_tasks SET acceptance = $3::jsonb "
                "WHERE workspace_id = $1 AND task_key = $2",
                workspace_id, "invariant-done",
                json.dumps([dict(ACCEPTANCE[0], statement="A changed terminal statement.")]),
            )
    finally:
        await conn.close()


async def test_in_progress_requires_a_lease(client, admin_headers):
    workspace_id = await _workspace(client, admin_headers)
    conn = await asyncpg.connect(db_dsn())
    try:
        with pytest.raises(asyncpg.CheckViolationError,
                           match="chk_in_progress_requires_lease"):
            await _insert_task(conn, workspace_id, "invariant-no-lease",
                               status="IN_PROGRESS")
    finally:
        await conn.close()


async def test_reviewing_requires_the_handoff_brief(client, admin_headers):
    workspace_id = await _workspace(client, admin_headers)
    conn = await asyncpg.connect(db_dsn())
    try:
        with pytest.raises(asyncpg.CheckViolationError,
                           match="chk_reviewing_requires_briefing"):
            await _insert_task(conn, workspace_id, "invariant-no-brief",
                               status="REVIEWING")
    finally:
        await conn.close()
