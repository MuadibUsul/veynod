"""Minimal task command layer. Phase 1B starts with cancellation.

Other transitions still live in their current services; this module is the
single entry point for cancellation and is the place to grow the remaining
commands.  A cancelled task is terminal: it is never retried, verified or
merged.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.scheduler_core import scheduler_core_manager
from app.services.event_bus import EventBus

logger = logging.getLogger("veynod.task_commands")

_TERMINAL = frozenset({"COMMITTED", "FAILED", "CANCELLED", "SPLIT_ARCHIVED"})


class TaskCommandError(RuntimeError):
    pass


class TaskNotFound(TaskCommandError):
    pass


class TaskCancelConflict(TaskCommandError):
    pass


async def _cancel_side_effects(
    session: AsyncSession, task_ids: list[UUID], reason: str
) -> None:
    if not task_ids:
        return
    await session.execute(
        text(
            """
            UPDATE patch_submissions
            SET status = 'FAILED',
                task_status = 'CANCELLED',
                error_code = 'cancelled',
                error_detail = :reason,
                work_lease_id = NULL,
                work_lease_expires_at = NULL,
                completed_at = COALESCE(completed_at, NOW()),
                updated_at = NOW()
            WHERE task_id = ANY(CAST(:task_ids AS UUID[]))
              AND status IN ('QUEUED', 'RUNNING')
            """
        ),
        {"task_ids": task_ids, "reason": reason[:8000]},
    )
    await session.execute(
        text(
            """
            UPDATE merge_intents
            SET status = 'REJECTED',
                task_status = 'CANCELLED',
                error_code = 'cancelled',
                error_detail = :reason,
                coordinator_token = NULL,
                coordinator_id = NULL,
                completed_at = COALESCE(completed_at, NOW()),
                updated_at = NOW()
            WHERE task_id = ANY(CAST(:task_ids AS UUID[]))
              AND status IN ('PENDING', 'RUNNING', 'REVALIDATE')
            """
        ),
        {"task_ids": task_ids, "reason": reason[:8000]},
    )


async def cancel_task(
    session: AsyncSession,
    task_id: UUID,
    *,
    reason: str,
    actor: str = "admin",
) -> dict[str, Any]:
    """Cancel a task and every downstream task that can no longer complete.

    The root row is locked first; the recursive update covers the dependency
    subtree.  Open submissions and merge intents are durably failed/rejected so
    a late gate result cannot resurrect the work.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a cancel reason is required")

    row = (
        await session.execute(
            text(
                """
                SELECT task_id, workspace_id, status::text AS status, title
                FROM dag_tasks
                WHERE task_id = :task_id
                FOR UPDATE
                """
            ),
            {"task_id": task_id},
        )
    ).mappings().first()
    if row is None:
        await session.rollback()
        raise TaskNotFound(f"Task {task_id} not found")

    workspace_id = row["workspace_id"]
    status = str(row["status"])
    if status == "CANCELLED":
        await _cancel_side_effects(session, [task_id], reason)
        await session.commit()
        return {
            "task_id": str(task_id),
            "workspace_id": str(workspace_id),
            "status": "CANCELLED",
            "cancelled_task_ids": [str(task_id)],
            "idempotent": True,
        }
    if status in _TERMINAL:
        await session.rollback()
        raise TaskCancelConflict(
            f"Task {task_id} is {status} and cannot be cancelled"
        )

    result = await session.execute(
        text(
            """
            WITH RECURSIVE descendants AS (
                SELECT task_id
                FROM dag_tasks
                WHERE task_id = :task_id
                UNION
                SELECT dep.downstream_task_id
                FROM task_dependencies dep
                JOIN descendants d ON dep.upstream_task_id = d.task_id
            )
            UPDATE dag_tasks AS target
            SET status = 'CANCELLED'::task_status,
                leased_by_runner = NULL,
                lease_token_hash = NULL,
                lease_expires_at = NULL,
                cancelled_at = COALESCE(target.cancelled_at, NOW()),
                cancel_reason = :reason,
                updated_at = NOW()
            WHERE target.task_id IN (SELECT task_id FROM descendants)
              AND target.status NOT IN (
                  'COMMITTED', 'FAILED', 'CANCELLED', 'SPLIT_ARCHIVED'
              )
            RETURNING target.task_id
            """
        ),
        {"task_id": task_id, "reason": reason[:8000]},
    )
    cancelled_ids = [row[0] for row in result.fetchall()]
    if not cancelled_ids:
        await session.rollback()
        raise TaskCancelConflict(f"Task {task_id} could not be cancelled")

    await _cancel_side_effects(session, cancelled_ids, reason)
    await EventBus(session).publish(
        workspace_id,
        "TASK_CANCELLED",
        {
            "task_id": str(task_id),
            "cancelled_task_ids": [str(value) for value in cancelled_ids],
            "reason": reason[:8000],
            "actor": actor,
        },
    )
    await session.commit()

    try:
        await scheduler_core_manager.sync_tasks(
            session, workspace_id, cancelled_ids
        )
        await session.commit()
    except Exception:  # noqa: BLE001 - DB is authoritative; core reloads
        await session.rollback()
        logger.exception("scheduler core sync after cancel failed")

    return {
        "task_id": str(task_id),
        "workspace_id": str(workspace_id),
        "status": "CANCELLED",
        "cancelled_task_ids": [str(value) for value in cancelled_ids],
        "idempotent": False,
    }


__all__ = [
    "TaskCancelConflict",
    "TaskCommandError",
    "TaskNotFound",
    "cancel_task",
]
