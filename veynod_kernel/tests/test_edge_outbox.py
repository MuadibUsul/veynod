"""Persistent edge-job restart and transactional event outbox tests."""

from __future__ import annotations

import asyncio
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
from app.services.edge_rebuild import EdgeRebuildJob, EdgeRebuildService
from app.services.event_bus import EventBus
from app.services.outbox import OutboxDispatcher

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


async def _prepare(tmp_path: Path) -> tuple[uuid.UUID, Path, str]:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await conn.close()
    settings.database_url = TEST_DB_URL
    settings.auto_init_db = True
    await init_db()
    await configure_database(TEST_DB_URL)

    work = tmp_path / "work"
    work.mkdir()
    _run(["git", "init", "-b", "main"], work)
    _run(["git", "config", "user.name", "Edge"], work)
    _run(["git", "config", "user.email", "edge@example.com"], work)
    (work / "src").mkdir()
    (work / "src" / "a.py").write_text("def f() -> int:\n    ...\n", encoding="utf-8")
    _run(["git", "add", "-A"], work)
    _run(["git", "commit", "-m", "seed"], work)
    sha = _run(["git", "rev-parse", "HEAD"], work).strip()
    bare = tmp_path / "bare.git"
    _run(["git", "clone", "--bare", str(work), str(bare)], tmp_path)

    workspace_id = uuid.uuid4()
    async with session_scope() as session:
        await session.execute(
            text(
                "INSERT INTO workspaces (workspace_id, name, repo_path, main_branch, "
                "head_commit_sha) VALUES (:ws, 'edge', :repo, 'main', :sha)"
            ),
            {"ws": workspace_id, "repo": str(bare), "sha": sha},
        )
        await session.commit()
    return workspace_id, bare, sha


@pytest.mark.asyncio
async def test_edge_jobs_survive_restart_and_outbox_is_transactional(
    tmp_path: Path,
) -> None:
    name = TEST_DB_URL.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in name.lower() and os.environ.get("VEYNOD_ALLOW_DESTRUCTIVE_TESTS") != "1":
        pytest.skip("TEST_DATABASE_URL must point at a disposable test database")
    workspace_id, bare, _sha = await _prepare(tmp_path)
    settings.edge_rebuild_lease_seconds = 10
    service = EdgeRebuildService()
    await service.enqueue(
        EdgeRebuildJob(
            workspace_id=workspace_id,
            repo_path=str(bare),
            main_branch="main",
            new_sha=_sha,
            changed_files=(),
        )
    )
    await service.start()
    try:
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            async with session_scope() as session:
                status = await session.scalar(
                    text(
                        "SELECT status FROM edge_rebuild_jobs WHERE workspace_id = :ws "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"ws": workspace_id},
                )
                await session.commit()
            if status == "DONE":
                break
            await asyncio.sleep(0.1)
        assert status == "DONE", status
    finally:
        await service.stop()

    # Crash window: a RUNNING job with an expired lease must be reclaimed by a
    # fresh service process, not left stuck.
    async with session_scope() as session:
        await session.execute(
            text(
                "INSERT INTO edge_rebuild_jobs (workspace_id, repo_path, main_branch, "
                "new_sha, changed_files, status, attempts, locked_by, lease_expires_at) "
                "VALUES (:ws, :repo, 'main', :sha, '{}'::TEXT[], 'RUNNING', 1, "
                "'dead-worker', NOW() - INTERVAL '1 minute')"
            ),
            {"ws": workspace_id, "repo": str(bare), "sha": _sha},
        )
        await session.commit()
    restarted = EdgeRebuildService()
    await restarted.start()
    try:
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            async with session_scope() as session:
                status = await session.scalar(
                    text(
                        "SELECT status FROM edge_rebuild_jobs WHERE workspace_id = :ws "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"ws": workspace_id},
                )
                await session.commit()
            if status == "DONE":
                break
            await asyncio.sleep(0.1)
        assert status == "DONE", status
    finally:
        await restarted.stop()

    # Event outbox: kernel_events + outbox commit together; dispatcher retries
    # post-commit notification and can be restarted.
    async with session_scope() as session:
        await EventBus(session).publish(
            workspace_id, "TEST_OUTBOX", {"value": 1}
        )
        await session.commit()
    async with session_scope() as session:
        event_count = await session.scalar(
            text("SELECT count(*) FROM kernel_events WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
        outbox_count = await session.scalar(
            text(
                "SELECT count(*) FROM event_outbox WHERE workspace_id = :ws "
                "AND published_at IS NULL"
            ),
            {"ws": workspace_id},
        )
        await session.commit()
    assert event_count == 1
    assert outbox_count == 1
    dispatcher = OutboxDispatcher()
    await dispatcher._dispatch_once()
    async with session_scope() as session:
        unpublished = await session.scalar(
            text(
                "SELECT count(*) FROM event_outbox WHERE workspace_id = :ws "
                "AND published_at IS NULL"
            ),
            {"ws": workspace_id},
        )
        await session.commit()
    assert unpublished == 0
    shutil.rmtree(tmp_path, ignore_errors=True)
