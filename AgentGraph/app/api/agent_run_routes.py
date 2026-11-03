"""Product-task Agent Runtime routes (Phase 2K)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, can, get_principal, require_permission
from app.database import get_db
from app.errors import ApiError
from app.services import agent_runtime, task_lifecycle

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
    try:
        return await task_lifecycle.execution_view(session, principal, task_key)
    except task_lifecycle.LifecycleError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=str(exc)
        ) from exc


def _drive(principal: Principal) -> None:
    require_permission(principal, "task:read")
    if not principal.is_human and not can(principal, "task:claim"):
        require_permission(principal, "task:claim")


async def _product_task(session, principal: Principal, task_key: str):
    from app.auth import ensure_namespace

    task = await task_lifecycle.load_task_row(session, principal.workspace.id, task_key)
    if task is None:
        raise HTTPException(status_code=404, detail=f"{task_key} not found")
    ensure_namespace(principal, task.namespace)
    return task


def _delivery_error(exc: Exception) -> None:
    """Keep the delivery code in the standard envelope (``error.code``).

    A dict ``HTTPException.detail`` is flattened to ``http_error`` by the global
    handler, so the runner could never tell ``kernel_unbound`` from
    ``scope_reissue_required``.
    """
    from app.services.kernel_delivery import KernelDeliveryError

    if isinstance(exc, KernelDeliveryError):
        raise ApiError(exc.status_code, exc.code, str(exc)) from exc
    raise exc


@router.post(
    "/{task_key}/kernel/lease",
    summary="Create the Kernel task if needed and take a worktree lease",
)
async def kernel_lease(
    task_key: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> Any:
    """Write-set lock plus a pooled worktree. The lease token stays on the server."""
    _drive(principal)
    from app.services import kernel_delivery

    task = await _product_task(session, principal, task_key)
    try:
        return await kernel_delivery.open_lease(session, task)
    except kernel_delivery.KernelDeliveryError as exc:
        _delivery_error(exc)


@router.post(
    "/{task_key}/kernel/submit",
    summary="Submit a diff through Kernel submit_patch (gate + merge)",
)
async def kernel_submit(
    task_key: str,
    body: dict[str, Any],
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> Any:
    _drive(principal)
    from app.services import kernel_delivery

    task = await _product_task(session, principal, task_key)
    symbols = body.get("touched_symbols") or []
    if isinstance(symbols, str):
        symbols = [symbols]
    try:
        return await kernel_delivery.submit_diff(
            session,
            task,
            git_diff=str(body.get("git_diff") or ""),
            touched_symbols=[str(item) for item in symbols if str(item).strip()],
            local_test_passed=bool(body.get("local_test_passed")),
            idempotency_key=(str(body["idempotency_key"]) if body.get("idempotency_key") else None),
        )
    except kernel_delivery.KernelDeliveryError as exc:
        _delivery_error(exc)


@router.get(
    "/{task_key}/kernel/delivery",
    summary="Worktree lease, verification gate and merge for this task",
)
async def kernel_delivery_view(
    task_key: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> Any:
    require_permission(principal, "task:read")
    from app.services import kernel_delivery

    task = await _product_task(session, principal, task_key)
    try:
        return await kernel_delivery.refresh_delivery(session, task)
    except kernel_delivery.KernelDeliveryError as exc:
        _delivery_error(exc)


@router.get(
    "/{task_key}/trace",
    summary="One task_key -> product, Run, Kernel facts and merged timeline",
)
async def task_trace(
    task_key: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> Any:
    try:
        return await task_lifecycle.trace_view(session, principal, task_key)
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
