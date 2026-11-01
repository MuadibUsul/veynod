"""Durable gate/merge evidence for one DAG task.

Extracted from the admin route so the admin plane, the runner plane and the
Agent Runtime service identity all read exactly the same evidence shape.  A
consumer must never infer PASS from a task status: only these rows are proof.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DagTask


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _submission_payload(row: Any) -> dict | None:
    if row is None:
        return None
    return {
        "submission_id": str(row["submission_id"]),
        "status": row["status"],
        "task_status": row["task_status"],
        "result_commit_sha": row["result_commit_sha"],
        "merge_mode": row["merge_mode"],
        "gate_results": _json_value(row["gate_results"], []),
        "error_code": row["error_code"],
        "error_detail": row["error_detail"],
        "attempts": int(row["attempts"] or 0),
        "patch_sha256": row["patch_sha256"],
        "correlation": _json_value(row.get("correlation"), {}),
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def _intent_payload(row: Any) -> dict | None:
    if row is None:
        return None
    return {
        "merge_id": str(row["merge_id"]),
        "status": row["status"],
        "merged_commit_sha": row["merged_commit_sha"],
        "merge_mode": row["merge_mode"],
        "gate_results": _json_value(row["gate_results"], []),
        "merge_timing": _json_value(row["merge_timing"], {}),
        "task_status": row["task_status"],
        "error_code": row["error_code"],
        "error_detail": row["error_detail"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


async def build_task_evidence(
    session: AsyncSession, task_id: UUID
) -> dict[str, Any] | None:
    """Return the evidence for ``task_id`` or ``None`` when the task is gone."""
    task = await session.get(DagTask, task_id)
    if task is None:
        return None
    submission = (
        await session.execute(
            text(
                """
                SELECT submission_id, status, task_status, result_commit_sha,
                       merge_mode, gate_results, error_code, error_detail,
                       attempts, patch_sha256, correlation, created_at,
                       completed_at
                FROM patch_submissions
                WHERE task_id = :task_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"task_id": task_id},
        )
    ).mappings().first()
    intent = (
        await session.execute(
            text(
                """
                SELECT merge_id, status, merged_commit_sha, merge_mode,
                       gate_results, merge_timing, task_status, error_code,
                       error_detail, created_at, completed_at
                FROM merge_intents
                WHERE task_id = :task_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"task_id": task_id},
        )
    ).mappings().first()

    delivery = (
        await session.execute(
            text(
                """
                SELECT delivery_id, commit_sha, merge_mode, evidence_hash,
                       snapshot_id, created_at
                FROM delivery_records WHERE task_id = :task_id
                """
            ),
            {"task_id": task_id},
        )
    ).mappings().first()
    submission_payload = _submission_payload(submission)
    intent_payload = _intent_payload(intent)
    gate_results = (
        (intent_payload or {}).get("gate_results")
        or (submission_payload or {}).get("gate_results")
        or []
    )
    return {
        "task_id": str(task.task_id),
        "workspace_id": str(task.workspace_id),
        "task_status": task.status.value,
        "result_commit_sha": task.result_commit_sha,
        "submission": submission_payload,
        "merge_intent": intent_payload,
        "delivery": dict(delivery) if delivery is not None else None,
        "gate_results": gate_results,
        "evidence_complete": bool(
            submission_payload
            and submission_payload["status"] == "COMMITTED"
            and intent_payload
            and intent_payload["status"] == "MERGED"
            and gate_results
        ),
    }


__all__ = ["build_task_evidence"]
