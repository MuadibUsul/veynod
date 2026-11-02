"""AgentGraph -> Veynod Agent Runtime adapter (Phase 2K).

AgentGraph owns the product task; the Runtime owns the Agent Run.  This module
creates a Runtime Run for an existing product task and projects terminal Run
states back to the task when it is read.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_permission
from app.config import get_settings
from app.database import async_session_factory
from app.models import CollabTask, TaskStatus, Workspace
from app.services import tasks as task_service
from app.services import usage as usage_service

logger = logging.getLogger("agentgraph.agent_runtime")
settings = get_settings()

_TERMINAL = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}


class AgentRunCreate(BaseModel):
    budget: dict[str, Any] = Field(default_factory=dict)


class AgentRuntimeError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class AgentRuntimeClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.transport = transport

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self.transport
        ) as client:
            response = await client.request(
                method,
                self.base_url + path,
                headers={"X-Runtime-Key": self.api_key},
                **kwargs,
            )
        if response.status_code >= 400:
            detail = response.text[:1000]
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    detail = str(payload.get("detail") or payload)[:1000]
            except Exception:  # noqa: BLE001
                pass
            raise AgentRuntimeError(
                f"Agent Runtime HTTP {response.status_code}: {detail}",
                status_code=response.status_code
                if response.status_code in {404, 409, 422, 429}
                else 502,
            )
        return response.json() if response.content else {}

    async def create_run(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        product_task_id: str,
        budget: dict[str, Any],
        goal: str | None = None,
        project_id: str | None = None,
        repo_path: str | None = None,
        trace_id: str | None = None,
        kernel: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/runs",
            json={
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "product_task_id": product_task_id,
                "goal": goal,
                "budget": budget,
                "project_id": project_id,
                "repo_path": repo_path,
                "trace_id": trace_id,
                "kernel": kernel or {},
            },
        )

    async def externals(self, run_id: str) -> list[dict[str, Any]]:
        """Deferred tool calls (Kernel submissions) owned by a Run."""
        payload = await self._request("GET", f"/v1/runs/{run_id}/external")
        return list(payload) if isinstance(payload, list) else []

    async def run_events(
        self, run_id: str, after_seq: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            f"/v1/runs/{run_id}/events?after_seq={int(after_seq)}&limit={int(limit)}",
        )
        return list(payload) if isinstance(payload, list) else []

    async def steps(self, run_id: str, limit: int = 200) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET", f"/v1/runs/{run_id}/steps?limit={int(limit)}"
        )
        return list(payload) if isinstance(payload, list) else []

    async def tenant_usage(self, tenant_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/usage/{tenant_id}")

    async def provider_health(self, window_minutes: int = 15) -> dict[str, Any]:
        return await self._request(
            "GET", f"/v1/provider-health?window_minutes={window_minutes}"
        )

    async def events(self, after_seq: int, limit: int = 500) -> dict[str, Any]:
        return await self._request(
            "GET", f"/v1/events?after_seq={after_seq}&limit={limit}"
        )

    async def latest_event_seq(self) -> int:
        payload = await self._request("GET", "/v1/events/head")
        return int(payload.get("latest_event_seq") or 0)

    async def tenant_quota(self, tenant_id: str) -> dict[str, Any] | None:
        """Current tenant quota state; None when the Runtime predates quotas."""
        try:
            return await self._request("GET", f"/v1/quota/{tenant_id}")
        except AgentRuntimeError as exc:
            if exc.status_code == 404:
                logger.warning(
                    "Agent Runtime has no quota endpoint; skipping the pre-check"
                )
                return None
            raise

    async def start_run(self, run_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/v1/runs/{run_id}/start")

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/runs/{run_id}")

    async def cancel_run(self, run_id: str, reason: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/v1/runs/{run_id}/cancel", json={"reason": reason}
        )


def _client() -> AgentRuntimeClient:
    if not settings.agent_runtime_url:
        raise AgentRuntimeError(
            "AGENT_RUNTIME_URL is not configured for this deployment", 503
        )
    return AgentRuntimeClient(
        settings.agent_runtime_url,
        settings.agent_runtime_key,
        settings.agent_runtime_timeout,
    )


def _run_ids(task: Any) -> tuple[str, str] | None:
    context = getattr(task, "input_context", None) or {}
    runtime = context.get("agent_runtime") or {}
    run_id = runtime.get("run_id")
    base_url = runtime.get("base_url")
    if not run_id or not base_url:
        return None
    return str(base_url), str(run_id)


def runtime_status_action(runtime_status: str, task_status: str) -> str | None:
    """Only terminal Runtime states move a not-yet-reviewed product task."""
    if task_status not in {"TODO", "READY", "IN_PROGRESS"}:
        return None
    if runtime_status == "COMPLETED" and task_status == "READY":
        return "review"
    if runtime_status == "FAILED":
        return "fail"
    if runtime_status == "CANCELLED":
        return "cancel"
    return None


async def create_task_run(
    session: AsyncSession,
    principal: Principal,
    task_key: str,
    request: AgentRunCreate,
) -> dict[str, Any]:
    """Start the single active Agent Run for a product task.

    The write authority lives in ``task_lifecycle`` (ADR-015); this adapter
    keeps the historical entry point so existing callers keep working.
    """
    require_permission(principal, "task:transition")
    from app.services import task_lifecycle

    try:
        return await task_lifecycle.start_run(
            session,
            principal,
            task_key,
            budget=request.budget,
            goal=getattr(request, "goal", None),
        )
    except task_lifecycle.LifecycleError as exc:
        raise AgentRuntimeError(str(exc), exc.status_code) from exc


async def meter_runtime_run(
    session: AsyncSession,
    *,
    workspace_id: Any,
    run: dict[str, Any],
    tenant_id: str,
) -> bool:
    """Mirror terminal Runtime usage into the product ledger exactly once."""
    usage = run.get("usage") or {}
    run_id = str(run.get("run_id") or "")
    if not run_id or run.get("status") not in {"COMPLETED", "FAILED", "CANCELLED"}:
        return False
    prompt = float(usage.get("prompt_tokens") or 0)
    completion = float(usage.get("completion_tokens") or 0)
    cost = float(usage.get("cost_usd") or 0)
    if cost == 0.0:
        cost = (
            prompt / 1000 * settings.runtime_prompt_usd_per_1k
            + completion / 1000 * settings.runtime_completion_usd_per_1k
        )
    inserted = (
        await session.execute(
            text(
                """
                INSERT INTO runtime_usage_metered (
                    run_id, workspace_id, tenant_id, model_calls,
                    prompt_tokens, completion_tokens, cost_usd
                ) VALUES (
                    CAST(:run_id AS UUID), :workspace_id, :tenant_id,
                    :model_calls, :prompt, :completion, :cost
                )
                ON CONFLICT (run_id) DO NOTHING
                RETURNING run_id
                """
            ),
            {
                "run_id": run_id,
                "workspace_id": str(workspace_id),
                "tenant_id": str(tenant_id or ""),
                "model_calls": float(usage.get("model_calls") or 0),
                "prompt": prompt,
                "completion": completion,
                "cost": cost,
            },
        )
    ).scalar_one_or_none()
    if inserted is None:
        return False
    await usage_service.record_usage(
        session,
        workspace_id=workspace_id,
        agent_signature="agent_runtime",
        category="agent_runtime",
        amount=int(prompt + completion),
        cost_usd=cost,
        metadata={
            "run_id": run_id,
            "model_calls": float(usage.get("model_calls") or 0),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
        },
    )
    await session.commit()
    return True


async def get_task_run(
    session: AsyncSession, principal: Principal, task_key: str
) -> dict[str, Any]:
    """Read a Run and project a terminal state through the lifecycle."""
    require_permission(principal, "task:read")
    from app.services import task_lifecycle

    task = await task_service.get_task(session, principal, task_key)
    correlation = task.correlation or {}
    run_id = correlation.get("run_id")
    if not run_id:
        raise AgentRuntimeError(f"{task_key} has no Agent Runtime run", 404)
    client = _client()
    run = await client.get_run(str(run_id))
    action = None
    try:
        action = await task_lifecycle.project_run(
            session,
            workspace=principal.workspace,
            task=task,
            run=run,
        )
    except Exception:  # noqa: BLE001 - the run stays readable
        logger.exception("lifecycle projection failed for %s", run_id)
        await session.rollback()
    try:
        await meter_runtime_run(
            session,
            workspace_id=principal.workspace.id,
            run=run,
            tenant_id=str(principal.tenant_id),
        )
    except Exception:  # noqa: BLE001 - metering must not block the read
        logger.exception("runtime usage metering failed for run %s", run_id)
        await session.rollback()
    refreshed = await task_service.get_task(session, principal, task.task_key)
    return {
        "task_key": refreshed.task_key,
        "run_id": str(run_id),
        "trace_id": correlation.get("trace_id"),
        "runtime_status": str(run.get("status") or "UNKNOWN"),
        "runtime": run,
        "agentgraph_status": refreshed.status.value,
        "projected": action,
    }


async def cancel_task_run(
    session: AsyncSession, principal: Principal, task_key: str, reason: str
) -> dict[str, Any]:
    require_permission(principal, "task:transition")
    from app.services import task_lifecycle

    return await task_lifecycle.cancel_run(session, principal, task_key, reason)


async def _legacy_cancel_task_run(
    session: AsyncSession, principal: Principal, task_key: str, reason: str
) -> dict[str, Any]:
    task = await task_service.get_task(session, principal, task_key)
    ids = _run_ids(task)
    if ids is None:
        raise AgentRuntimeError(f"{task_key} has no Agent Runtime run", 404)
    base_url, run_id = ids
    client = AgentRuntimeClient(
        base_url, settings.agent_runtime_key, settings.agent_runtime_timeout
    )
    run = await client.get_run(run_id)
    if run.get("status") not in {"COMPLETED", "FAILED", "CANCELLED"}:
        await client.cancel_run(run_id, reason)
    if task.status not in _TERMINAL:
        try:
            await task_service.cancel_task(
                session,
                principal.workspace.id,
                task.task_key,
                reason=reason,
                expected_version=None,
                actor=principal.agent_signature,
                propagate_to_kernel=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("could not cancel product task %s: %s", task_key, exc)
    refreshed = await task_service.get_task(session, principal, task.task_key)
    return {
        "task_key": refreshed.task_key,
        "run_id": run_id,
        "runtime_status": run.get("status"),
        "agentgraph_status": refreshed.status.value,
    }


async def sync_runtime_run_event(base_url: str, run_id: str) -> dict[str, int]:
    """Project one Runtime run into the task that owns it (durable inbox path)."""
    from app.services import task_lifecycle

    stats = {"tasks": 0, "projected": 0, "skipped": 0}
    async with async_session_factory() as session:
        task = (
            await session.execute(
                select(CollabTask)
                .where(CollabTask.active_run_id == UUID(run_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if task is None:
            stats["skipped"] += 1
            return stats
        stats["tasks"] = 1
        workspace = await session.get(Workspace, task.workspace_id)
        if workspace is None:
            stats["skipped"] += 1
            return stats
        client = AgentRuntimeClient(
            base_url or settings.agent_runtime_url or "",
            settings.agent_runtime_key,
            settings.agent_runtime_timeout,
        )
        run = await client.get_run(run_id)
        action = await task_lifecycle.project_run(
            session, workspace=workspace, task=task, run=run
        )
        if action:
            stats["projected"] += 1
        else:
            stats["skipped"] += 1
    return stats


async def project_agent_runs_once(limit: int = 100) -> dict[str, int]:
    """Safety-net sweep; the durable inbox is the primary projection path."""
    from app.services import task_lifecycle

    return await task_lifecycle.reconcile_active_runs(limit)


async def agent_run_projection_loop() -> None:
    logger.info(
        "agent runtime projection loop started (interval=%ss)",
        settings.agent_runtime_projection_interval_seconds,
    )
    while True:
        try:
            stats = await project_agent_runs_once()
            logger.info("agent runtime projection tick: %s", stats)
        except Exception:  # noqa: BLE001 - the loop must survive
            logger.exception("agent runtime projection loop failed")
        await asyncio.sleep(
            max(3, settings.agent_runtime_projection_interval_seconds)
        )


__all__ = [
    "AgentRunCreate",
    "AgentRuntimeClient",
    "AgentRuntimeError",
    "agent_run_projection_loop",
    "cancel_task_run",
    "create_task_run",
    "get_task_run",
    "project_agent_runs_once",
    "runtime_status_action",
]
