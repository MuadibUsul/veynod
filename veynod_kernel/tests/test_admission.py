"""Per-repository admission control test."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
from sqlalchemy import text

from app.config import settings
from app.database import configure_database, init_db, session_scope
from app.engine.scheduler import AdmissionDenied, DisjointWriteSetScheduler
from app.services.event_bus import EventBus

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_test",
)
pytestmark = pytest.mark.integration


def _dsn() -> str:
    return TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.mark.asyncio
async def test_admission_pauses_when_gate_queue_reaches_limit() -> None:
    name = TEST_DB_URL.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in name.lower() and os.environ.get("VEYNOD_ALLOW_DESTRUCTIVE_TESTS") != "1":
        pytest.skip("TEST_DATABASE_URL must point at a disposable test database")
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await conn.close()
    settings.database_url = TEST_DB_URL
    settings.auto_init_db = True
    await init_db()
    await configure_database(TEST_DB_URL)

    workspace_id = uuid.uuid4()
    task_id = uuid.uuid4()
    submission_id = uuid.uuid4()
    lease_workspace_id = uuid.uuid4()
    lease_task_ids = [uuid.uuid4(), uuid.uuid4()]
    async with session_scope() as session:
        await session.execute(
            text(
                "INSERT INTO workspaces (workspace_id, name, repo_path, main_branch, "
                "head_commit_sha) VALUES (:ws, 'admission', 'C:/nonexistent', 'main', :head)"
            ),
            {"ws": workspace_id, "head": "0" * 40},
        )
        await session.execute(
            text(
                "INSERT INTO dag_tasks (task_id, workspace_id, title, spec_prompt, type, "
                "status, write_locks) VALUES (:task, :ws, 'admission task', "
                "'admission test', CAST('IMPLEMENT_BODY' AS task_type), "
                "CAST('READY' AS task_status), ARRAY['src/a.py::f'])"
            ),
            {"task": task_id, "ws": workspace_id},
        )
        await session.execute(
            text(
                "INSERT INTO patch_submissions (submission_id, workspace_id, task_id, "
                "runner_id, lease_token_hash, idempotency_key, patch_sha256, git_diff, "
                "touched_symbols, local_test_passed) VALUES (:sub, :ws, :task, 'r1', "
                ":lease, 'admission-key', :sha, 'diff --git a/src/a.py b/src/a.py', "
                "ARRAY['src/a.py::f'], TRUE)"
            ),
            {
                "sub": submission_id,
                "ws": workspace_id,
                "task": task_id,
                "lease": "a" * 64,
                "sha": "b" * 64,
            },
        )
        await session.execute(
            text(
                "INSERT INTO workspaces (workspace_id, name, repo_path, main_branch, "
                "head_commit_sha) VALUES (:ws, 'admission-leases', 'C:/nonexistent', "
                "'main', :head)"
            ),
            {"ws": lease_workspace_id, "head": "0" * 40},
        )
        for index, lease_task_id in enumerate(lease_task_ids):
            await session.execute(
                text(
                    "INSERT INTO dag_tasks (task_id, workspace_id, title, spec_prompt, "
                    "type, status, write_locks) VALUES (:task, :ws, 'lease task', "
                    "'admission test', CAST('IMPLEMENT_BODY' AS task_type), "
                    "CAST('READY' AS task_status), ARRAY[:lock])"
                ),
                {
                    "task": lease_task_id,
                    "ws": lease_workspace_id,
                    "lock": f"src/lease_{index}.py::f{index}",
                },
            )
        await session.commit()

    old_gate_limit = settings.admission_gate_queue_limit
    old_merge_limit = settings.admission_merge_queue_limit
    settings.admission_gate_queue_limit = 1
    settings.admission_merge_queue_limit = 100
    try:
        async with session_scope() as session:
            scheduler = DisjointWriteSetScheduler(session, EventBus(session))
            with pytest.raises(AdmissionDenied) as denied:
                await scheduler.claim_next_orthogonal_task(
                    workspace_id=workspace_id,
                    runner_id="runner",
                    supported_task_types=["IMPLEMENT_BODY"],
                    lease_token_hash="c" * 64,
                )
            assert denied.value.retry_after >= 1
            assert denied.value.queue_depth["gate_queue"] >= 1

        # A live LEASED task reserves capacity even before its patch is
        # accepted; otherwise an initial claim burst can overshoot the limit.
        async with session_scope() as session:
            scheduler = DisjointWriteSetScheduler(session, EventBus(session))
            claimed = await scheduler.claim_next_orthogonal_task(
                workspace_id=lease_workspace_id,
                runner_id="lease-runner",
                supported_task_types=["IMPLEMENT_BODY"],
                lease_token_hash="d" * 64,
            )
            assert claimed is not None
        async with session_scope() as session:
            scheduler = DisjointWriteSetScheduler(session, EventBus(session))
            with pytest.raises(AdmissionDenied) as lease_denied:
                await scheduler.claim_next_orthogonal_task(
                    workspace_id=lease_workspace_id,
                    runner_id="lease-runner-2",
                    supported_task_types=["IMPLEMENT_BODY"],
                    lease_token_hash="e" * 64,
                )
            assert lease_denied.value.queue_depth["leased"] >= 1
            assert lease_denied.value.queue_depth["gate_queue"] >= 1
    finally:
        settings.admission_gate_queue_limit = old_gate_limit
        settings.admission_merge_queue_limit = old_merge_limit
