"""Independent deterministic-gate worker.

The API process only persists submissions.  This process claims durable rows
with a time-limited work lease, renews it, and runs Git/typecheck/lint/pytest in
a sandbox.  A stale worker is fenced out: its result UPDATE must match both
``status='RUNNING'`` and its ``work_lease_id``; the merge path also re-checks the
lease immediately before Git CAS.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import text

from app.config import settings
from app.database import session_scope
from app.engine.scheduler import DisjointWriteSetScheduler
from app.engine.scheduler_core import scheduler_core_manager
from app.models import DagTask, Workspace
from app.services.ast_slicer import get_slicer
from app.services.cell_registry import cell_filter_sql
from app.services.event_bus import EventBus
from app.services.runtime_metrics import get_runtime_metrics
from app.services.verification_gate import (
    DeterministicVerificationGate,
    MergeFenceLost,
)

logger = logging.getLogger("veynod.gate_worker")


class WorkLeaseLost(RuntimeError):
    pass


class GateWorker:
    def __init__(self, worker_id: str | None = None) -> None:
        self.worker_id = worker_id or f"gate-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def run_forever(self) -> None:
        while not self._stopping:
            record = await self.claim()
            if record is None:
                await asyncio.sleep(settings.worker_poll_seconds)
                continue
            try:
                await self.process(record)
            except asyncio.CancelledError:
                raise
            except (WorkLeaseLost, MergeFenceLost):
                logger.warning(
                    "worker %s lost work/merge lease for submission %s",
                    self.worker_id,
                    record["submission_id"],
                )
            except Exception:  # noqa: BLE001 - keep the worker alive
                logger.exception(
                    "worker %s failed submission %s",
                    self.worker_id,
                    record["submission_id"],
                )
                await self._handle_transient(record, "worker_exception")

    # ------------------------------------------------------------------ claim
    async def claim(self) -> dict[str, Any] | None:
        lease_id = uuid.uuid4()
        async with session_scope() as session:
            result = await session.execute(
                text(
                    f"""
                    WITH next AS (
                        SELECT submission_id
                        FROM patch_submissions p
                        WHERE ((status = 'QUEUED' AND available_at <= NOW())
                           OR (
                               status = 'RUNNING'
                               AND work_lease_expires_at IS NOT NULL
                               AND work_lease_expires_at < NOW()
                           ))
                        {cell_filter_sql("p")}
                        ORDER BY created_at, submission_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE patch_submissions p
                    SET status = 'RUNNING',
                        attempts = p.attempts + 1,
                        work_lease_id = :lease_id,
                        work_owner = :worker_id,
                        work_lease_expires_at =
                            NOW() + (:lease_seconds * INTERVAL '1 second'),
                        started_at = COALESCE(p.started_at, NOW()),
                        updated_at = NOW()
                    FROM next
                    WHERE p.submission_id = next.submission_id
                    RETURNING p.*
                    """
                ),
                {
                    "lease_id": lease_id,
                    "worker_id": self.worker_id,
                    "lease_seconds": settings.work_lease_seconds,
                },
            )
            row = result.mappings().first()
            await session.commit()
            return dict(row) if row else None

    async def renew(self, record: Mapping[str, Any]) -> bool:
        async with session_scope() as session:
            row = (
                await session.execute(
                    text(
                        """
                        UPDATE patch_submissions
                        SET work_lease_expires_at =
                                NOW() + (:lease_seconds * INTERVAL '1 second'),
                            updated_at = NOW()
                        WHERE submission_id = :submission_id
                          AND status = 'RUNNING'
                          AND work_lease_id = :lease_id
                        RETURNING submission_id
                        """
                    ),
                    {
                        "submission_id": record["submission_id"],
                        "lease_id": record["work_lease_id"],
                        "lease_seconds": settings.work_lease_seconds,
                    },
                )
            ).first()
            await session.commit()
            return row is not None

    async def fence(self, record: Mapping[str, Any]) -> bool:
        """True only if this worker still owns a live work lease."""
        async with session_scope() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT 1 FROM patch_submissions
                        WHERE submission_id = :submission_id
                          AND status = 'RUNNING'
                          AND work_lease_id = :lease_id
                          AND work_lease_expires_at > NOW() + INTERVAL '2 seconds'
                        """
                    ),
                    {
                        "submission_id": record["submission_id"],
                        "lease_id": record["work_lease_id"],
                    },
                )
            ).first()
            await session.commit()
            return row is not None

    # ---------------------------------------------------------------- process
    async def process(self, record: Mapping[str, Any]) -> None:
        submission_id = record["submission_id"]
        if int(record.get("attempts") or 0) > settings.work_max_attempts:
            await self._finalize(
                record,
                status="FAILED",
                error_code="work_retries_exhausted",
                error_detail=(
                    f"Work attempts exceeded {settings.work_max_attempts}; "
                    "the submission was not executed."
                ),
            )
            return

        lost = asyncio.Event()
        cancel_signal = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(record, lost),
            name=f"work-heartbeat-{submission_id}",
        )
        gate_task = asyncio.create_task(
            self._process_with_gate(record, lost),
            name=f"work-gate-{submission_id}",
        )
        watcher = asyncio.create_task(
            self._watch_cancellation(record, gate_task, cancel_signal),
            name=f"work-cancel-watch-{submission_id}",
        )
        try:
            try:
                await gate_task
            except asyncio.CancelledError:
                if cancel_signal.is_set():
                    logger.info(
                        "gate for submission %s cancelled by task cancellation",
                        submission_id,
                    )
                    return
                raise
        finally:
            watcher.cancel()
            heartbeat.cancel()
            if not gate_task.done():
                gate_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await gate_task
            for task in (watcher, heartbeat):
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _watch_cancellation(
        self,
        record: Mapping[str, Any],
        gate_task: asyncio.Task,
        cancel_signal: asyncio.Event,
    ) -> None:
        """Poll the task row and stop an in-flight gate when it is cancelled."""
        while not self._stopping and not gate_task.done():
            await asyncio.sleep(settings.cancel_poll_seconds)
            if gate_task.done():
                return
            try:
                async with session_scope() as session:
                    row = (
                        await session.execute(
                            text(
                                "SELECT status::text AS status, lease_token_hash "
                                "FROM dag_tasks WHERE task_id = :task_id"
                            ),
                            {"task_id": record["task_id"]},
                        )
                    ).mappings().first()
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep watching while the gate runs
                continue
            expected_hash = str(record.get("lease_token_hash") or "")
            if row is not None and row["status"] == "COMMITTED":
                # The Git/DB merge already committed this task; the gate task
                # is finishing _finalize. Cancelling here would leave the
                # submission RUNNING forever.
                return
            if (
                row is None
                or row["status"] not in {"LEASED", "VERIFYING"}
                or str(row["lease_token_hash"] or "") != expected_hash
            ):
                cancel_signal.set()
                gate_task.cancel()
                return

    async def _heartbeat_loop(
        self, record: Mapping[str, Any], lost: asyncio.Event
    ) -> None:
        while not self._stopping and not lost.is_set():
            await asyncio.sleep(settings.work_heartbeat_seconds)
            try:
                if not await self.renew(record):
                    lost.set()
                    return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - retry while the lease is still valid
                logger.exception("work lease renewal failed")

    async def _process_with_gate(
        self, record: Mapping[str, Any], lost: asyncio.Event
    ) -> None:
        submission_id = record["submission_id"]
        task_id = record["task_id"]
        lease_hash = str(record["lease_token_hash"])
        runner_id = str(record["runner_id"])

        async with session_scope() as session:
            task = await session.get(DagTask, task_id)
            if task is None:
                await session.rollback()
                await self._finalize(
                    record,
                    status="FAILED",
                    task_status=None,
                    error_code="task_not_found",
                    error_detail=f"Task {task_id} disappeared.",
                )
                return
            now = datetime.now(timezone.utc)
            expired = task.lease_expires_at is not None and task.lease_expires_at <= now
            task_state = task.status.value
            await session.commit()

        if task_state == "COMMITTED":
            # A previous attempt (or an external process) completed this task.
            async with session_scope() as session:
                task = await session.get(DagTask, task_id)
                sha = task.result_commit_sha if task else None
                await session.commit()
            await self._finalize(
                record,
                status="COMMITTED",
                task_status="COMMITTED",
                result_commit_sha=sha,
            )
            return
        if task_state == "FAILED":
            await self._finalize(
                record,
                status="FAILED",
                task_status="FAILED",
                error_code="task_failed",
                error_detail="Task is already FAILED.",
            )
            return
        if task_state == "CANCELLED":
            await self._finalize(
                record,
                status="FAILED",
                task_status="CANCELLED",
                error_code="task_cancelled",
                error_detail="Task was cancelled; the submission must not merge.",
            )
            return
        if task_state == "VERIFYING":
            await self._finalize(
                record,
                status="FAILED",
                task_status="VERIFYING",
                error_code="interrupted_while_verifying",
                error_detail=(
                    "The previous worker's work lease expired after the task "
                    "entered VERIFYING; the gate is not replayed automatically."
                ),
            )
            return
        if (
            task_state != "LEASED"
            or expired
            or str(getattr(task, "lease_token_hash", "")) != lease_hash
            or str(getattr(task, "leased_by_runner", "")) != runner_id
        ):
            code = "lease_expired" if expired else "lease_lost"
            await self._finalize(
                record,
                status="FAILED",
                # A LEASED task whose work lease is gone must be re-claimable;
                # leaving it LEASED strands it until a reaper runs.
                task_status="READY" if task_state == "LEASED" else task_state,
                error_code=code,
                error_detail="The task lease is no longer held by this submission.",
            )
            if task_state == "LEASED":
                await self.release_task(
                    task_id, lease_hash, f"submission {submission_id}: {code}"
                )
            return

        # Gate stages run here; the Git CAS and DB finalization are performed
        # by the per-repository merge coordinator from a durable intent.
        for round_index in range(1, 4):
            async with session_scope() as session:
                task = await session.get(DagTask, task_id)
                workspace = (
                    await session.get(Workspace, task.workspace_id) if task else None
                )
                if task is None or workspace is None:
                    await session.rollback()
                    await self._finalize(
                        record,
                        status="FAILED",
                        task_status=None,
                        error_code="task_not_found",
                        error_detail="Task or workspace disappeared before the gate.",
                    )
                    return
                await session.commit()
                bus = EventBus(session)
                gate = DeterministicVerificationGate(
                    session,
                    get_slicer(),
                    DisjointWriteSetScheduler(session, bus),
                    bus,
                )
                try:
                    verification = await gate.verify_patch(
                        task,
                        workspace,
                        str(record["git_diff"]),
                        list(record["touched_symbols"]),
                        lease_hash,
                    )
                    await session.commit()
                except MergeFenceLost:
                    await session.rollback()
                    raise
                except Exception as exc:  # noqa: BLE001
                    await session.rollback()
                    detail = getattr(exc, "detail", None) or repr(exc)
                    if not isinstance(detail, str):
                        detail = json.dumps(detail, default=str)
                    await self._finalize(
                        record,
                        status="FAILED",
                        task_status="VERIFYING",
                        error_code="gate_error",
                        error_detail=detail,
                    )
                    logger.exception("submission %s gate failed", submission_id)
                    return

            if verification.verified is None:
                response = verification.rejection
                if response is not None:
                    await self._reject_merge_intent(record, response)
                if response is None:
                    await self._finalize(
                        record,
                        status="FAILED",
                        task_status=None,
                        error_code="gate_error",
                        error_detail="Gate returned neither a verified patch nor a rejection.",
                    )
                    return
                gate_results = [
                    item.model_dump(mode="json") for item in response.gate_results
                ]
                status = response.new_status.value
                if status == "LEASED":
                    if await self.fence(record):
                        await self.release_task(
                            task_id,
                            lease_hash,
                            "gate rejected the patch; task released for retry",
                        )
                await self._finalize(
                    record,
                    status="FAILED",
                    task_status=status,
                    gate_results=gate_results,
                    error_code="gate_rejected" if status == "LEASED" else "gate_failed",
                    error_detail="Gate rejected the patch.",
                )
                return

            verified = verification.verified
            async with session_scope() as session:
                current_status = await session.scalar(
                    text(
                        "SELECT status::text FROM dag_tasks WHERE task_id = :task_id"
                    ),
                    {"task_id": task_id},
                )
                await session.commit()
            if current_status == "CANCELLED":
                await self._finalize(
                    record,
                    status="FAILED",
                    task_status="CANCELLED",
                    error_code="task_cancelled",
                    error_detail=(
                        "Task was cancelled while the gate was running; "
                        "no merge intent was created."
                    ),
                )
                return
            intent = await self._upsert_merge_intent(
                record=record,
                task_id=task_id,
                workspace_id=workspace.workspace_id,
                lease_hash=lease_hash,
                verified=verified,
            )
            if intent is None:
                await self._finalize(
                    record,
                    status="FAILED",
                    task_status="VERIFYING",
                    error_code="merge_intent_error",
                    error_detail="Could not persist the merge intent.",
                )
                return

            result = await self._wait_merge_intent(
                record, intent["merge_id"], lost
            )
            if result is None:
                await self._finalize(
                    record,
                    status="FAILED",
                    task_status="VERIFYING",
                    error_code="merge_coordinator_timeout",
                    error_detail="The merge coordinator did not finalize the intent.",
                )
                return
            intent_status = str(result["status"])
            if intent_status == "REVALIDATE":
                if round_index >= 3:
                    await self._finalize(
                        record,
                        status="FAILED",
                        task_status="VERIFYING",
                        error_code="revalidation_exhausted",
                        error_detail="Main advanced through a verified dependency repeatedly.",
                    )
                    return
                continue
            if intent_status == "MERGED":
                await self._finalize(
                    record,
                    status="COMMITTED",
                    task_status="COMMITTED",
                    result_commit_sha=result.get("merged_commit_sha"),
                    merge_mode=result.get("merge_mode"),
                    gate_results=list(result.get("gate_results") or []),
                    merge_timing=dict(result.get("merge_timing") or {}),
                    unlocked_downstream_task_ids=[
                        str(value)
                        for value in (result.get("unlocked_downstream_task_ids") or [])
                    ],
                )
                return
            if intent_status in {"REJECTED", "CONFLICT"}:
                task_status = result.get("task_status") or "FAILED"
                if task_status == "LEASED" and await self.fence(record):
                    await self.release_task(
                        task_id, lease_hash, "merge coordinator rejected the patch"
                    )
                await self._finalize(
                    record,
                    status="FAILED",
                    task_status=task_status,
                    gate_results=list(result.get("gate_results") or []),
                    error_code=result.get("error_code") or "merge_rejected",
                    error_detail=result.get("error_detail"),
                )
                return
            # Coordinator moved the intent back to PENDING; wait once more.
            await asyncio.sleep(0.05)


    async def _reject_merge_intent(self, record: Mapping[str, Any], response) -> None:
        async with session_scope() as session:
            await session.execute(
                text(
                    """
                    UPDATE merge_intents
                    SET status = 'REJECTED',
                        task_status = :task_status,
                        gate_results = CAST(:gate_results AS JSONB),
                        error_code = :error_code,
                        error_detail = :error_detail,
                        completed_at = NOW(),
                        updated_at = NOW(),
                        coordinator_token = NULL,
                        coordinator_id = NULL
                    WHERE submission_id = :submission_id
                      AND status IN ('PENDING', 'REVALIDATE', 'RUNNING')
                    """
                ),
                {
                    "submission_id": record["submission_id"],
                    "task_status": response.new_status.value,
                    "gate_results": json.dumps(
                        [
                            item.model_dump(mode="json")
                            for item in response.gate_results
                        ],
                        default=str,
                    ),
                    "error_code": (
                        "gate_rejected"
                        if response.new_status.value == "LEASED"
                        else "gate_failed"
                    ),
                    "error_detail": "Gate rejected the patch.",
                },
            )
            await session.commit()

    # ----------------------------------------------------------- merge intents
    async def _upsert_merge_intent(
        self,
        *,
        record: Mapping[str, Any],
        task_id,
        workspace_id,
        lease_hash: str,
        verified,
    ) -> dict[str, Any] | None:
        gate_results = [
            item.model_dump(mode="json") for item in verified.gate_results
        ]
        async with session_scope() as session:
            row = (
                await session.execute(
                    text(
                        """
                        INSERT INTO merge_intents (
                            workspace_id, task_id, submission_id,
                            base_sha, task_commit_sha, lease_token_hash,
                            changed_files, gate_results, pool_wait_ms,
                            status
                        ) VALUES (
                            :workspace_id, :task_id, :submission_id,
                            :base_sha, :task_commit_sha, :lease_hash,
                            CAST(:changed_files AS TEXT[]),
                            CAST(:gate_results AS JSONB), :pool_wait_ms,
                            'PENDING'
                        )
                        ON CONFLICT (submission_id) DO UPDATE
                        SET base_sha = EXCLUDED.base_sha,
                            task_commit_sha = EXCLUDED.task_commit_sha,
                            lease_token_hash = EXCLUDED.lease_token_hash,
                            changed_files = EXCLUDED.changed_files,
                            gate_results = EXCLUDED.gate_results,
                            pool_wait_ms = EXCLUDED.pool_wait_ms,
                            status = 'PENDING',
                            coordinator_id = NULL,
                            coordinator_token = NULL,
                            merged_commit_sha = NULL,
                            task_status = NULL,
                            error_code = NULL,
                            error_detail = NULL,
                            completed_at = NULL,
                            updated_at = NOW()
                        WHERE merge_intents.status IN ('PENDING', 'REVALIDATE')
                        RETURNING *
                        """
                    ),
                    {
                        "workspace_id": workspace_id,
                        "task_id": task_id,
                        "submission_id": record["submission_id"],
                        "base_sha": verified.base_sha,
                        "task_commit_sha": verified.task_commit_sha,
                        "lease_hash": lease_hash,
                        "changed_files": verified.changed_files,
                        "gate_results": json.dumps(gate_results, default=str),
                        "pool_wait_ms": verified.pool_wait_ms,
                    },
                )
            ).mappings().first()
            if row is None:
                row = (
                    await session.execute(
                        text(
                            "SELECT * FROM merge_intents WHERE submission_id = :submission_id"
                        ),
                        {"submission_id": record["submission_id"]},
                    )
                ).mappings().first()
            await session.commit()
            return dict(row) if row else None

    async def _wait_merge_intent(
        self, record: Mapping[str, Any], merge_id, lost: asyncio.Event
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + 180.0
        while time.monotonic() < deadline and not lost.is_set():
            try:
                async with session_scope() as session:
                    row = (
                        await session.execute(
                            text(
                                "SELECT * FROM merge_intents WHERE merge_id = :merge_id"
                            ),
                            {"merge_id": merge_id},
                        )
                    ).mappings().first()
                    await session.commit()
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.25)
                continue
            if row is None:
                return None
            status = str(row["status"])
            if status in {"MERGED", "REJECTED", "CONFLICT", "REVALIDATE"}:
                return dict(row)
            try:
                if not await self.fence(record):
                    raise MergeFenceLost(
                        f"work lease lost while waiting for merge {merge_id}"
                    )
            except MergeFenceLost:
                raise
            except Exception:
                pass
            await asyncio.sleep(0.15)
        return None

    # --------------------------------------------------------------- finalize
    async def _finalize(
        self,
        record: Mapping[str, Any],
        *,
        status: str,
        task_status: str | None = None,
        result_commit_sha: str | None = None,
        merge_mode: str | None = None,
        gate_results: list[dict[str, Any]] | None = None,
        merge_timing: dict[str, Any] | None = None,
        unlocked_downstream_task_ids: list[Any] | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> bool:
        async with session_scope() as session:
            result = await session.execute(
                text(
                    """
                    UPDATE patch_submissions
                    SET status = :status,
                        task_status = :task_status,
                        result_commit_sha = :result_commit_sha,
                        merge_mode = :merge_mode,
                        gate_results = CAST(:gate_results AS JSONB),
                        merge_timing = CAST(:merge_timing AS JSONB),
                        unlocked_downstream_task_ids =
                            CAST(:unlocked_downstream_task_ids AS UUID[]),
                        error_code = :error_code,
                        error_detail = :error_detail,
                        work_lease_id = NULL,
                        work_lease_expires_at = NULL,
                        completed_at = COALESCE(completed_at, NOW()),
                        updated_at = NOW()
                    WHERE submission_id = :submission_id
                      AND status = 'RUNNING'
                      AND work_lease_id = :lease_id
                    RETURNING submission_id, workspace_id, task_id,
                              correlation
                    """
                ),
                {
                    "submission_id": record["submission_id"],
                    "lease_id": record["work_lease_id"],
                    "status": status,
                    "task_status": task_status,
                    "result_commit_sha": result_commit_sha,
                    "merge_mode": merge_mode,
                    "gate_results": json.dumps(gate_results or [], default=str),
                    "merge_timing": json.dumps(merge_timing or {}, default=str),
                    "unlocked_downstream_task_ids": [
                        str(value) for value in (unlocked_downstream_task_ids or [])
                    ],
                    "error_code": error_code,
                    "error_detail": (error_detail or "")[:8000] or None,
                },
            )
            finalized = result.mappings().first()
            await session.commit()
        if finalized is None:
            logger.warning(
                "fenced out: submission %s was already finalized/reclaimed",
                record["submission_id"],
            )
            return False
        get_runtime_metrics().inc(f"submission.{status.lower()}")
        if error_code:
            get_runtime_metrics().inc(f"submission.error.{error_code}")
        with contextlib.suppress(Exception):
            async with session_scope() as session:
                correlation = finalized.get("correlation") or {}
                if isinstance(correlation, str):
                    try:
                        import json as _json

                        correlation = _json.loads(correlation)
                    except ValueError:
                        correlation = {}
                await EventBus(session).publish(
                    finalized["workspace_id"],
                    "SUBMISSION_COMMITTED" if status == "COMMITTED" else "SUBMISSION_FAILED",
                    {
                        "submission_id": str(finalized["submission_id"]),
                        "task_id": str(finalized["task_id"]),
                        "status": status,
                        "task_status": task_status,
                        "error_code": error_code,
                        "error_detail": error_detail,
                        "result_commit_sha": result_commit_sha,
                        "merge_mode": merge_mode,
                        "gate_results": gate_results or [],
                        "correlation": correlation,
                    },
                    correlation_id=(correlation or {}).get("trace_id"),
                )
                await session.commit()
        return True

    async def _requeue(
        self, record: Mapping[str, Any], detail: str, code: str
    ) -> None:
        attempts = int(record.get("attempts") or 0)
        delay = min(60.0, float(2**min(attempts, 6)))
        async with session_scope() as session:
            await session.execute(
                text(
                    """
                    UPDATE patch_submissions
                    SET status = 'QUEUED',
                        available_at = NOW() + (:delay * INTERVAL '1 second'),
                        work_lease_id = NULL,
                        work_lease_expires_at = NULL,
                        last_worker_error = :detail,
                        updated_at = NOW()
                    WHERE submission_id = :submission_id
                      AND status = 'RUNNING'
                      AND work_lease_id = :lease_id
                    """
                ),
                {
                    "submission_id": record["submission_id"],
                    "lease_id": record["work_lease_id"],
                    "delay": delay,
                    "detail": (detail or "")[:8000],
                },
            )
            await session.commit()

    async def _handle_transient(
        self, record: Mapping[str, Any], code: str
    ) -> None:
        if int(record.get("attempts") or 0) < settings.work_max_attempts:
            await self._requeue(record, f"worker exception: {code}", code)
        else:
            await self._finalize(
                record,
                status="FAILED",
                error_code="work_retries_exhausted",
                error_detail=f"Worker retries exhausted after {code}.",
            )

    async def release_task(self, task_id, lease_hash: str, reason: str) -> bool:
        try:
            async with session_scope() as session:
                workspace_id = await session.scalar(
                    text("SELECT workspace_id FROM dag_tasks WHERE task_id = :task_id"),
                    {"task_id": task_id},
                )
                scheduler = DisjointWriteSetScheduler(session, EventBus(session))
                released = await scheduler.release_task(
                    task_id, lease_hash, reason, retry_counted=False
                )
                await session.commit()
                if released is not None and workspace_id is not None:
                    await scheduler_core_manager.sync_tasks(
                        session, workspace_id, [task_id]
                    )
                    await session.commit()
                return released is not None
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not release task %s: %s", task_id, exc)
            return False


__all__ = ["GateWorker", "WorkLeaseLost"]
