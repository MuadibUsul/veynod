"""Runner plane: SSE preemption stream, lease heartbeat/release, task status."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    RunnerIdentity,
    authenticate_worker_context,
    constant_time_equals,
    hash_token,
)
from app.config import settings
from app.database import get_db_session, get_session_factory
from app.engine.scheduler import DisjointWriteSetScheduler
from app.engine.scheduler_core import scheduler_core_manager
from app.models import DagTask, TaskStatus, Workspace
from app.services.event_bus import EventBus, sse_frame
from app.services.event_hub import EventBufferOverflow, get_event_hub
from app.services.runtime_metrics import get_runtime_metrics

router = APIRouter(prefix="/v1", tags=["Runner"])


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    lease_token: str = Field(..., min_length=16)
    extend_seconds: int = Field(default=300, ge=30, le=3600)


class ReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    lease_token: str = Field(..., min_length=16)
    reason: str = Field(default="Runner released the task", max_length=4000)
    retryable: bool = True


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer vnk_"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Veynod Kernel Runner token.",
        )
    return authorization[7:].strip()


async def require_runner(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> RunnerIdentity:
    token = _bearer_token(authorization)
    identity = await authenticate_worker_context(session, token)
    if identity is None or (
        settings.require_bound_tokens and identity.workspace_id is None
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runner token is unknown, revoked or not bound to a workspace.",
        )
    return identity


async def require_stream_runner(
    authorization: Annotated[str | None, Header()] = None,
) -> RunnerIdentity:
    """SSE connections must not pin a pooled DB connection for their lifetime."""
    token = _bearer_token(authorization)
    factory = get_session_factory()
    async with factory() as session:
        identity = await authenticate_worker_context(session, token)
    if identity is None or (
        settings.require_bound_tokens and identity.workspace_id is None
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runner token is unknown, revoked or not bound to a workspace.",
        )
    return identity


def _assert_task_workspace(identity: RunnerIdentity, task: DagTask) -> None:
    if identity.workspace_id is not None and task.workspace_id != identity.workspace_id:
        # Do not disclose the existence of another workspace's task.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found."
        )


async def _resolve_workspace_id(
    requested: UUID | None, identity: RunnerIdentity | None = None
) -> UUID:
    if identity is not None and identity.workspace_id is not None:
        if requested is not None and requested != identity.workspace_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Runner token is bound to a different workspace.",
            )
        factory = get_session_factory()
        async with factory() as session:
            exists = await session.scalar(
                select(Workspace.workspace_id).where(
                    Workspace.workspace_id == identity.workspace_id
                )
            )
        if exists is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The runner's workspace no longer exists.",
            )
        return identity.workspace_id
    factory = get_session_factory()
    async with factory() as session:
        if requested is not None:
            exists = await session.scalar(
                select(Workspace.workspace_id).where(
                    Workspace.workspace_id == requested
                )
            )
            if exists is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Workspace {requested} not found.",
                )
            return exists
        if settings.default_workspace_id is not None:
            pinned = await session.scalar(
                select(Workspace.workspace_id).where(
                    Workspace.workspace_id == settings.default_workspace_id
                )
            )
            if pinned is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "VN_DEFAULT_WORKSPACE_ID does not exist in this database."
                    ),
                )
            return pinned
        oldest = await session.scalar(
            select(Workspace.workspace_id)
            .order_by(Workspace.created_at.asc())
            .limit(1)
        )
        if oldest is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No workspace exists.",
            )
        return oldest


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------
@router.get("/events/stream")
async def event_stream(
    request: Request,
    identity: Annotated[RunnerIdentity, Depends(require_stream_runner)],
    workspace_id: UUID | None = Query(default=None),
    since: int | None = Query(default=None, ge=0),
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    resolved_workspace = await _resolve_workspace_id(workspace_id, identity)
    hub = get_event_hub()

    cursor = since
    if cursor is None and last_event_id and last_event_id.isdigit():
        cursor = int(last_event_id)
    if cursor is None:
        cursor = hub.latest_seq(resolved_workspace)
    cursor_value: int = int(cursor)

    async def generator():
        nonlocal cursor_value
        metrics = get_runtime_metrics()
        metrics.sse_opened()
        try:
            yield "retry: 3000\n: veynod stream open\n\n"
            while True:
                if await request.is_disconnected():
                    break

                earliest = hub.earliest_seq(resolved_workspace)
                latest = hub.latest_seq(resolved_workspace)
                if cursor_value < latest and (
                    earliest is None or cursor_value < earliest - 1
                ):
                    try:
                        events = await hub.catch_up(
                            resolved_workspace, cursor_value, 1000
                        )
                    except Exception:
                        yield 'event: STREAM_ERROR\ndata: {"error":"database unavailable"}\n\n'
                        await asyncio.sleep(2.0)
                        continue
                    if events:
                        for event in events:
                            cursor_value = event.event_seq
                            yield sse_frame(event)
                        continue

                try:
                    async for event in hub.subscribe(resolved_workspace, cursor_value):
                        if event is None:
                            yield ": heartbeat\n\n"
                        else:
                            cursor_value = event.event_seq
                            yield sse_frame(event)
                except EventBufferOverflow:
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    yield 'event: STREAM_ERROR\ndata: {"error":"event hub unavailable"}\n\n'
                    await asyncio.sleep(2.0)
        finally:
            metrics.sse_closed()

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Lease maintenance
# ---------------------------------------------------------------------------
@router.post("/runner/heartbeat")
async def heartbeat(
    body: HeartbeatRequest,
    identity: Annotated[RunnerIdentity, Depends(require_runner)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    worker_id = identity.worker_id
    task = await session.get(DagTask, body.task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found."
        )
    _assert_task_workspace(identity, task)
    if task.status != TaskStatus.LEASED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Task is {task.status.value}, not LEASED.",
        )
    if task.leased_by_runner != worker_id or task.lease_token_hash is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Lease belongs to a different runner.",
        )
    if not constant_time_equals(
        task.lease_token_hash, hash_token(body.lease_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid lease token."
        )

    scheduler = DisjointWriteSetScheduler(session, EventBus(session))
    expires_at = await scheduler.extend_lease(
        task.task_id, task.lease_token_hash, body.extend_seconds
    )
    if expires_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Lease could not be extended (state changed).",
        )
    await session.commit()
    return {
        "task_id": str(task.task_id),
        "lease_expires_at": expires_at,
        "runner_id": worker_id,
    }


@router.post("/runner/release")
async def release(
    body: ReleaseRequest,
    identity: Annotated[RunnerIdentity, Depends(require_runner)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    worker_id = identity.worker_id
    task = await session.get(DagTask, body.task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found."
        )
    _assert_task_workspace(identity, task)
    if task.status != TaskStatus.LEASED:
        # Idempotent: the agent may already have submitted through MCP.
        return {"released": False, "status": task.status.value}

    if task.leased_by_runner != worker_id or task.lease_token_hash is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Lease belongs to a different runner.",
        )
    token_hash = hash_token(body.lease_token)
    if not constant_time_equals(task.lease_token_hash, token_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid lease token."
        )

    scheduler = DisjointWriteSetScheduler(session, EventBus(session))
    result = await scheduler.release_task(
        task.task_id,
        token_hash,
        body.reason,
        retry_counted=body.retryable,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Lease could not be released (state changed).",
        )
    await EventBus(session).publish(
        task.workspace_id,
        "TASK_RELEASED",
        {
            "task_id": str(task.task_id),
            "runner_id": worker_id,
            "new_status": result["status"],
            "reason": body.reason,
        },
    )
    await session.commit()
    await scheduler_core_manager.sync_tasks(
        session, task.workspace_id, [task.task_id]
    )
    return {
        "released": True,
        "task_id": str(task.task_id),
        "status": result["status"],
        "retry_count": result["retry_count"],
    }


# ---------------------------------------------------------------------------
# Task status (lets a runner detect an MCP-side submit/split/escalation)
# ---------------------------------------------------------------------------
@router.get("/runner/tasks/{task_id}")
async def task_status(
    task_id: UUID,
    identity: Annotated[RunnerIdentity, Depends(require_runner)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    task = await session.get(DagTask, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found."
        )
    _assert_task_workspace(identity, task)
    return {
        "task_id": str(task.task_id),
        "title": task.title,
        "status": task.status.value,
        "runner_id": task.leased_by_runner,
        "lease_expires_at": task.lease_expires_at,
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
        "result_commit_sha": task.result_commit_sha,
        "last_error_traceback": task.last_error_traceback,
    }


@router.get("/runner/ping")
async def ping(identity: Annotated[RunnerIdentity, Depends(require_runner)]) -> dict:
    return {
        "worker_id": identity.worker_id,
        "workspace_id": (
            str(identity.workspace_id) if identity.workspace_id else None
        ),
        "server_time": datetime.now(timezone.utc),
        "api_version": "4.0.0",
    }
