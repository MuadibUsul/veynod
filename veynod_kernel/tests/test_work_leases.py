"""DB-level work-lease, reclaim and fencing tests for the external gate worker."""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import text

from app.config import settings
from app.database import configure_database, init_db, session_scope
from app.services.gate_worker import GateWorker

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_test",
)
pytestmark = pytest.mark.integration


def _asyncpg_dsn() -> str:
    return TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _reset_schema() -> None:
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_work_lease_reclaim_and_stale_worker_fencing() -> None:
    name = TEST_DB_URL.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in name.lower() and os.environ.get("VEYNOD_ALLOW_DESTRUCTIVE_TESTS") != "1":
        pytest.skip("TEST_DATABASE_URL must point at a disposable test database")
    await _reset_schema()
    settings.database_url = TEST_DB_URL
    settings.auto_init_db = True
    await init_db()
    await configure_database(TEST_DB_URL)

    workspace_id = uuid.uuid4()
    task_id = uuid.uuid4()
    submission_id = uuid.uuid4()
    lease_hash = "a" * 64
    patch_sha = "b" * 64
    async with session_scope() as session:
        await session.execute(
            text(
                """
                INSERT INTO workspaces (
                    workspace_id, name, repo_path, main_branch, head_commit_sha
                ) VALUES (
                    :workspace_id, 'lease-test', 'C:/nonexistent',
                    'main', :head
                )
                """
            ),
            {"workspace_id": workspace_id, "head": "0" * 40},
        )
        await session.execute(
            text(
                """
                INSERT INTO dag_tasks (
                    task_id, workspace_id, title, spec_prompt, type, status,
                    write_locks, leased_by_runner, lease_token_hash,
                    lease_expires_at
                ) VALUES (
                    :task_id, :workspace_id, 'lease task',
                    'Test work leases', CAST('IMPLEMENT_BODY' AS task_type),
                    CAST('LEASED' AS task_status), ARRAY['src/a.py::f'],
                    'runner-1', :lease_hash, NOW() + INTERVAL '10 minutes'
                )
                """
            ),
            {
                "task_id": task_id,
                "workspace_id": workspace_id,
                "lease_hash": lease_hash,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO patch_submissions (
                    submission_id, workspace_id, task_id, runner_id,
                    lease_token_hash, idempotency_key, patch_sha256,
                    git_diff, touched_symbols, local_test_passed,
                    lease_expires_at
                ) VALUES (
                    :submission_id, :workspace_id, :task_id, 'runner-1',
                    :lease_hash, 'lease-key', :patch_sha,
                    'diff --git a/src/a.py b/src/a.py', ARRAY['src/a.py::f'],
                    TRUE, NOW() + INTERVAL '10 minutes'
                )
                """
            ),
            {
                "submission_id": submission_id,
                "workspace_id": workspace_id,
                "task_id": task_id,
                "lease_hash": lease_hash,
                "patch_sha": patch_sha,
            },
        )
        await session.commit()

    worker_a = GateWorker("worker-a")
    worker_b = GateWorker("worker-b")
    first = await worker_a.claim()
    assert first is not None
    assert str(first["submission_id"]) == str(submission_id)
    assert await worker_a.renew(first) is True

    # Simulate a crash: the lease expires without a result.
    async with session_scope() as session:
        await session.execute(
            text(
                "UPDATE patch_submissions SET work_lease_expires_at = "
                "NOW() - INTERVAL '1 second' WHERE submission_id = :id"
            ),
            {"id": submission_id},
        )
        await session.commit()

    second = await worker_b.claim()
    assert second is not None
    assert second["work_lease_id"] != first["work_lease_id"]
    assert int(second["attempts"]) == 2

    # Old worker must not overwrite the new lease/result.
    assert (
        await worker_a._finalize(
            first, status="FAILED", task_status="LEASED", error_code="stale_worker"
        )
        is False
    )
    async with session_scope() as session:
        status = await session.scalar(
            text("SELECT status FROM patch_submissions WHERE submission_id = :id"),
            {"id": submission_id},
        )
        owner = await session.scalar(
            text(
                "SELECT work_owner FROM patch_submissions WHERE submission_id = :id"
            ),
            {"id": submission_id},
        )
        await session.commit()
    assert status == "RUNNING"
    assert owner == "worker-b"

    assert (
        await worker_b._finalize(
            second, status="FAILED", task_status="LEASED", error_code="test_reclaim"
        )
        is True
    )
    # A late old-worker result still cannot overwrite the new final state.
    assert (
        await worker_a._finalize(
            first, status="COMMITTED", task_status="COMMITTED"
        )
        is False
    )
    async with session_scope() as session:
        status = await session.scalar(
            text("SELECT status FROM patch_submissions WHERE submission_id = :id"),
            {"id": submission_id},
        )
        code = await session.scalar(
            text("SELECT error_code FROM patch_submissions WHERE submission_id = :id"),
            {"id": submission_id},
        )
        await session.commit()
    assert status == "FAILED"
    assert code == "test_reclaim"


@pytest.mark.asyncio
async def test_two_workers_cannot_claim_the_same_queued_submission() -> None:
    name = TEST_DB_URL.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in name.lower() and os.environ.get("VEYNOD_ALLOW_DESTRUCTIVE_TESTS") != "1":
        pytest.skip("TEST_DATABASE_URL must point at a disposable test database")
    await _reset_schema()
    settings.database_url = TEST_DB_URL
    settings.auto_init_db = True
    await init_db()
    await configure_database(TEST_DB_URL)

    workspace_id = uuid.uuid4()
    task_id = uuid.uuid4()
    submission_id = uuid.uuid4()
    async with session_scope() as session:
        await session.execute(
            text(
                "INSERT INTO workspaces (workspace_id, name, repo_path, main_branch, "
                "head_commit_sha) VALUES (:id, 'claim-test', 'C:/nonexistent', 'main', :head)"
            ),
            {"id": workspace_id, "head": "0" * 40},
        )
        await session.execute(
            text(
                "INSERT INTO dag_tasks (task_id, workspace_id, title, spec_prompt, type, "
                "status, write_locks, leased_by_runner, lease_token_hash, lease_expires_at) "
                "VALUES (:task_id, :workspace_id, 'claim task', 'Claim test', "
                "CAST('IMPLEMENT_BODY' AS task_type), CAST('LEASED' AS task_status), "
                "ARRAY['src/a.py::f'], 'runner-1', :lease_hash, NOW() + INTERVAL '10 minutes')"
            ),
            {
                "task_id": task_id,
                "workspace_id": workspace_id,
                "lease_hash": "c" * 64,
            },
        )
        await session.execute(
            text(
                "INSERT INTO patch_submissions (submission_id, workspace_id, task_id, "
                "runner_id, lease_token_hash, idempotency_key, patch_sha256, git_diff, "
                "touched_symbols, local_test_passed, lease_expires_at) VALUES "
                "(:submission_id, :workspace_id, :task_id, 'runner-1', :lease_hash, "
                "'claim-key', :patch_sha, 'diff --git a/src/a.py b/src/a.py', "
                "ARRAY['src/a.py::f'], TRUE, NOW() + INTERVAL '10 minutes')"
            ),
            {
                "submission_id": submission_id,
                "workspace_id": workspace_id,
                "task_id": task_id,
                "lease_hash": "c" * 64,
                "patch_sha": "d" * 64,
            },
        )
        await session.commit()

    worker_a = GateWorker("claim-a")
    worker_b = GateWorker("claim-b")
    results = await asyncio.gather(worker_a.claim(), worker_b.claim())
    claimed = [row for row in results if row is not None]
    assert len(claimed) == 1, results
    winner, winner_record = (
        (worker_a, results[0]) if results[0] is not None else (worker_b, results[1])
    )
    assert await winner._finalize(
        winner_record, status="FAILED", task_status="LEASED", error_code="cleanup"
    )
