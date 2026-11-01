"""Product-task Agent Runtime routes (Phase 2K)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, get_principal
from app.database import get_db
from app.services import agent_runtime

router = APIRouter(prefix="/v1/tasks", tags=["Agent Runtime"])


def _raise(exc: agent_runtime.AgentRuntimeError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get(
    "/{task_key}/execution",
    summary="Unified task execution view (loop, code, gate, delivery)",
)
async def task_execution(
    task_key: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> Any:
    from app.services import task_lifecycle

    try:
        return await task_lifecycle.execution_view(session, principal, task_key)
    except task_lifecycle.LifecycleError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=str(exc)
        ) from exc


@router.post("/{task_key}/run", summary="Start the task's Agent Run")
@router.post("/{task_key}/agent-run", summary="Start an Agent Runtime run for a task")
async def create_agent_run(
    task_key: str,
    body: agent_runtime.AgentRunCreate,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> Any:
    try:
        return await agent_runtime.create_task_run(
            session, principal, task_key, body
        )
    except agent_runtime.AgentRuntimeError as exc:
        _raise(exc)


@router.get("/{task_key}/run", summary="Read the task's Agent Run")
@router.get("/{task_key}/agent-run", summary="Read and project an Agent Runtime run")
async def get_agent_run(
    task_key: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> Any:
    try:
        return await agent_runtime.get_task_run(session, principal, task_key)
    except agent_runtime.AgentRuntimeError as exc:
        _raise(exc)


@router.post("/{task_key}/run/cancel", summary="Cancel a task's Agent Run")
@router.post("/{task_key}/agent-run/cancel", summary="Cancel a task's Agent Run")
async def cancel_agent_run(
    task_key: str,
    reason: str = "Cancelled from AgentGraph",
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> Any:
    try:
        return await agent_runtime.cancel_task_run(
            session, principal, task_key, reason
        )
    except agent_runtime.AgentRuntimeError as exc:
        _raise(exc)


__all__ = ["router"]
