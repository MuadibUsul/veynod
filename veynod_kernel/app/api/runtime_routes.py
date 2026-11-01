"""Runtime service plane: the Agent Runtime's internal Kernel identity.

This is a *separate* principal from the admin key and from runner tokens:

* ``X-Veynod-Service-Key`` identifies the Agent Runtime service.  It can read a
  workspace's durable event feed, read task evidence and cancel a task.
* Runner tokens (``Bearer vnk_...``) stay the credential for the syscall plane
  (pull/submit/split/...), because every one of those calls is lease-bound.

Every endpoint is workspace-scoped; a request without a workspace that resolves
to exactly one is rejected rather than silently falling back, so a multi-tenant
deployment cannot cross-read.
"""

from __future__ import annotations

import hmac
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RunnerIdentity
from app.config import settings
from app.database import get_db_session
from app.models import DagTask, KernelEvent, Workspace
from app.api.runner_routes import require_runner
from app.engine.task_commands import (
    TaskCancelConflict,
    TaskNotFound,
    cancel_task as cancel_task_command,
)
from app.services.git_ops import GitError, GitRepository, RepositoryNotUsable
from app.services.task_bootstrap import create_dag_task, task_json
from app.services.task_evidence import build_task_evidence

router = APIRouter(prefix="/v1", tags=["Runtime service"])


async def require_runtime_service(
    x_veynod_service_key: Annotated[
        str | None, Header(alias="X-Veynod-Service-Key")
    ] = None,
) -> None:
    expected = settings.runtime_service_key or ""
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="VN_RUNTIME_SERVICE_KEY is not configured on this Kernel.",
        )
    if not x_veynod_service_key or not hmac.compare_digest(
        x_veynod_service_key.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid X-Veynod-Service-Key.",
        )


def _workspace_id(value: UUID | None, header: str | None) -> UUID:
    chosen = value
    if chosen is None and header:
        try:
            chosen = UUID(header.strip())
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="X-Workspace-Id must be a UUID.",
            ) from exc
    if chosen is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="workspace_id is required for the runtime service plane.",
        )
    return chosen


async def _workspace(session: AsyncSession, workspace_id: UUID) -> Workspace:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {workspace_id} not found.",
        )
    return workspace


# ---------------------------------------------------------------------------
# Events (durable cursor feed consumed by the Agent Runtime watcher)
# ---------------------------------------------------------------------------


@router.get("/runtime/events", dependencies=[Depends(require_runtime_service)])
async def runtime_events(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    workspace_id: UUID | None = Query(default=None),
    x_workspace_id: Annotated[
        str | None, Header(alias="X-Workspace-Id")
    ] = None,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    resolved = _workspace_id(workspace_id, x_workspace_id)
    await _workspace(session, resolved)
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


@router.post("/runtime/tasks", status_code=201)
async def runtime_create_task(
    body: dict,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    x_workspace_id: Annotated[
        str | None, Header(alias="X-Workspace-Id")
    ] = None,
    _service: None = Depends(require_runtime_service),
) -> dict:
    """Create the Kernel DAG node an Agent Run will lease and execute.

    The Runtime carries the plan (spec, write set, frozen tests) but never the
    admin key; this endpoint is the product-to-kernel hand-off at the
    workspace boundary.  ``request_key`` makes a retried Run create one node.
    """
    workspace_id = _workspace_id(None, x_workspace_id)
    workspace = await _workspace(session, workspace_id)
    request_key = str(body.get("request_key") or "").strip()
    if request_key:
        existing = await session.scalar(
            text(
                """
                SELECT task_id FROM dag_tasks
                WHERE workspace_id = :workspace_id
                  AND runtime_request_key = :key
                LIMIT 1
                """
            ),
            {"workspace_id": workspace_id, "key": request_key},
        )
        if existing is not None:
            task = await session.get(DagTask, existing)
            if task is not None:
                return task_json(task)
    title = str(body.get("title") or "").strip()
    spec = str(body.get("spec_prompt") or "").strip()
    if not title or len(spec) < 10:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="title and spec_prompt (>=10 chars) are required.",
        )
    task = await create_dag_task(
        session,
        workspace=workspace,
        title=title[:256],
        spec_prompt=spec[:20_000],
        task_type=str(body.get("type") or "IMPLEMENT_BODY"),
        read_symbols=list(body.get("read_symbols") or []),
        target_symbols=list(body.get("target_symbols") or []),
        test_symbols=list(body.get("test_symbols") or []),
        write_set_symbols=list(body.get("write_set_symbols") or []),
        priority=int(body.get("priority") or 100),
    )
    if request_key:
        await session.execute(
            text(
                "UPDATE dag_tasks SET runtime_request_key = :key "
                "WHERE task_id = :task_id"
            ),
            {"key": request_key[:200], "task_id": task.task_id},
        )
        await session.commit()
    return task_json(task)


