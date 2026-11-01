"""AgentGraph -> Veynod Agent Runtime adapter (Phase 2K).

AgentGraph owns the product task; the Runtime owns the Agent Run.  This module
creates a Runtime Run for an existing product task and projects terminal Run
states back to the task when it is read.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, principal_for_admin, require_permission
from app.config import get_settings
from app.database import async_session_factory
from app.models import CollabTask, TaskStatus, Workspace
from app.schemas import HandoffBrief, TaskClaimRequest, TaskHandoffRequest
from app.services import tasks as task_service
from app.services import usage as usage_service
from app.services.common import (
    append_event,
    record_audit,
    task_snapshot,
    utcnow,
)

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
            },
        )

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
    require_permission(principal, "task:transition")
    task = (
        await session.execute(
            select(CollabTask).where(
                CollabTask.workspace_id == principal.workspace.id,
                CollabTask.task_key == task_key,
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise AgentRuntimeError(f"{task_key} not found", 404)
    if task.status in _TERMINAL:
        raise AgentRuntimeError(f"{task_key} is {task.status.value}", 409)
    existing = _run_ids(task)
    if existing is not None:
        return await get_task_run(session, principal, task_key)
    client = _client()
    if not settings.agent_runtime_url:
        raise AgentRuntimeError("AGENT_RUNTIME_URL is not configured", 503)
    quota = await client.tenant_quota(str(principal.tenant_id))
    if quota and quota.get("exceeded"):
        raise AgentRuntimeError(
            "tenant runtime quota exceeded: "
            f"{quota.get('reason') or 'limit reached'}",
            429,
        )
    limit_row = (
        await session.execute(
            text(
                """
                SELECT max_model_calls, max_cost_usd
                FROM tenant_runtime_limits WHERE tenant_id = :tenant_id
                """
            ),
            {"tenant_id": str(principal.tenant_id)},
        )
    ).mappings().first()
    if limit_row is not None:
        usage = await client.tenant_usage(str(principal.tenant_id))
        if (
            limit_row["max_model_calls"] is not None
            and float(usage.get("model_calls", 0) or 0)
            >= float(limit_row["max_model_calls"])
        ):
            raise AgentRuntimeError(
                "tenant runtime quota exceeded: max_model_calls="
                f"{limit_row['max_model_calls']}",
                429,
            )
        if (
            limit_row["max_cost_usd"] is not None
            and float(usage.get("cost_usd", 0) or 0)
            >= float(limit_row["max_cost_usd"])
        ):
            raise AgentRuntimeError(
                "tenant runtime quota exceeded: max_cost_usd="
                f"{limit_row['max_cost_usd']}",
                429,
            )
    created = await client.create_run(
        tenant_id=str(principal.tenant_id),
        workspace_id=str(principal.workspace.id),
        product_task_id=task.task_key,
        goal=task.description,
        budget=request.budget,
    )
    run_id = str(created.get("run_id") or "")
    if not run_id:
        raise AgentRuntimeError("Agent Runtime did not return run_id")
    started = await client.start_run(run_id)
    before = task_snapshot(task)
    context = dict(task.input_context or {})
    context["agent_runtime"] = {
        "run_id": run_id,
        "base_url": settings.agent_runtime_url,
    }
    task.input_context = context
    task.version += 1
    task.updated_at = utcnow()
    await session.flush()
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=uuid4(),
        agent_signature=principal.agent_signature,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="UPDATE",
        before=before,
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type="TASK_UPDATED",
        payload={"task_key": task.task_key, "agent_runtime_run_id": run_id},
        namespace=task.namespace,
    )
    await session.commit()
    return {
        "task_key": task.task_key,
        "run_id": run_id,
        "runtime_status": started.get("status"),
        "agentgraph_status": task.status.value,
        "budget": started.get("budget") or request.budget,
    }


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
    require_permission(principal, "task:read")
    task = await task_service.get_task(session, principal, task_key)
    ids = _run_ids(task)
    if ids is None:
        raise AgentRuntimeError(f"{task_key} has no Agent Runtime run", 404)
    base_url, run_id = ids
    client = AgentRuntimeClient(
        base_url, settings.agent_runtime_key, settings.agent_runtime_timeout
    )
    run = await client.get_run(run_id)
    runtime_status = str(run.get("status") or "UNKNOWN")
    task_status = task.status.value
    action = runtime_status_action(runtime_status, task_status)
    admin = principal_for_admin(principal.workspace)
    if action == "review":
        try:
            await task_service.claim_task(
                session,
                admin,
                task.task_key,
                TaskClaimRequest(lease_seconds=120, include_context=False),
            )
            await task_service.handoff_task(
                session,
                admin,
                task.task_key,
                TaskHandoffRequest(
                    next_status="REVIEWING",
                    brief=HandoffBrief(
                        summary=f"Agent Runtime run {run_id} completed.",
                        key_metrics={"agent_runtime_run_id": run_id},
                        unverified_hypotheses=[],
                        risk_flag=False,
                    ),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - the run remains readable
            logger.info("could not project completed run %s: %s", run_id, exc)
    elif action == "fail":
        try:
            await task_service.fail_task(
                session,
                admin,
                task.task_key,
                reason=str(run.get("last_error") or "Agent Runtime run failed."),
                expected_version=task.version,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("could not project failed run %s: %s", run_id, exc)
    elif action == "cancel":
        try:
            await task_service.cancel_task(
                session,
                principal.workspace.id,
                task.task_key,
                reason="Agent Runtime run cancelled.",
                expected_version=task.version,
                actor="runtime_projection",
                propagate_to_kernel=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("could not project cancelled run %s: %s", run_id, exc)

    try:
        await meter_runtime_run(
            session,
            workspace_id=principal.workspace.id,
            run=run,
            tenant_id=str(principal.tenant_id),
        )
    except Exception:  # noqa: BLE001 - metering must not block the projection
        logger.exception("runtime usage metering failed for run %s", run_id)
        await session.rollback()
    refreshed = await task_service.get_task(session, principal, task.task_key)
    return {
        "task_key": refreshed.task_key,
        "run_id": run_id,
        "runtime_status": runtime_status,
        "runtime": run,
        "agentgraph_status": refreshed.status.value,
        "projected": action,
    }


async def cancel_task_run(
    session: AsyncSession, principal: Principal, task_key: str, reason: str
) -> dict[str, Any]:
    require_permission(principal, "task:transition")
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
    """Project one Runtime run into every task that references it."""
    stats = {"tasks": 0, "projected": 0, "skipped": 0}
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(CollabTask.id).where(
                    CollabTask.input_context["agent_runtime"]["run_id"].astext
                    == str(run_id)
                )
            )
        ).scalars().all()
        task_ids = [row for row in rows]
    stats["tasks"] = len(task_ids)
    for task_id in task_ids:
        try:
            async with async_session_factory() as session:
                task = await session.get(CollabTask, task_id)
                if task is None:
                    stats["skipped"] += 1
                    continue
                workspace = await session.get(Workspace, task.workspace_id)
                if workspace is None:
                    stats["skipped"] += 1
                    continue
                principal = principal_for_admin(workspace)
                result = await get_task_run(session, principal, task.task_key)
                if result.get("projected"):
                    stats["projected"] += 1
                else:
                    stats["skipped"] += 1
        except Exception:  # noqa: BLE001 - one task must not stop the event
            logger.exception(
                "runtime event projection failed for run %s task %s",
                run_id,
                task_id,
            )
            raise
    return stats


async def project_agent_runs_once(limit: int = 100) -> dict[str, int]:
    """Project terminal Runtime states for product tasks without a GET."""
    stats = {"scanned": 0, "projected": 0, "skipped": 0, "errors": 0}
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(CollabTask)
                .where(
                    CollabTask.status.in_(
                        [
                            TaskStatus.TODO,
                            TaskStatus.READY,
                            TaskStatus.IN_PROGRESS,
                        ]
                    ),
                    CollabTask.input_context["agent_runtime"]["run_id"]
                    .astext.isnot(None),
                )
                .order_by(CollabTask.updated_at.asc())
                .limit(max(1, min(500, limit)))
            )
        ).scalars().all()
        refs = [(task.id, task.workspace_id) for task in rows]

    stats["scanned"] = len(refs)
    for task_id, workspace_id in refs:
        try:
            async with async_session_factory() as session:
                task = await session.get(CollabTask, task_id)
                workspace = await session.get(Workspace, workspace_id)
                if task is None or workspace is None:
                    stats["skipped"] += 1
                    continue
                principal = principal_for_admin(workspace)
                result = await get_task_run(session, principal, task.task_key)
                if result.get("projected"):
                    stats["projected"] += 1
                else:
                    stats["skipped"] += 1
        except Exception:  # noqa: BLE001 - one run must not stop the loop
            stats["errors"] += 1
            logger.exception("agent runtime projection failed for task %s", task_id)
    return stats


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
