"""Restart reconciliation tests for the per-repository merge coordinator."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import text

from app.config import settings
from app.database import configure_database, init_db, session_scope
from app.models import DagTask
from app.services.merge_coordinator import MergeCoordinator
from app.services.verification_gate import MergeFenceLost, assert_task_mergeable

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_test",
)
pytestmark = pytest.mark.integration


def _dsn() -> str:
    return TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


def _run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise RuntimeError(f"{cmd} failed\n{result.stdout}\n{result.stderr}")
    return result.stdout


async def _prepare(tmp_path: Path) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, Path, str, str]:
    await _reset()
    settings.database_url = TEST_DB_URL
    settings.auto_init_db = True
    await init_db()
    await configure_database(TEST_DB_URL)

    work = tmp_path / "work"
    work.mkdir()
    _run(["git", "init", "-b", "main"], work)
    _run(["git", "config", "user.name", "Coord"], work)
    _run(["git", "config", "user.email", "coord@example.com"], work)
    (work / "src").mkdir()
    (work / "src" / "a.py").write_text("def f() -> int:\n    ...\n", encoding="utf-8")
    _run(["git", "add", "-A"], work)
    _run(["git", "commit", "-m", "base"], work)
    base = _run(["git", "rev-parse", "HEAD"], work).strip()
    bare = tmp_path / "bare.git"
    _run(["git", "clone", "--bare", str(work), str(bare)], tmp_path)

    wt = tmp_path / "task-wt"
    _run(["git", "worktree", "add", "--detach", str(wt), base], bare)
    try:
        (wt / "src" / "a.py").write_text(
            "def f() -> int:\n    return 1\n", encoding="utf-8"
        )
        _run(["git", "add", "-A"], wt)
        _run(["git", "commit", "-m", "task commit"], wt)
        task_commit = _run(["git", "rev-parse", "HEAD"], wt).strip()
    finally:
        _run(["git", "worktree", "remove", "--force", str(wt)], bare)

    workspace_id = uuid.uuid4()
    task_id = uuid.uuid4()
    submission_id = uuid.uuid4()
    lease_hash = "e" * 64
    async with session_scope() as session:
        await session.execute(
            text(
                "INSERT INTO workspaces (workspace_id, name, repo_path, main_branch, "
                "head_commit_sha) VALUES (:workspace, 'coord', :repo, 'main', :base)"
            ),
            {"workspace": workspace_id, "repo": str(bare), "base": base},
        )
        await session.execute(
            text(
                "INSERT INTO dag_tasks (task_id, workspace_id, title, spec_prompt, type, "
                "status, write_locks, leased_by_runner, lease_token_hash, lease_expires_at) "
                "VALUES (:task, :workspace, 'coord task', 'coord test', "
                "CAST('IMPLEMENT_BODY' AS task_type), CAST('VERIFYING' AS task_status), "
                "ARRAY['src/a.py::f'], 'runner', :lease, NOW() + INTERVAL '10 minutes')"
            ),
            {"task": task_id, "workspace": workspace_id, "lease": lease_hash},
        )
        await session.execute(
            text(
                "INSERT INTO patch_submissions (submission_id, workspace_id, task_id, "
                "runner_id, lease_token_hash, idempotency_key, patch_sha256, git_diff, "
                "touched_symbols, local_test_passed, lease_expires_at) VALUES "
                "(:submission, :workspace, :task, 'runner', :lease, 'coord-key', "
                ":sha, 'diff --git a/src/a.py b/src/a.py', ARRAY['src/a.py::f'], TRUE, "
                "NOW() + INTERVAL '10 minutes')"
            ),
            {
                "submission": submission_id,
                "workspace": workspace_id,
                "task": task_id,
                "lease": lease_hash,
                "sha": "f" * 64,
            },
        )
        await session.execute(
            text(
                "INSERT INTO merge_intents (merge_id, workspace_id, task_id, submission_id, "
                "base_sha, task_commit_sha, lease_token_hash, changed_files, gate_results, "
                "status, coordinator_token) VALUES (:merge, :workspace, :task, :submission, "
                ":base, :task_commit, :lease, ARRAY['src/a.py'], CAST('[]' AS JSONB), "
                "'RUNNING', NULL)"
            ),
            {
                "merge": uuid.uuid4(),
                "workspace": workspace_id,
                "task": task_id,
                "submission": submission_id,
                "base": base,
                "task_commit": task_commit,
                "lease": lease_hash,
            },
        )
        await session.commit()
    return workspace_id, task_id, submission_id, bare, base, task_commit


async def _reset() -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_merge_fence_rejects_reassigned_task(tmp_path: Path) -> None:
    name = TEST_DB_URL.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in name.lower() and os.environ.get("VEYNOD_ALLOW_DESTRUCTIVE_TESTS") != "1":
        pytest.skip("TEST_DATABASE_URL must point at a disposable test database")
    workspace_id, task_id, _submission_id, _bare, _base, _task_commit = await _prepare(
        tmp_path
    )
    try:
        async with session_scope() as session:
            task = await session.get(DagTask, task_id)
            assert task is not None
            # The fresh VERIFYING lease is mergeable.
            await assert_task_mergeable(session, task, task.lease_token_hash)
            await session.rollback()

        # Simulate a reaper/another holder re-leasing the task while an old
        # merge intent is still pending.  The old intent must be fenced out.
        async with session_scope() as session:
            await session.execute(
                text(
                    "UPDATE dag_tasks SET status = 'LEASED', "
                    "lease_token_hash = :new_hash WHERE task_id = :task_id"
                ),
                {"new_hash": "f" * 64, "task_id": task_id},
            )
            await session.commit()

        async with session_scope() as session:
            task = await session.get(DagTask, task_id)
            assert task is not None
            with pytest.raises(MergeFenceLost):
                await assert_task_mergeable(session, task, "e" * 64)
            await session.rollback()
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.mark.asyncio
async def test_reconcile_git_first_crash_finalizes_db(tmp_path: Path) -> None:
    name = TEST_DB_URL.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in name.lower() and os.environ.get("VEYNOD_ALLOW_DESTRUCTIVE_TESTS") != "1":
        pytest.skip("TEST_DATABASE_URL must point at a disposable test database")
    workspace_id, task_id, submission_id, bare, base, task_commit = await _prepare(tmp_path)
    try:
        _run(["git", "update-ref", "refs/heads/main", task_commit, base], bare)
        coordinator = MergeCoordinator("reconcile-git-first")
        await coordinator.reconcile_workspace(workspace_id)
        async with session_scope() as session:
            task_status = await session.scalar(
                text("SELECT status::text FROM dag_tasks WHERE task_id = :id"),
                {"id": task_id},
            )
            workspace_head = await session.scalar(
                text("SELECT head_commit_sha FROM workspaces WHERE workspace_id = :id"),
                {"id": workspace_id},
            )
            intent_status = await session.scalar(
                text(
                    "SELECT status FROM merge_intents WHERE submission_id = :id"
                ),
                {"id": submission_id},
            )
            await session.commit()
        assert task_status == "COMMITTED", task_status
        assert workspace_head == task_commit, workspace_head
        assert intent_status == "MERGED", intent_status
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.mark.asyncio
async def test_reconcile_db_first_crash_reverts_false_commit(tmp_path: Path) -> None:
    name = TEST_DB_URL.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in name.lower() and os.environ.get("VEYNOD_ALLOW_DESTRUCTIVE_TESTS") != "1":
        pytest.skip("TEST_DATABASE_URL must point at a disposable test database")
    workspace_id, task_id, submission_id, bare, base, task_commit = await _prepare(tmp_path)
    try:
        # Simulate DB-first: task and head claim the commit, Git main is still base.
        async with session_scope() as session:
            await session.execute(
                text(
                    "UPDATE dag_tasks SET status = 'COMMITTED', "
                    "result_commit_sha = :sha WHERE task_id = :id"
                ),
                {"sha": task_commit, "id": task_id},
            )
            await session.execute(
                text(
                    "UPDATE workspaces SET head_commit_sha = :sha "
                    "WHERE workspace_id = :id"
                ),
                {"sha": task_commit, "id": workspace_id},
            )
            await session.execute(
                text(
                    "UPDATE merge_intents SET status = 'MERGED', "
                    "merged_commit_sha = :sha WHERE submission_id = :id"
                ),
                {"sha": task_commit, "id": submission_id},
            )
            await session.commit()
        coordinator = MergeCoordinator("reconcile-db-first")
        await coordinator.reconcile_workspace(workspace_id)
        async with session_scope() as session:
            task_status = await session.scalar(
                text("SELECT status::text FROM dag_tasks WHERE task_id = :id"),
                {"id": task_id},
            )
            workspace_head = await session.scalar(
                text("SELECT head_commit_sha FROM workspaces WHERE workspace_id = :id"),
                {"id": workspace_id},
            )
            intent_status = await session.scalar(
                text(
                    "SELECT status FROM merge_intents WHERE submission_id = :id"
                ),
                {"id": submission_id},
            )
            await session.commit()
        assert task_status != "COMMITTED", task_status
        assert workspace_head == base, workspace_head
        assert intent_status == "REVALIDATE", intent_status
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)
