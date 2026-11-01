"""O(1) disjoint write-set scheduler and Kahn topological unblocker."""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.engine.hold_queue import GLOBAL_CLAIM_HOLD_QUEUE
from app.engine.scheduler_core import scheduler_core_manager
from app.services.event_bus import EventBus
from app.services.runtime_metrics import get_runtime_metrics

logger = logging.getLogger(__name__)


class AdmissionDenied(RuntimeError):
    """The per-repository gate/merge queues are near capacity."""

    def __init__(self, retry_after: int, reason: str, queue_depth: dict[str, int]):
        super().__init__(reason)
        self.retry_after = max(1, int(retry_after))
        self.reason = reason
        self.queue_depth = queue_depth



class DisjointWriteSetScheduler:
    """
    The Rust ``SchedulerCore`` holds the authoritative task graph and active
    lock indexes behind one Mutex. In single-kernel mode (ADR-03) its 5µs
    decision already serializes the 100 concurrent claims and returns pairwise
    orthogonal task ids, so a per-workspace claim advisory lock would only
    serialize independent row updates. ``VN_SINGLE_KERNEL_CLAIM=false`` restores
    the cross-process serialization for multi-process deployments.
    """

    def __init__(self, session: AsyncSession, event_bus: EventBus) -> None:
        self.session = session
        self.event_bus = event_bus

    async def claim_next_orthogonal_task(
        self,
        workspace_id: UUID,
        runner_id: str,
        supported_task_types: list[str],
        lease_token_hash: str,
        lease_seconds: int = 600,
        wait_seconds: float | None = None,
        requested_task_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        """Claim a task; optionally hold the request in memory without a DB connection."""
        effective_wait = (
            0.0
            if wait_seconds is None
            else min(float(wait_seconds), settings.claim_max_wait_seconds)
        )
        deadline = time.monotonic() + max(0.0, effective_wait)
        last_denied: AdmissionDenied | None = None
        metrics = get_runtime_metrics()

        while True:
            reserve_started = time.perf_counter()
            try:
                permit_id = await self._reserve_admission(
                    workspace_id, lease_seconds
                )
            except AdmissionDenied as denied:
                last_denied = denied
                metrics.observe(
                    "claim.reserve_ms",
                    (time.perf_counter() - reserve_started) * 1000,
                )
                permit_id = None
            else:
                metrics.observe(
                    "claim.reserve_ms",
                    (time.perf_counter() - reserve_started) * 1000,
                )
                claim_started = time.perf_counter()
                try:
                    task = await self._claim_after_admission(
                        workspace_id=workspace_id,
                        runner_id=runner_id,
                        supported_task_types=supported_task_types,
                        lease_token_hash=lease_token_hash,
                        lease_seconds=lease_seconds,
                        permit_id=permit_id,
                        requested_task_id=requested_task_id,
                    )
                except BaseException:
                    await self._release_permit(permit_id)
                    raise
                metrics.observe(
                    "claim.core_claim_ms",
                    (time.perf_counter() - claim_started) * 1000,
                )
                if task is not None:
                    return task
                await self._release_permit(permit_id)

            remaining = deadline - time.monotonic()
            if remaining <= 0.1:
                if last_denied is not None:
                    raise last_denied
                return None

            # Critical: return the pooled connection before parking. The next
            # iteration checks out a fresh connection and re-runs the fence.
            await self.session.close()
            slice_wait = min(remaining, 3.0 + random.uniform(0.0, 1.5))
            await GLOBAL_CLAIM_HOLD_QUEUE.wait_for_opportunity(
                workspace_id, timeout=slice_wait
            )

    async def _reserve_admission(
        self, workspace_id: UUID, lease_seconds: int
    ) -> UUID:
        """Reserve one pipeline slot before the heavy claim decision.

        The reservation is committed under a short per-workspace advisory lock.
        This prevents an initial claim burst from all observing an empty queue
        while keeping the lock off the (much slower) bundle/claim path. The
        permit is released by the terminal-status trigger or by TTL.
        """
        # Cheap fast path for the common 1,000-puller case: when permits
        # already fill the gate budget, deny without probing 32 advisory locks
        # or running the eight-scan depth CTE. The authoritative depth check
        # still runs below for the accepted path.
        permit_count = await self.session.scalar(
            text(
                "SELECT count(*) FROM admission_permits "
                "WHERE workspace_id = :workspace_id AND expires_at > NOW()"
            ),
            {"workspace_id": workspace_id},
        )
        if int(permit_count or 0) >= settings.admission_gate_queue_limit:
            depth_row = (
                await self.session.execute(
                    text(
                        """
                        SELECT
                            (SELECT count(*) FROM dag_tasks
                             WHERE workspace_id = :workspace_id
                               AND status = 'LEASED'
                               AND lease_expires_at > NOW()) AS leased,
                            (SELECT count(*) FROM dag_tasks
                             WHERE workspace_id = :workspace_id
                               AND status = 'VERIFYING') AS verifying,
                            (SELECT count(*) FROM patch_submissions
                             WHERE workspace_id = :workspace_id
                               AND status IN ('QUEUED', 'RUNNING')) AS submissions,
                            (SELECT count(*) FROM merge_intents
                             WHERE workspace_id = :workspace_id
                               AND status IN ('PENDING', 'RUNNING')) AS merge_queue
                        """
                    ),
                    {"workspace_id": workspace_id},
                )
            ).mappings().first()
            await self.session.rollback()
            raise AdmissionDenied(
                settings.admission_retry_after_seconds,
                "admission paused: permits at gate budget",
                {
                    "gate_queue": int(permit_count or 0),
                    "leased": int(depth_row["leased"] if depth_row else 0),
                    "submissions": int(depth_row["submissions"] if depth_row else 0),
                    "merge_queue": int(depth_row["merge_queue"] if depth_row else 0),
                    "verifying": int(depth_row["verifying"] if depth_row else 0),
                    "permits": int(permit_count or 0),
                },
            )

        # Fail fast instead of queueing on the lock: a blocked reservation
        # would hold a DB connection while the event loop serves 1000 pullers,
        # exhausting the pool and starving submit/bundle work. Multiple slots
        # keep a handful of reservations in flight; if all are busy the caller
        # gets a 429 with a retry hint instead of a lock convoy.
        acquired = False
        for slot in range(settings.admission_claim_slots):
            acquired = (
                await self.session.execute(
                    text(
                        "SELECT pg_try_advisory_xact_lock("
                        "hashtextextended(:key, 0))"
                    ),
                    {"key": f"veynod:admission:{workspace_id}:{slot}"},
                )
            ).scalar_one()
            if acquired:
                break
        if not acquired:
            await self.session.rollback()
            raise AdmissionDenied(
                1,
                "admission reservation busy",
                {
                    "gate_queue": 0,
                    "leased": 0,
                    "submissions": 0,
                    "merge_queue": 0,
                    "verifying": 0,
                    "permits": 0,
                },
            )
        await self.session.execute(
            text(
                """
                DELETE FROM admission_permits
                WHERE workspace_id = :workspace_id
                  AND (
                        expires_at < NOW()
                     OR (task_id IS NULL
                         AND created_at < NOW() - INTERVAL '2 minutes')
                  )
                """
            ),
            {"workspace_id": workspace_id},
        )
        depth = await self._admission_depth(workspace_id)
        permit_row = (
            await self.session.execute(
                text(
                    """
                    SELECT count(*) AS permits
                    FROM admission_permits
                    WHERE workspace_id = :workspace_id
                    """
                ),
                {"workspace_id": workspace_id},
            )
        ).mappings().first()
        permits = int(permit_row["permits"] if permit_row else 0)
        # ``pipeline`` covers tasks whose permit already expired but which are
        # still LEASED/VERIFYING/queued; permits cover claims not yet persisted.
        pressure = max(depth["pipeline"], permits)
        if (
            pressure >= settings.admission_gate_queue_limit
            or depth["merge_queue"] >= settings.admission_merge_queue_limit
        ):
            await self.session.rollback()
            raise AdmissionDenied(
                settings.admission_retry_after_seconds,
                (
                    f"admission paused: gate_depth={pressure} "
                    f"(limit {settings.admission_gate_queue_limit}), "
                    f"merge_depth={depth['merge_queue']} "
                    f"(limit {settings.admission_merge_queue_limit})"
                ),
                {
                    "gate_queue": pressure,
                    "leased": depth["leased"],
                    "submissions": depth["gate_submissions"],
                    "merge_queue": depth["merge_queue"],
                    "verifying": depth["verifying"],
                    "permits": permits,
                },
            )
        permit_id = (
            await self.session.execute(
                text(
                    """
                    INSERT INTO admission_permits (workspace_id, expires_at)
                    VALUES (
                        :workspace_id,
                        NOW() + ((:lease_seconds + 300) * INTERVAL '1 second')
                    )
                    RETURNING permit_id
                    """
                ),
                {"workspace_id": workspace_id, "lease_seconds": lease_seconds},
            )
        ).scalar_one()
        await self.session.commit()
        return permit_id

    async def _bind_permit_tx(
        self, permit_id: UUID, task_id: str, lease_seconds: int
    ) -> None:
        """Bind the permit inside the caller's claim transaction.

        The permit UPDATE rides the same commit as the task lease, removing a
        second WAL fsync from the claim critical path.
        """
        await self.session.execute(
            text(
                """
                UPDATE admission_permits
                SET task_id = :task_id,
                    expires_at = NOW() + ((:lease_seconds + 300) * INTERVAL '1 second')
                WHERE permit_id = :permit_id
                """
            ),
            {
                "permit_id": permit_id,
                "task_id": task_id,
                "lease_seconds": lease_seconds,
            },
        )

    async def _release_permit(self, permit_id: UUID) -> None:
        try:
            await self.session.rollback()
            await self.session.execute(
                text("DELETE FROM admission_permits WHERE permit_id = :permit_id"),
                {"permit_id": permit_id},
            )
            await self.session.commit()
        except Exception:  # noqa: BLE001
            await self.session.rollback()
            logger.warning("could not release admission permit %s", permit_id)

    async def _claim_after_admission(
        self,
        workspace_id: UUID,
        runner_id: str,
        supported_task_types: list[str],
        lease_token_hash: str,
        lease_seconds: int = 600,
        permit_id: UUID | None = None,
        requested_task_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        core = None

        # A targeted claim (the Runtime leases the node it created) goes
        # straight to the SQL fence: the in-memory core decides "any" task,
        # not a specific one.
        if settings.single_kernel_claim and requested_task_id is None:
            if settings.multi_core_claim:
                ensure_started = time.perf_counter()
                core = await scheduler_core_manager.ensure_loaded(
                    self.session, workspace_id
                )
                get_runtime_metrics().observe(
                    "claim.ensure_core_ms",
                    (time.perf_counter() - ensure_started) * 1000,
                )
            else:
                authority_started = time.perf_counter()
                has_authority = await scheduler_core_manager.acquire_authority(
                    self.session, workspace_id
                )
                get_runtime_metrics().observe(
                    "claim.authority_ms",
                    (time.perf_counter() - authority_started) * 1000,
                )
                if has_authority:
                    ensure_started = time.perf_counter()
                    core = await scheduler_core_manager.ensure_loaded(
                        self.session, workspace_id
                    )
                    get_runtime_metrics().observe(
                        "claim.ensure_core_ms",
                        (time.perf_counter() - ensure_started) * 1000,
                    )
        if core is not None:
            # Pure in-memory decision after the admission check. No DB round
            # trip when the queue is empty or all candidates conflict; the
            # per-task conditional UPDATE remains the durable fence.
            decision_started = time.perf_counter()
            task_id = scheduler_core_manager.claim(
                workspace_id,
                supported_task_types,
                runner_id,
                lease_token_hash,
                0,
            )
            get_runtime_metrics().observe(
                "claim.rust_decision_ms",
                (time.perf_counter() - decision_started) * 1000,
            )
            if task_id is None:
                await self.session.commit()
                return None
            if not settings.single_kernel_claim:
                await self.session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"veynod:claim:{workspace_id}"},
                )
            return await self._claim_with_rust_core(
                workspace_id=workspace_id,
                runner_id=runner_id,
                supported_task_types=supported_task_types,
                lease_token_hash=lease_token_hash,
                lease_seconds=lease_seconds,
                initial_task_id=task_id,
                permit_id=permit_id,
            )

        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"veynod:claim:{workspace_id}"},
        )
        row = await self._claim_with_sql(
            workspace_id=workspace_id,
            runner_id=runner_id,
            supported_task_types=supported_task_types,
            lease_token_hash=lease_token_hash,
            lease_seconds=lease_seconds,
            requested_task_id=requested_task_id,
        )
        if row is not None and permit_id is not None:
            await self._bind_permit_tx(permit_id, str(row["task_id"]), lease_seconds)
        await self.session.commit()
        return row

    async def _claim_with_rust_core(
        self,
        workspace_id: UUID,
        runner_id: str,
        supported_task_types: list[str],
        lease_token_hash: str,
        lease_seconds: int,
        initial_task_id: str | None = None,
        permit_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        """Rust decides; PostgreSQL validates one row and persists the lease.

        In the default single-kernel mode the Rust Mutex is the serialization
        point and the conditional UPDATE is the ACID fence; no advisory lock is
        taken. If the DB fence rejects the chosen task (stale core snapshot,
        cross-process claim), the core is reloaded and a fresh decision is made.
        """
        for attempt in range(3):
            if attempt == 0 and initial_task_id is not None:
                task_id = initial_task_id
            else:
                # Retries after a stale-core/fence rejection use the DB
                # transaction clock; the initial pure-memory claim does not
                # need this extra round trip.
                claim_version = await self.session.scalar(
                    text("SELECT (EXTRACT(EPOCH FROM NOW()) * 1000000)::bigint")
                )
                task_id = scheduler_core_manager.claim(
                    workspace_id,
                    supported_task_types,
                    runner_id,
                    lease_token_hash,
                    int(claim_version or 0),
                )
            if task_id is None:
                await self.session.commit()
                return None

            try:
                update_started = time.perf_counter()
                row = await self._update_selected_task(
                    UUID(task_id),
                    workspace_id,
                    runner_id,
                    lease_token_hash,
                    lease_seconds,
                )
                get_runtime_metrics().observe(
                    "claim.update_field_ms",
                    (time.perf_counter() - update_started) * 1000,
                )
                if row is not None:
                    if permit_id is not None:
                        await self._bind_permit_tx(
                            permit_id, str(row["task_id"]), lease_seconds
                        )
                    commit_started = time.perf_counter()
                    await self.session.commit()
                    get_runtime_metrics().observe(
                        "claim.commit_ms",
                        (time.perf_counter() - commit_started) * 1000,
                    )
                    return row

                scheduler_core_manager.revert(
                    workspace_id, UUID(task_id), lease_token_hash
                )
                await scheduler_core_manager.reload_workspace(
                    self.session, workspace_id
                )
            except Exception:
                scheduler_core_manager.revert(
                    workspace_id, UUID(task_id), lease_token_hash
                )
                await self.session.rollback()
                raise

        logger.warning(
            "Rust scheduler could not persist a claim after 3 attempts (workspace %s).",
            workspace_id,
        )
        await self.session.rollback()
        return None

    async def _admission_depth(self, workspace_id: UUID) -> dict[str, int]:
        row = (
            await self.session.execute(
                text(
                    """
                    WITH pipeline AS (
                        SELECT task_id FROM dag_tasks
                        WHERE workspace_id = :workspace_id
                          AND status = 'LEASED'
                          AND lease_expires_at > NOW()
                        UNION
                        SELECT task_id FROM dag_tasks
                        WHERE workspace_id = :workspace_id
                          AND status = 'VERIFYING'
                        UNION
                        SELECT task_id FROM patch_submissions
                        WHERE workspace_id = :workspace_id
                          AND status IN ('QUEUED', 'RUNNING')
                        UNION
                        SELECT task_id FROM merge_intents
                        WHERE workspace_id = :workspace_id
                          AND status IN ('PENDING', 'RUNNING')
                    )
                    SELECT
                        (SELECT count(*) FROM dag_tasks
                         WHERE workspace_id = :workspace_id
                           AND status = 'LEASED'
                           AND lease_expires_at > NOW()) AS leased,
                        (SELECT count(*) FROM patch_submissions
                         WHERE workspace_id = :workspace_id
                           AND status IN ('QUEUED', 'RUNNING')) AS gate_submissions,
                        (SELECT count(*) FROM merge_intents
                         WHERE workspace_id = :workspace_id
                           AND status IN ('PENDING', 'RUNNING')) AS merge_queue,
                        (SELECT count(*) FROM dag_tasks
                         WHERE workspace_id = :workspace_id
                           AND status = 'VERIFYING') AS verifying,
                        (SELECT count(*) FROM pipeline) AS pipeline
                    """
                ),
                {"workspace_id": workspace_id},
            )
        ).mappings().first()
        if row is None:
            return {
                "leased": 0,
                "gate_submissions": 0,
                "merge_queue": 0,
                "verifying": 0,
                "pipeline": 0,
            }
        return {
            "leased": int(row["leased"]),
            "gate_submissions": int(row["gate_submissions"]),
            "merge_queue": int(row["merge_queue"]),
            "verifying": int(row["verifying"]),
            "pipeline": int(row["pipeline"]),
        }

    async def _update_selected_task(
        self,
        task_id: UUID,
        workspace_id: UUID,
        runner_id: str,
        lease_token_hash: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        result = await self.session.execute(
            text(
                """
                UPDATE dag_tasks AS target
                SET status = 'LEASED',
                    leased_by_runner = :runner_id,
                    lease_token_hash = :lease_token_hash,
                    lease_expires_at = NOW() + (:lease_seconds * INTERVAL '1 second'),
                    base_commit_sha = (
                        SELECT head_commit_sha FROM workspaces
                        WHERE workspace_id = :workspace_id
                    ),
                    updated_at = NOW()
                WHERE target.task_id = :task_id
                  AND target.workspace_id = :workspace_id
                  AND target.status = 'READY'
                  AND target.lease_token_hash IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM dag_tasks AS active
                      WHERE active.workspace_id = :workspace_id
                        AND active.task_id <> target.task_id
                        AND (
                              (active.status = 'LEASED'
                               AND active.lease_expires_at > NOW())
                           OR active.status = 'VERIFYING'
                        )
                        AND (
                              target.write_locks && active.write_locks
                           OR target.write_locks && active.ancestor_locks
                           OR target.ancestor_locks && active.write_locks
                        )
                  )
                RETURNING
                    target.task_id, target.title, target.spec_prompt, target.type,
                    target.read_artifact_ids, target.target_artifact_ids,
                    target.test_artifact_ids, target.write_locks,
                    target.ancestor_locks, target.base_commit_sha,
                    target.last_error_traceback, target.lease_expires_at,
                    target.priority
                """
            ),
            {
                "task_id": task_id,
                "workspace_id": workspace_id,
                "runner_id": runner_id,
                "lease_token_hash": lease_token_hash,
                "lease_seconds": lease_seconds,
            },
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def _claim_with_sql(
        self,
        workspace_id: UUID,
        runner_id: str,
        supported_task_types: list[str],
        lease_token_hash: str,
        lease_seconds: int,
        requested_task_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        """Fallback when the optional Rust extension is unavailable."""
        sql = text(
            """
            WITH active AS (
                SELECT write_locks, ancestor_locks
                FROM dag_tasks
                WHERE workspace_id = :workspace_id
                  AND (
                        (status = 'LEASED' AND lease_expires_at > NOW())
                     OR status = 'VERIFYING'
                  )
            ),
            active_w AS (SELECT unnest(write_locks) AS lock FROM active),
            active_a AS (SELECT unnest(ancestor_locks) AS lock FROM active),
            locks AS (
                SELECT
                    COALESCE((SELECT array_agg(DISTINCT lock) FROM active_w), '{}'::TEXT[]) AS w,
                    COALESCE((SELECT array_agg(DISTINCT lock) FROM active_a), '{}'::TEXT[]) AS a
            ),
            candidate AS (
                SELECT t.task_id
                FROM dag_tasks t
                CROSS JOIN locks l
                WHERE t.workspace_id = :workspace_id
                  AND t.status = 'READY'
                  AND (
                        CAST(:requested_task_id AS UUID) IS NULL
                     OR t.task_id = CAST(:requested_task_id AS UUID)
                  )
                  AND t.type::TEXT = ANY(CAST(:supported_types AS TEXT[]))
                  AND NOT (t.write_locks && l.w)
                  AND NOT (t.write_locks && l.a)
                  AND NOT (t.ancestor_locks && l.w)
                ORDER BY t.priority DESC, t.created_at ASC
                FOR UPDATE OF t SKIP LOCKED
                LIMIT 1
            )
            UPDATE dag_tasks AS target
            SET status = 'LEASED',
                leased_by_runner = :runner_id,
                lease_token_hash = :lease_token_hash,
                lease_expires_at = NOW() + (:lease_seconds * INTERVAL '1 second'),
                base_commit_sha = (
                    SELECT head_commit_sha FROM workspaces WHERE workspace_id = :workspace_id
                ),
                updated_at = NOW()
            FROM candidate
            WHERE target.task_id = candidate.task_id
            RETURNING
                target.task_id, target.title, target.spec_prompt, target.type,
                target.read_artifact_ids, target.target_artifact_ids,
                target.test_artifact_ids, target.write_locks, target.ancestor_locks,
                target.base_commit_sha, target.last_error_traceback,
                target.lease_expires_at, target.priority
            """
        )
        result = await self.session.execute(
            sql,
            {
                "workspace_id": workspace_id,
                "runner_id": runner_id,
                "supported_types": supported_task_types,
                "lease_token_hash": lease_token_hash,
                "lease_seconds": lease_seconds,
                "requested_task_id": (
                    str(requested_task_id) if requested_task_id else None
                ),
            },
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def on_task_committed(
        self,
        workspace_id: UUID,
        completed_task_id: UUID,
        new_head_sha: str,
    ) -> list[UUID]:
        """
        Advance workspace HEAD, close the task and decrement the in-degree of
        every direct downstream task. Returns the ids that became READY.

        The caller owns the transaction (the gate holds the merge advisory lock
        across the git ref update and this statement).
        """
        unlock_sql = text(
            """
            WITH mark_done AS (
                UPDATE dag_tasks
                SET status = 'COMMITTED',
                    result_commit_sha = :new_head_sha,
                    lease_token_hash = NULL,
                    leased_by_runner = NULL,
                    lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE task_id = :completed_task_id
                  AND status = 'VERIFYING'
                RETURNING task_id
            ),
            updated_ws AS (
                UPDATE workspaces
                SET head_commit_sha = :new_head_sha
                WHERE workspace_id = :workspace_id
                  AND EXISTS (SELECT 1 FROM mark_done)
                RETURNING workspace_id
            ),
            decremented AS (
                UPDATE dag_tasks AS downstream
                SET in_degree = GREATEST(0, downstream.in_degree - 1),
                    status = CASE
                        WHEN downstream.in_degree - 1 <= 0
                         AND downstream.status IN ('BLOCKED', 'SUSPENDED_ESCALATED')
                        THEN 'READY'::task_status
                        ELSE downstream.status
                    END,
                    updated_at = NOW()
                FROM task_dependencies dep
                JOIN mark_done md ON md.task_id = dep.upstream_task_id
                WHERE dep.downstream_task_id = downstream.task_id
                RETURNING downstream.task_id, downstream.status
            )
            SELECT task_id FROM decremented WHERE status = 'READY'
            """
        )
        result = await self.session.execute(
            unlock_sql,
            {
                "workspace_id": workspace_id,
                "completed_task_id": completed_task_id,
                "new_head_sha": new_head_sha,
            },
        )
        return [row[0] for row in result.fetchall()]

    async def reap_expired_leases(self, workspace_id: UUID) -> list[dict[str, Any]]:
        sql = text(
            """
            UPDATE dag_tasks
            SET status = CASE
                    WHEN retry_count + 1 >= max_retries THEN 'FAILED'::task_status
                    ELSE 'READY'::task_status
                END,
                retry_count = retry_count + 1,
                leased_by_runner = NULL,
                lease_token_hash = NULL,
                lease_expires_at = NULL,
                last_error_traceback = 'Lease expired (Runner heartbeat timeout)',
                updated_at = NOW()
            WHERE workspace_id = :workspace_id
              AND status = 'LEASED'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= NOW()
            RETURNING task_id, status, leased_by_runner
            """
        )
        result = await self.session.execute(sql, {"workspace_id": workspace_id})
        return [dict(row) for row in result.mappings().all()]

    async def reap_stuck_verifications(
        self, workspace_id: UUID, older_than_seconds: int
    ) -> list[dict[str, Any]]:
        sql = text(
            """
            UPDATE dag_tasks
            SET status = CASE
                    WHEN retry_count + 1 >= max_retries THEN 'FAILED'::task_status
                    ELSE 'READY'::task_status
                END,
                retry_count = retry_count + 1,
                leased_by_runner = NULL,
                lease_token_hash = NULL,
                lease_expires_at = NULL,
                last_error_traceback = 'Verification timed out (kernel restarted or gate hung)',
                updated_at = NOW()
            WHERE workspace_id = :workspace_id
              AND status = 'VERIFYING'
              AND updated_at <= NOW() - (:older_than_seconds * INTERVAL '1 second')
            RETURNING task_id, status
            """
        )
        result = await self.session.execute(
            sql,
            {
                "workspace_id": workspace_id,
                "older_than_seconds": older_than_seconds,
            },
        )
        return [dict(row) for row in result.mappings().all()]

    async def extend_lease(
        self, task_id: UUID, lease_token_hash: str, extend_seconds: int
    ) -> datetime | None:
        result = await self.session.execute(
            text(
                """
                UPDATE dag_tasks
                SET lease_expires_at = NOW() + (:extend_seconds * INTERVAL '1 second'),
                    updated_at = NOW()
                WHERE task_id = :task_id
                  AND status = 'LEASED'
                  AND lease_token_hash = :lease_token_hash
                RETURNING lease_expires_at
                """
            ),
            {
                "task_id": task_id,
                "lease_token_hash": lease_token_hash,
                "extend_seconds": extend_seconds,
            },
        )
        row = result.first()
        return row[0] if row else None

    async def release_task(
        self,
        task_id: UUID,
        lease_token_hash: str,
        reason: str,
        *,
        retry_counted: bool = False,
    ) -> dict[str, Any] | None:
        result = await self.session.execute(
            text(
                """
                UPDATE dag_tasks
                SET status = CASE
                        WHEN CAST(:retry_counted AS BOOLEAN)
                         AND retry_count + 1 >= max_retries THEN 'FAILED'::task_status
                        ELSE 'READY'::task_status
                    END,
                    retry_count = retry_count + CASE WHEN CAST(:retry_counted AS BOOLEAN) THEN 1 ELSE 0 END,
                    leased_by_runner = NULL,
                    lease_token_hash = NULL,
                    lease_expires_at = NULL,
                    last_error_traceback = :reason,
                    updated_at = NOW()
                WHERE task_id = :task_id
                  AND status = 'LEASED'
                  AND lease_token_hash = :lease_token_hash
                RETURNING task_id, status, retry_count
                """
            ),
            {
                "task_id": task_id,
                "lease_token_hash": lease_token_hash,
                "reason": reason[:4000],
                "retry_counted": retry_counted,
            },
        )
        row = result.mappings().first()
        return dict(row) if row else None


__all__ = ["DisjointWriteSetScheduler"]
