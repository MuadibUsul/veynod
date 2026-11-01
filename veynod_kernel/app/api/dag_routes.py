"""Read-only DAG topology for the observation dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_routes import require_admin
from app.config import REPO_ROOT, settings
from app.database import get_db_session
from app.engine import rust_bridge
from app.engine.lock_algebra import plan_claimable
from app.models import CodeArtifact, DagTask, KernelEvent, TaskDependency, TaskStatus, Workspace

router = APIRouter(tags=["DAG / Observability"])


async def _resolve_workspace_id(
    session: AsyncSession, requested: UUID | None
) -> UUID:
    if requested is not None:
        exists = await session.scalar(
            select(Workspace.workspace_id).where(Workspace.workspace_id == requested)
        )
        if exists is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workspace {requested} not found.",
            )
        return exists
    if settings.default_workspace_id is not None:
        return settings.default_workspace_id
    oldest = await session.scalar(
        select(Workspace.workspace_id).order_by(Workspace.created_at.asc()).limit(1)
    )
    if oldest is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No workspace exists. Create one via /v1/admin/workspaces.",
        )
    return oldest


@router.get("/v1/dag")
async def dag_topology(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _admin: Annotated[None, Depends(require_admin)],
    workspace_id: UUID | None = Query(default=None),
    limit: int = Query(default=5000, ge=1, le=20000),
) -> dict:
    resolved = await _resolve_workspace_id(session, workspace_id)
    workspace = await session.get(Workspace, resolved)

    tasks = (
        await session.execute(
            select(DagTask)
            .where(DagTask.workspace_id == resolved)
            .order_by(DagTask.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    task_ids = {task.task_id for task in tasks}

    dependencies = (
        await session.execute(
            select(TaskDependency).where(
                TaskDependency.upstream_task_id.in_(task_ids)
            )
        )
    ).scalars().all() if task_ids else []

    artifacts = (
        await session.execute(
            select(CodeArtifact)
            .where(CodeArtifact.workspace_id == resolved)
            .order_by(CodeArtifact.symbol_path.asc())
            .limit(limit)
        )
    ).scalars().all()

    status_rows = (
        await session.execute(
            select(DagTask.status, func.count())
            .where(DagTask.workspace_id == resolved)
            .group_by(DagTask.status)
        )
    ).all()
    status_counts: dict = {}
    for status_value, count in status_rows:
        status_counts[status_value] = count

    latest_event_seq = await session.scalar(
        select(func.max(KernelEvent.event_seq)).where(
            KernelEvent.workspace_id == resolved
        )
    )
    return {
        "workspace": {
            "workspace_id": str(resolved),
            "name": workspace.name if workspace else None,
            "head_commit_sha": workspace.head_commit_sha if workspace else None,
            "main_branch": workspace.main_branch if workspace else None,
        },
        "latest_event_seq": int(latest_event_seq or 0),
        "stats": {
            "tasks": len(tasks),
            "artifacts": len(artifacts),
            "by_status": {
                (key.value if hasattr(key, "value") else str(key)): count
                for key, count in status_counts.items()
            },
        },
        "nodes": [
            {
                "task_id": str(task.task_id),
                "title": task.title,
                "type": task.type.value,
                "status": task.status.value,
                "priority": task.priority,
                "in_degree": task.in_degree,
                "parent_task_id": str(task.parent_task_id)
                if task.parent_task_id
                else None,
                "runner": task.leased_by_runner,
                "retry_count": task.retry_count,
                "write_locks": list(task.write_locks),
                "updated_at": task.updated_at,
            }
            for task in tasks
        ],
        "edges": [
            {
                "upstream_task_id": str(edge.upstream_task_id),
                "downstream_task_id": str(edge.downstream_task_id),
            }
            for edge in dependencies
        ],
        "artifacts": [
            {
                "artifact_id": str(artifact.artifact_id),
                "symbol_path": artifact.symbol_path,
                "kind": artifact.kind.value,
                "status": artifact.status.value,
                "version": artifact.version,
            }
            for artifact in artifacts
        ],
    }


@router.get("/v1/dag/plan")
async def dag_plan(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _admin: Annotated[None, Depends(require_admin)],
    workspace_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    """Read-only snapshot plan computed by the Rust disjoint-write-set engine.

    The authoritative claim remains the PostgreSQL GIN + advisory-lock
    transaction; this endpoint is for operators and conflict diagnostics.
    """
    resolved = await _resolve_workspace_id(session, workspace_id)
    tasks = (
        await session.execute(
            select(DagTask)
            .where(
                DagTask.workspace_id == resolved,
                DagTask.status.in_(
                    [TaskStatus.READY, TaskStatus.LEASED, TaskStatus.VERIFYING]
                ),
            )
            .order_by(DagTask.created_at.asc())
            .limit(50000)
        )
    ).scalars().all()

    snapshots = [
        {
            "task_id": str(task.task_id),
            "write_locks": list(task.write_locks),
            "ancestor_locks": list(task.ancestor_locks),
            "status": task.status.value,
            "priority": task.priority,
            "seq": int(task.created_at.timestamp() * 1_000_000),
        }
        for task in tasks
    ]
    plan = plan_claimable(snapshots)
    return {
        "workspace_id": str(resolved),
        "engine": plan["engine"],
        "tasks_considered": len(snapshots),
        "claimable_count": len(plan["claimable"]),
        "claimable": plan["claimable"][:limit],
        "blocked": plan["conflicts"][:limit],
        "note": (
            "read-only snapshot; the authoritative lease is claimed by "
            "PostgreSQL GIN + per-workspace advisory lock"
        ),
    }


@router.get("/v1/dag/events")
async def dag_events(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _admin: Annotated[None, Depends(require_admin)],
    workspace_id: UUID | None = Query(default=None),
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    resolved = await _resolve_workspace_id(session, workspace_id)
    events = (
        await session.execute(
            select(KernelEvent)
            .where(
                KernelEvent.workspace_id == resolved,
                KernelEvent.event_seq > after_seq,
            )
            .order_by(KernelEvent.event_seq.asc())
            .limit(limit)
        )
    ).scalars().all()
    return {
        "workspace_id": str(resolved),
        "events": [
            {
                "event_seq": event.event_seq,
                "event_id": str(event.event_id) if event.event_id else None,
                "event_version": event.event_version,
                "producer": event.producer,
                "correlation_id": event.correlation_id,
                "causation_id": event.causation_id,
                "event_type": event.event_type,
                "payload": event.payload,
                "created_at": event.created_at,
            }
            for event in events
        ],
        "next_after_seq": events[-1].event_seq if events else after_seq,
    }


@router.get("/dashboard", include_in_schema=False)
async def dashboard(request: Request) -> FileResponse:
    if not settings.debug_dashboard_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The Kernel debug dashboard is disabled.",
        )
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The Kernel debug dashboard is loopback-only.",
        )
    path = Path(REPO_ROOT) / "dashboard-lite" / "index.html"
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="dashboard-lite/index.html is not installed.",
        )
    return FileResponse(path, media_type="text/html")


@router.get("/healthz")
async def healthz(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    await session.execute(text("SELECT 1"))
    return {
        "status": "ok",
        "version": "4.0.0",
        "core": rust_bridge.describe(),
    }