@router.get(
    "/runtime/tasks/{task_id}/evidence",
    dependencies=[Depends(require_runtime_service)],
)
async def runtime_task_evidence(
    task_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    x_workspace_id: Annotated[
        str | None, Header(alias="X-Workspace-Id")
    ] = None,
) -> dict:
    task = await session.get(DagTask, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found."
        )
    if x_workspace_id and str(task.workspace_id) != x_workspace_id.strip():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found."
        )
    payload = await build_task_evidence(session, task_id)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found."
        )
    return payload


@router.post(
    "/runtime/tasks/{task_id}/cancel",
    dependencies=[Depends(require_runtime_service)],
)
async def runtime_cancel_task(
    task_id: UUID,
    body: dict,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    x_workspace_id: Annotated[
        str | None, Header(alias="X-Workspace-Id")
    ] = None,
) -> dict:
    task = await session.get(DagTask, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found."
        )
    if x_workspace_id and str(task.workspace_id) != x_workspace_id.strip():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found."
        )
    reason = str(body.get("reason") or "cancelled by the Agent Runtime")
    try:
        return await cancel_task_command(
            session, task_id, reason=reason, actor="runtime"
        )
    except TaskNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except TaskCancelConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc


# ---------------------------------------------------------------------------
# Runner plane additions: evidence and read-only file access
# ---------------------------------------------------------------------------


@router.get("/runner/tasks/{task_id}/evidence")
async def runner_task_evidence(
    task_id: UUID,
    identity: Annotated[RunnerIdentity, Depends(require_runner)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    task = await session.get(DagTask, task_id)
    if task is None or (
        identity.workspace_id is not None
        and task.workspace_id != identity.workspace_id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found."
        )
    payload = await build_task_evidence(session, task_id)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found."
        )
    return payload


@router.get("/runner/workspaces/{workspace_id}/file")
async def runner_workspace_file(
    workspace_id: UUID,
    path: str,
    identity: Annotated[RunnerIdentity, Depends(require_runner)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    rev: str = "HEAD",
) -> dict:
    """Read one file at ``rev`` (default HEAD) inside the caller's workspace.

    Read-only, sized, and path-normalised: the runner needs a base to produce a
    valid unified diff, nothing more.
    """
    if not settings.allow_runner_file_read:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="File reads are disabled on this Kernel.",
        )
    if identity.workspace_id is not None and workspace_id != identity.workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found."
        )
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found."
        )
    cleaned = path.strip().replace("\\", "/").lstrip("/")
    if not cleaned or ".." in cleaned.split("/"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="path must be a relative repository path.",
        )
    repo = GitRepository(workspace.repo_path, workspace.main_branch)
    try:
        content = await repo.read_file(cleaned, rev=rev)
    except (GitError, RepositoryNotUsable) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{cleaned} does not exist at {rev}.",
        )
    encoded = content.encode("utf-8")
    if len(encoded) > settings.runner_file_max_bytes:
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={
                "error": "file_too_large",
                "size": len(encoded),
                "limit": settings.runner_file_max_bytes,
                "path": cleaned,
            },
        )
    return {
        "workspace_id": str(workspace_id),
        "path": cleaned,
        "rev": rev,
        "size": len(encoded),
        "content": content,
    }


__all__ = ["require_runtime_service", "router"]
