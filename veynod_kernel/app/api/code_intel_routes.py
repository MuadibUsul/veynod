"""Snapshot, conflict and delivery read APIs (Phases 5/6)."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_routes import require_admin
from app.database import get_db_session

router = APIRouter(
    prefix="/v1/admin",
    tags=["Code Intelligence"],
    dependencies=[Depends(require_admin)],
)


@router.get("/tasks/{task_id}/snapshot")
async def task_snapshot(
    task_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    row = (
        await session.execute(
            text(
                """
                SELECT d.task_id, d.snapshot_id, s.base_sha, s.graph_version,
                       s.manifest, s.manifest_hash, s.created_at
                FROM dag_tasks d
                JOIN task_snapshots s ON s.snapshot_id = d.snapshot_id
                WHERE d.task_id = :task_id
                """
            ),
            {"task_id": str(task_id)},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="snapshot not found")
    return dict(row)


@router.get("/tasks/{task_id}/delivery")
async def task_delivery(
    task_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    row = (
        await session.execute(
            text(
                """
                SELECT delivery_id, workspace_id, task_id, submission_id,
                       merge_id, commit_sha, base_sha, merge_mode, snapshot_id,
                       gate_results, evidence_hash, created_at
                FROM delivery_records
                WHERE task_id = :task_id
                """
            ),
            {"task_id": str(task_id)},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="delivery record not found")
    return dict(row)


@router.get("/merge-conflicts")
async def list_conflicts(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    workspace_id: UUID | None = Query(default=None),
    status: str | None = Query(default="OPEN"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    clauses = []
    params: dict[str, Any] = {"limit": limit}
    if workspace_id is not None:
        clauses.append("workspace_id = :workspace_id")
        params["workspace_id"] = str(workspace_id)
    if status:
        clauses.append("status = :status")
        params["status"] = status.upper()
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = (
        await session.execute(
            text(
                f"""
                SELECT conflict_id, workspace_id, task_id, submission_id,
                       merge_id, kind, paths, detail, status, created_at,
                       resolved_at
                FROM merge_conflicts {where}
                ORDER BY created_at DESC LIMIT :limit
                """
            ),
            params,
        )
    ).mappings().all()
    return [dict(row) for row in rows]


@router.post("/merge-conflicts/{conflict_id}/resolve")
async def resolve_conflict(
    conflict_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    note: str = Query(default="", max_length=2000),
) -> dict[str, Any]:
    row = (
        await session.execute(
            text(
                """
                UPDATE merge_conflicts
                SET status = 'RESOLVED', resolved_at = NOW(),
                    detail = detail || CAST(:extra AS JSONB)
                WHERE conflict_id = :conflict_id
                RETURNING conflict_id, status, resolved_at
                """
            ),
            {
                "conflict_id": str(conflict_id),
                "extra": __import__("json").dumps({"resolution_note": note}),
            },
        )
    ).mappings().first()
    await session.commit()
    if row is None:
        raise HTTPException(status_code=404, detail="conflict not found")
    return dict(row)


__all__ = ["router"]
