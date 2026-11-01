"""Per-repository fenced merge coordinator.

Gate workers run in parallel and persist a verified task commit as a
``merge_intents`` row.  One elected coordinator per workspace performs the
serialized Git ``update-ref <new> <old>`` CAS and the DB finalization, records
the result durably, and reconciles either crash ordering on startup.

Fencing is a workspace lease row (``merge_coordinators.fencing_token``).  Intent
result UPDATEs require ``status='RUNNING' AND coordinator_token=<token>``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import select, text

from app.config import settings
from app.database import session_scope
from app.models import CodeArtifact, DagTask, TaskStatus, Workspace
from app.services.ast_slicer import get_slicer
from app.engine.scheduler import DisjointWriteSetScheduler
from app.services.event_bus import EventBus
from app.services.git_ops import GitRepository
from app.services.runtime_metrics import get_runtime_metrics
from app.mcp_server.syscalls import GateCheckResult
from app.services.verification_gate import (
    DeterministicVerificationGate,
    MergeFenceLost,
    VerifiedPatch,
)

logger = logging.getLogger("veynod.merge_coordinator")

MAX_REVALIDATIONS = 3
_TEST_INFRA = {
    "conftest.py",
    "pytest.ini",
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
}


class MergeCoordinator:
    def __init__(self, coordinator_id: str | None = None) -> None:
        self.coordinator_id = coordinator_id or (
            f"merge-{secrets.token_hex(6)}"
        )
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def run_forever(self) -> None:
        try:
            await self.reconcile_all()
        except Exception:  # noqa: BLE001 - keep the coordinator alive
            logger.exception("merge coordinator startup reconciliation failed")
        while not self._stopping:
            try:
                await self.process_all()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("merge coordinator iteration failed")
            await asyncio.sleep(0.2)

    # --------------------------------------------------------------- election
    async def acquire(self, workspace_id: uuid.UUID) -> int | None:
        token = secrets.randbits(62) + 1
        async with session_scope() as session:
            row = (
                await session.execute(
                    text(
                        """
                        INSERT INTO merge_coordinators (
                            workspace_id, coordinator_id, fencing_token,
                            lease_expires_at
                        ) VALUES (
                            :workspace_id, :coordinator_id, :token,
                            NOW() + (:lease_seconds * INTERVAL '1 second')
                        )
                        ON CONFLICT (workspace_id) DO UPDATE
                        SET coordinator_id = EXCLUDED.coordinator_id,
                            fencing_token = EXCLUDED.fencing_token,
                            lease_expires_at = EXCLUDED.lease_expires_at,
                            updated_at = NOW()
                        WHERE merge_coordinators.lease_expires_at < NOW()
                           OR merge_coordinators.coordinator_id = :coordinator_id
                        RETURNING fencing_token
                        """
                    ),
                    {
                        "workspace_id": workspace_id,
                        "coordinator_id": self.coordinator_id,
                        "token": token,
                        "lease_seconds": max(10, settings.merge_coordinator_lease_seconds),
                    },
                )
            ).first()
            await session.commit()
            return int(row[0]) if row else None

    async def renew(self, workspace_id: uuid.UUID, token: int) -> bool:
        async with session_scope() as session:
            row = (
                await session.execute(
                    text(
                        """
                        UPDATE merge_coordinators
                        SET lease_expires_at =
                                NOW() + (:lease_seconds * INTERVAL '1 second'),
                            updated_at = NOW()
                        WHERE workspace_id = :workspace_id
                          AND coordinator_id = :coordinator_id
                          AND fencing_token = :token
                        RETURNING fencing_token
                        """
                    ),
                    {
                        "workspace_id": workspace_id,
                        "coordinator_id": self.coordinator_id,
                        "token": token,
                        "lease_seconds": max(10, settings.merge_coordinator_lease_seconds),
                    },
                )
            ).first()
            await session.commit()
            return row is not None

    async def release(self, workspace_id: uuid.UUID, token: int) -> None:
        async with session_scope() as session:
            await session.execute(
                text(
                    """
                    UPDATE merge_coordinators
                    SET lease_expires_at = NOW(), updated_at = NOW()
                    WHERE workspace_id = :workspace_id AND fencing_token = :token
                    """
                ),
                {"workspace_id": workspace_id, "token": token},
            )
            await session.commit()

    # ----------------------------------------------------------------- driver
    async def _workspace_ids(self) -> list[uuid.UUID]:
        async with session_scope() as session:
            if settings.cell_id is None:
                rows = await session.execute(select(Workspace.workspace_id))
            else:
                rows = await session.execute(
                    text(
                        """
                        SELECT w.workspace_id
                        FROM workspaces w
                        JOIN repository_placements rp
                          ON rp.workspace_id = w.workspace_id
                        WHERE rp.cell_id = :cell_id AND rp.status = 'ACTIVE'
                        """
                    ),
                    {"cell_id": str(settings.cell_id)},
                )
            ids = [row[0] for row in rows.all()]
            await session.commit()
        return ids

    async def process_all(self) -> None:
        workspace_ids = await self._workspace_ids()
        for workspace_id in workspace_ids:
            token = await self.acquire(workspace_id)
            if token is None:
                continue
            try:
                while not self._stopping:
                    if not await self.renew(workspace_id, token):
                        break
                    intent = await self.claim(workspace_id, token)
                    if intent is None:
                        break
                    await self.process_intent(intent, token)
            finally:
                await self.release(workspace_id, token)

    async def claim(
        self, workspace_id: uuid.UUID, token: int
    ) -> dict[str, Any] | None:
        async with session_scope() as session:
            row = (
                await session.execute(
                    text(
                        """
                        WITH next AS (
                            SELECT merge_id FROM merge_intents
                            WHERE workspace_id = :workspace_id
                              AND (
                                  status = 'PENDING'
                                  OR (
                                      status = 'RUNNING'
                                      AND (
                                          coordinator_token IS NULL
                                          OR coordinator_token <> :token
                                      )
                                  )
                              )
                            ORDER BY created_at, merge_id
                            FOR UPDATE SKIP LOCKED
                            LIMIT 1
                        )
                        UPDATE merge_intents m
                        SET status = 'RUNNING',
                            coordinator_id = :coordinator_id,
                            coordinator_token = :token,
                            started_at = COALESCE(m.started_at, NOW()),
                            updated_at = NOW()
                        FROM next
                        WHERE m.merge_id = next.merge_id
                        RETURNING m.*
                        """
                    ),
                    {
                        "workspace_id": workspace_id,
                        "coordinator_id": self.coordinator_id,
                        "token": token,
                    },
                )
            ).mappings().first()
            await session.commit()
            return dict(row) if row else None

    # --------------------------------------------------------------- process
    async def process_intent(self, intent: Mapping[str, Any], token: int) -> None:
        merge_id = intent["merge_id"]
        workspace_id = intent["workspace_id"]
        task_id = intent["task_id"]
        try:
            async with session_scope() as session:
                task = await session.get(DagTask, task_id)
                workspace = await session.get(Workspace, workspace_id)
                if task is None or workspace is None:
                    await session.rollback()
                    await self._finish_intent(
                        merge_id, token, "REJECTED", "task_not_found",
                        "Task or workspace disappeared.",
                    )
                    return
                await session.commit()

            repo = GitRepository(workspace.repo_path, workspace.main_branch)
            await repo.validate_usable()
            current_head = await repo.head_sha()
            base_sha = str(intent["base_sha"])
            task_commit = str(intent["task_commit_sha"])

            # Git-first crash window: the verified commit (or a descendant)
            # is already on main.  Rebuild the DB finalization from the intent.
            if current_head == task_commit or (
                await repo.commit_exists(task_commit)
                and await repo.is_ancestor(task_commit, current_head)
            ):
                await self._finalize_already_merged(intent, workspace, current_head)
                return

            if current_head != base_sha:
                changed = await repo.changed_files_between(base_sha, current_head)
                if await self._dependency_changed(task, changed) or not settings.fast_forward_merge:
                    await self._record_conflict(
                        workspace_id, task_id, merge_id, changed
                    )
                    await self._revalidate(intent, "dependency_changed")
                    return

            async def before_merge() -> None:
                if self._stopping or not await self.renew(workspace_id, token):
                    raise MergeFenceLost(
                        f"coordinator {self.coordinator_id} lost workspace lease"
                    )

            gate_results = [
                GateCheckResult.model_validate(item)
                for item in (intent.get("gate_results") or [])
            ]
            verified = VerifiedPatch(
                base_sha=base_sha,
                task_commit_sha=task_commit,
                changed_files=list(intent.get("changed_files") or []),
                gate_results=gate_results,
                pool_wait_ms=int(intent.get("pool_wait_ms") or 0),
            )
            async with session_scope() as session:
                task = await session.get(DagTask, task_id)
                workspace = await session.get(Workspace, workspace_id)
                await session.commit()
                bus = EventBus(session)
                gate = DeterministicVerificationGate(
                    session, get_slicer(), DisjointWriteSetScheduler(session, bus), bus
                )
                try:
                    outcome = await gate.merge_verified(
                        task, workspace, verified, before_merge=before_merge
                    )
                except MergeFenceLost:
                    await session.rollback()
                    raise
                except Exception as exc:  # noqa: BLE001
                    await session.rollback()
                    logger.exception("merge intent %s failed", merge_id)
                    await self._revalidate(intent, f"merge_error: {exc!r}")
                    return

            if outcome.retry:
                await self._revalidate(intent, "main_advanced_during_merge")
                return
            response = outcome.response
            if response is None:
                await self._finish_intent(
                    merge_id, token, "REJECTED", "merge_rejected",
                    "Merge produced no response.",
                )
                return
            status = response.new_status.value
            if status == "COMMITTED":
                await self._finish_intent(
                    merge_id,
                    token,
                    "MERGED",
                    None,
                    None,
                    merged_commit_sha=response.committed_revision_hash,
                    merge_mode=response.merge_mode,
                    merge_timing=dict(response.merge_timing),
                    unlocked=[str(value) for value in response.unlocked_downstream_task_ids],
                    task_status="COMMITTED",
                )
            else:
                await self._finish_intent(
                    merge_id,
                    token,
                    "REJECTED",
                    "gate_rejected" if status == "LEASED" else "gate_failed",
                    "Gate rejected the patch during merge.",
                    task_status=status,
                )
        except MergeFenceLost:
            logger.warning("merge intent %s lost coordinator fence", merge_id)
        except Exception as exc:  # noqa: BLE001 - leave a durable intent state
            logger.exception("merge intent %s crashed", merge_id)
            await self._finish_intent(
                merge_id, token, "REJECTED", "coordinator_error", repr(exc)
            )

    async def _dependency_changed(
        self, task: DagTask, changed_files: list[str]
    ) -> bool:
        ids = [
            *task.read_artifact_ids,
            *task.target_artifact_ids,
            *task.test_artifact_ids,
        ]
        if ids:
            async with session_scope() as session:
                artifacts = (
                    await session.execute(
                        select(CodeArtifact.file_path).where(
                            CodeArtifact.artifact_id.in_(ids)
                        )
                    )
                ).scalars().all()
                await session.commit()
            sensitive = set(artifacts)
        else:
            sensitive = set()
        for path in changed_files:
            if path in sensitive:
                return True
            if path.split("/")[-1] in _TEST_INFRA:
                return True
            if path.startswith("tests/") or "/tests/" in path:
                return True
        return False

    async def _finalize_already_merged(
        self,
        intent: Mapping[str, Any],
        workspace: Workspace,
        git_head: str,
    ) -> None:
        """Git update-ref happened, DB did not: finish DB from the intent."""
        gate_results = [
            GateCheckResult.model_validate(item)
            for item in (intent.get("gate_results") or [])
        ]
        async with session_scope() as session:
            task = await session.get(DagTask, intent["task_id"])
            workspace = await session.get(Workspace, intent["workspace_id"])
            await session.commit()
            bus = EventBus(session)
            gate = DeterministicVerificationGate(
                session, get_slicer(), DisjointWriteSetScheduler(session, bus), bus
            )
            try:
                await gate._finalize_merge(  # noqa: SLF001 - reconciliation path
                    repo=GitRepository(workspace.repo_path, workspace.main_branch),
                    task=task,
                    workspace=workspace,
                    previous_head=str(intent["base_sha"]),
                    new_sha=git_head,
                    changed_files=list(intent.get("changed_files") or []),
                    gate_results=gate_results,
                    merge_mode=str(intent.get("merge_mode") or "reconciled"),
                    timings={},
                    started=time.monotonic(),
                )
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                logger.exception("reconcile finalize failed for %s", intent["merge_id"])
                await self._finish_intent(
                    intent["merge_id"], intent.get("coordinator_token"),
                    "REJECTED", "reconcile_error", repr(exc),
                )
                return
        await self._finish_intent(
            intent["merge_id"],
            intent.get("coordinator_token"),
            "MERGED",
            None,
            None,
            merged_commit_sha=git_head,
            merge_mode="reconciled",
            task_status="COMMITTED",
        )

    async def _record_conflict(
        self,
        workspace_id: uuid.UUID,
        task_id: uuid.UUID,
        merge_id: uuid.UUID,
        changed_files: list[str],
    ) -> None:
        async with session_scope() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO merge_conflicts
                        (workspace_id, task_id, merge_id, kind, paths, detail)
                    VALUES (
                        :workspace_id, :task_id, :merge_id,
                        'dependency_changed', CAST(:paths AS TEXT[]),
                        CAST(:detail AS JSONB)
                    )
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "task_id": str(task_id),
                    "merge_id": str(merge_id),
                    "paths": changed_files,
                    "detail": json.dumps(
                        {"reason": "main_advanced_on_sensitive_file"}, default=str
                    ),
                },
            )
            await EventBus(session).publish(
                workspace_id,
                "CONFLICT_ESCALATED",
                {
                    "task_id": str(task_id),
                    "merge_id": str(merge_id),
                    "kind": "dependency_changed",
                    "paths": changed_files[:50],
                },
            )
            await session.commit()

    async def _revalidate(self, intent: Mapping[str, Any], reason: str) -> None:
        merge_id = intent["merge_id"]
        token = int(intent["coordinator_token"])
        revalidations = int(intent.get("revalidations") or 0) + 1
        if revalidations > MAX_REVALIDATIONS:
            await self._finish_intent(
                merge_id, token, "REJECTED", "revalidation_exhausted", reason
            )
            return
        async with session_scope() as session:
            # Put the task back into LEASED so the gate worker can rerun on the
            # new HEAD; the same lease identity is retained.
            await session.execute(
                text(
                    """
                    UPDATE dag_tasks
                    SET status = 'LEASED',
                        lease_expires_at = NOW() + (:verify_seconds * INTERVAL '1 second'),
                        updated_at = NOW()
                    WHERE task_id = :task_id
                      AND status = 'VERIFYING'
                      AND lease_token_hash = :lease_hash
                    """
                ),
                {
                    "task_id": intent["task_id"],
                    "lease_hash": intent["lease_token_hash"],
                    "verify_seconds": settings.verify_lease_seconds,
                },
            )
            await session.execute(
                text(
                    """
                    UPDATE merge_intents
                    SET status = 'REVALIDATE',
                        coordinator_token = NULL,
                        coordinator_id = NULL,
                        revalidations = :revalidations,
                        error_code = 'revalidation_required',
                        error_detail = :reason,
                        updated_at = NOW()
                    WHERE merge_id = :merge_id
                      AND status = 'RUNNING'
                      AND coordinator_token = :token
                    """
                ),
                {
                    "merge_id": merge_id,
                    "token": token,
                    "revalidations": revalidations,
                    "reason": reason[:8000],
                },
            )
            await session.commit()
        get_runtime_metrics().inc("merge.revalidated")

    async def _finish_intent(
        self,
        merge_id: uuid.UUID,
        token: int | None,
        status: str,
        error_code: str | None,
        error_detail: str | None,
        *,
        merged_commit_sha: str | None = None,
        merge_mode: str | None = None,
        merge_timing: dict[str, Any] | None = None,
        unlocked: list[str] | None = None,
        task_status: str | None = None,
    ) -> bool:
        async with session_scope() as session:
            row = (
                await session.execute(
                    text(
                        """
                        UPDATE merge_intents
                        SET status = :status,
                            merged_commit_sha = :merged_commit_sha,
                            merge_mode = :merge_mode,
                            merge_timing = CAST(:merge_timing AS JSONB),
                            unlocked_downstream_task_ids =
                                CAST(:unlocked AS UUID[]),
                            task_status = :task_status,
                            error_code = :error_code,
                            error_detail = :error_detail,
                            completed_at = NOW(),
                            updated_at = NOW()
                        WHERE merge_id = :merge_id
                          AND (CAST(:token AS BIGINT) IS NULL OR coordinator_token = :token)
                          AND (
                              :token IS NULL
                              OR status = 'RUNNING'
                          )
                        RETURNING merge_id
                        """
                    ),
                    {
                        "merge_id": merge_id,
                        "token": token,
                        "status": status,
                        "merged_commit_sha": merged_commit_sha,
                        "merge_mode": merge_mode,
                        "merge_timing": json.dumps(merge_timing or {}, default=str),
                        "unlocked": unlocked or [],
                        "task_status": task_status,
                        "error_code": error_code,
                        "error_detail": (error_detail or "")[:8000] or None,
                    },
                )
            ).first()
            await session.commit()
            if status == "MERGED":
                async with session_scope() as resolved_session:
                    await resolved_session.execute(
                        text(
                            """
                            UPDATE merge_conflicts
                            SET status = 'RESOLVED', resolved_at = NOW()
                            WHERE task_id = (
                                SELECT task_id FROM merge_intents
                                WHERE merge_id = :merge_id
                            )
                              AND status = 'OPEN'
                            """
                        ),
                        {"merge_id": str(merge_id)},
                    )
                    await resolved_session.commit()
            if row is None:
                logger.warning("merge intent %s fenced out", merge_id)
                return False
        get_runtime_metrics().inc(f"merge.intent.{status.lower()}")
        return True

    # ------------------------------------------------------------ reconcile
    async def reconcile_all(self) -> None:
        workspace_ids = await self._workspace_ids()
        for workspace_id in workspace_ids:
            try:
                await self.reconcile_workspace(workspace_id)
            except Exception:  # noqa: BLE001
                logger.exception("workspace %s merge reconciliation failed", workspace_id)

    async def reconcile_workspace(self, workspace_id: uuid.UUID) -> None:
        async with session_scope() as session:
            workspace = await session.get(Workspace, workspace_id)
            if workspace is None:
                return
            await session.commit()
        repo = GitRepository(workspace.repo_path, workspace.main_branch)
        try:
            git_head = await repo.head_sha()
        except Exception:  # noqa: BLE001
            return

        async with session_scope() as session:
            await session.execute(
                text(
                    "UPDATE workspaces SET head_commit_sha = :head "
                    "WHERE workspace_id = :workspace_id AND head_commit_sha <> :head"
                ),
                {"head": git_head, "workspace_id": workspace_id},
            )
            await session.commit()

        # Git has the commit but DB may not: finalize intents whose task commit
        # is now reachable from main.
        async with session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT * FROM merge_intents
                        WHERE workspace_id = :workspace_id
                          AND status IN ('PENDING', 'RUNNING', 'MERGED')
                        ORDER BY created_at
                        """
                    ),
                    {"workspace_id": workspace_id},
                )
            ).mappings().all()
            await session.commit()
        for row in rows:
            task_commit = str(row["task_commit_sha"])
            if await repo.commit_exists(task_commit) and await repo.is_ancestor(
                task_commit, git_head
            ):
                await self._finalize_already_merged(dict(row), workspace, git_head)

        # DB says COMMITTED but Git does not contain the result: never leave a
        # false success.  Reset those tasks and ask the gate to revalidate.
        async with session_scope() as session:
            tasks = (
                await session.execute(
                    select(DagTask).where(
                        DagTask.workspace_id == workspace_id,
                        DagTask.status == TaskStatus.COMMITTED,
                        DagTask.result_commit_sha.is_not(None),
                    )
                )
            ).scalars().all()
            await session.commit()
        for task in tasks:
            result_sha = task.result_commit_sha or ""
            if await repo.commit_exists(result_sha) and await repo.is_ancestor(
                result_sha, git_head
            ):
                continue
            async with session_scope() as session:
                refreshed = await session.get(DagTask, task.task_id)
                if refreshed is None or refreshed.status != TaskStatus.COMMITTED:
                    continue
                lease_hash = await session.scalar(
                    text(
                        "SELECT lease_token_hash FROM merge_intents "
                        "WHERE task_id = :task_id ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"task_id": task.task_id},
                )
                refreshed.result_commit_sha = None
                refreshed.last_error_traceback = (
                    "[Reconcile] DB reported COMMITTED but Git main does not "
                    "contain the result commit; task requires revalidation."
                )
                if lease_hash:
                    refreshed.status = TaskStatus.LEASED
                    refreshed.lease_expires_at = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=settings.verify_lease_seconds)
                    )
                else:
                    refreshed.status = TaskStatus.FAILED
                await session.commit()
            async with session_scope() as session:
                await session.execute(
                    text(
                        """
                        UPDATE merge_intents
                        SET status = 'REVALIDATE',
                            coordinator_token = NULL,
                            coordinator_id = NULL,
                            error_code = 'db_ahead_of_git',
                            error_detail = 'Git main does not contain the DB result commit.',
                            updated_at = NOW()
                        WHERE task_id = :task_id
                          AND status IN ('PENDING', 'RUNNING', 'MERGED')
                        """
                    ),
                    {"task_id": task.task_id},
                )
                await session.commit()


__all__ = ["MergeCoordinator"]
