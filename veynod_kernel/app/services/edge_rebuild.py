"""Durable symbol-edge rebuild jobs.

``symbol_edges`` is advisory to the six syscalls but must survive a process
restart.  Jobs are rows in ``edge_rebuild_jobs``, claimed with a lease and
processed in creation order.  Any API or worker process can participate; the
lease prevents duplicate work.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import session_scope
from app.services.artifact_sync import ArtifactSynchronizer
from app.services.ast_slicer import get_slicer
from app.services.cell_registry import cell_filter_sql
from app.services.git_ops import GitRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EdgeRebuildJob:
    workspace_id: UUID
    repo_path: str
    main_branch: str
    new_sha: str
    changed_files: tuple[str, ...]


class EdgeRebuildService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._worker_id = f"edge-{os.getpid()}-{secrets.token_hex(4)}"
        self._slicer: Any = None

    @property
    def enabled(self) -> bool:
        return settings.async_edge_rebuild

    async def start(self) -> None:
        if self._task is not None:
            return
        self._slicer = get_slicer()
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="veynod-edge-rebuild")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping = True
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        # Make this process's in-flight jobs immediately claimable by a
        # successor instead of waiting for the lease TTL.
        with suppress(Exception):
            async with session_scope() as session:
                await session.execute(
                    text(
                        """
                        UPDATE edge_rebuild_jobs
                        SET status = 'PENDING',
                            locked_by = NULL,
                            lease_expires_at = NULL,
                            updated_at = NOW()
                        WHERE status = 'RUNNING' AND locked_by = :worker_id
                        """
                    ),
                    {"worker_id": self._worker_id},
                )
                await session.commit()

    async def enqueue(
        self, job: EdgeRebuildJob, session: AsyncSession | None = None
    ) -> None:
        """Persist a job.  Passing ``session`` makes it transactional with the
        merge/outbox row that caused it."""
        if not self.enabled:
            return
        params = {
            "workspace_id": job.workspace_id,
            "repo_path": job.repo_path,
            "main_branch": job.main_branch,
            "new_sha": job.new_sha,
            "changed_files": list(job.changed_files),
        }
        sql = text(
            """
            INSERT INTO edge_rebuild_jobs (
                workspace_id, repo_path, main_branch, new_sha, changed_files
            ) VALUES (
                :workspace_id, :repo_path, :main_branch, :new_sha,
                CAST(:changed_files AS TEXT[])
            )
            """
        )
        if session is not None:
            await session.execute(sql, params)
            return
        async with session_scope() as own_session:
            await own_session.execute(sql, params)
            await own_session.commit()
        if self._task is None:
            await self.start()

    async def _run(self) -> None:
        while not self._stopping:
            job = None
            try:
                job = await self._claim()
                if job is None:
                    await asyncio.sleep(0.25)
                    continue
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Edge rebuild worker failed")
                if job is not None:
                    await self._retry_or_fail(job, repr(exc))

    async def _claim(self) -> dict[str, Any] | None:
        async with session_scope() as session:
            row = (
                await session.execute(
                    text(
                        f"""
                        WITH next AS (
                            SELECT job_id FROM edge_rebuild_jobs j
                            WHERE ((status = 'PENDING' AND available_at <= NOW())
                               OR (
                                   status = 'RUNNING'
                                   AND lease_expires_at IS NOT NULL
                                   AND lease_expires_at < NOW()
                               ))
                            {cell_filter_sql("j")}
                            ORDER BY created_at, job_id
                            FOR UPDATE SKIP LOCKED
                            LIMIT 1
                        )
                        UPDATE edge_rebuild_jobs j
                        SET status = 'RUNNING',
                            attempts = j.attempts + 1,
                            locked_by = :worker_id,
                            lease_expires_at =
                                NOW() + (:lease_seconds * INTERVAL '1 second'),
                            updated_at = NOW()
                        FROM next
                        WHERE j.job_id = next.job_id
                        RETURNING j.*
                        """
                    ),
                    {
                        "worker_id": self._worker_id,
                        "lease_seconds": settings.edge_rebuild_lease_seconds,
                    },
                )
            ).mappings().first()
            await session.commit()
            return dict(row) if row else None

    async def _process(self, job: dict[str, Any]) -> None:
        async with session_scope() as session:
            repo = GitRepository(job["repo_path"], job["main_branch"])
            synchronizer = ArtifactSynchronizer(session, self._slicer)
            await synchronizer.rebuild_symbol_edges(
                job["workspace_id"],
                list(job["changed_files"]),
                job["new_sha"],
                repo,
            )
            await session.commit()
        async with session_scope() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO symbol_graph_versions (workspace_id, version)
                    VALUES (:workspace_id, 1)
                    ON CONFLICT (workspace_id) DO UPDATE
                    SET version = symbol_graph_versions.version + 1,
                        updated_at = NOW()
                    """
                ),
                {"workspace_id": str(job["workspace_id"])},
            )
            await session.execute(
                text(
                    """
                    UPDATE edge_rebuild_jobs
                    SET status = 'DONE',
                        completed_at = NOW(),
                        updated_at = NOW(),
                        locked_by = NULL,
                        lease_expires_at = NULL
                    WHERE job_id = :job_id
                      AND status = 'RUNNING'
                    """
                ),
                {"job_id": job["job_id"]},
            )
            await session.commit()

    async def _retry_or_fail(self, job: dict[str, Any], error: str) -> None:
        attempts = int(job.get("attempts") or 0)
        if attempts >= settings.edge_rebuild_max_attempts:
            status = "FAILED"
            delay = 0
        else:
            status = "PENDING"
            delay = min(60, 2 ** min(attempts, 6))
        async with session_scope() as session:
            await session.execute(
                text(
                    """
                    UPDATE edge_rebuild_jobs
                    SET status = :status,
                        available_at = NOW() + (:delay * INTERVAL '1 second'),
                        last_error = :error,
                        updated_at = NOW(),
                        locked_by = NULL,
                        lease_expires_at = NULL
                    WHERE job_id = :job_id
                    """
                ),
                {
                    "job_id": job["job_id"],
                    "status": status,
                    "delay": delay,
                    "error": error[:8000],
                },
            )
            await session.commit()


edge_rebuild_service = EdgeRebuildService()

__all__ = ["EdgeRebuildJob", "EdgeRebuildService", "edge_rebuild_service"]
