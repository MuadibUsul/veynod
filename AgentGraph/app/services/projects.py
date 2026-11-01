"""Product projects, execution graph and delivery views (Phase 7)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_permission
from app.models import CollabTask, TaskStatus
from app.services import code_engine
from app.services.common import utcnow


async def list_projects(
    session: AsyncSession, principal: Principal
) -> list[dict[str, Any]]:
    require_permission(principal, "task:read")
    rows = (
        await session.execute(
            text(
                """
                SELECT p.project_id, p.name, p.repo, p.description,
                       p.created_at, p.updated_at,
                       COUNT(t.id) AS task_count,
                       COUNT(t.id) FILTER (
                           WHERE t.status IN ('DONE')
                       ) AS done_count,
                       COUNT(t.id) FILTER (
                           WHERE t.status IN ('FAILED', 'CANCELLED')
                       ) AS failed_count
                FROM projects p
                LEFT JOIN collab_tasks t
                  ON t.workspace_id = p.workspace_id
                 AND t.project = p.name
                WHERE p.workspace_id = :workspace_id
                GROUP BY p.project_id
                ORDER BY p.created_at DESC
                """
            ),
            {"workspace_id": str(principal.workspace.id)},
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def create_project(
    session: AsyncSession,
    principal: Principal,
    *,
    name: str,
    repo: str | None,
    description: str,
) -> dict[str, Any]:
    require_permission(principal, "task:create")
    row = (
        await session.execute(
            text(
                """
                INSERT INTO projects (workspace_id, name, repo, description)
                VALUES (:workspace_id, :name, :repo, :description)
                ON CONFLICT (workspace_id, name) DO UPDATE
                SET repo = COALESCE(EXCLUDED.repo, projects.repo),
                    description = EXCLUDED.description,
                    updated_at = NOW()
                RETURNING project_id, name, repo, description, created_at,
                          updated_at
                """
            ),
            {
                "workspace_id": str(principal.workspace.id),
                "name": name,
                "repo": repo,
                "description": description,
            },
        )
    ).mappings().one()
    await session.commit()
    return dict(row)


def _task_node(task: CollabTask) -> dict[str, Any]:
    context = task.input_context or {}
    return {
        "task_key": task.task_key,
        "title": task.title,
        "status": task.status.value,
        "project": task.project,
        "repo": task.repo,
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "agent_runtime": context.get("agent_runtime"),
        "kernel": context.get("kernel"),
        "acceptance": task.acceptance,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


async def execution_graph(
    session: AsyncSession, principal: Principal, project: str | None = None
) -> dict[str, Any]:
    require_permission(principal, "task:read")
    query = select(CollabTask).where(
        CollabTask.workspace_id == principal.workspace.id
    )
    if project:
        query = query.where(CollabTask.project == project)
    tasks = (
        await session.execute(query.order_by(CollabTask.created_at.asc()))
    ).scalars().all()
    nodes = [_task_node(task) for task in tasks]
    edges = [
        {
            "from": str(task.parent_task_id),
            "to": task.task_key,
            "kind": "parent",
        }
        for task in tasks
        if task.parent_task_id
    ]
    status_counts: dict[str, int] = {}
    for task in tasks:
        status_counts[task.status.value] = status_counts.get(task.status.value, 0) + 1
    return {
        "project": project,
        "nodes": nodes,
        "edges": edges,
        "status_counts": status_counts,
        "generated_at": utcnow().isoformat(),
    }


async def deliveries(
    session: AsyncSession,
    principal: Principal,
    *,
    project: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    require_permission(principal, "task:read")
    query = select(CollabTask).where(
        CollabTask.workspace_id == principal.workspace.id,
        CollabTask.status.in_([TaskStatus.REVIEWING, TaskStatus.DONE]),
    )
    if project:
        query = query.where(CollabTask.project == project)
    tasks = (
        await session.execute(
            query.order_by(CollabTask.updated_at.desc()).limit(min(limit, 200))
        )
    ).scalars().all()
    engine = code_engine._engine()
    result: list[dict[str, Any]] = []
    for task in tasks:
        ids = code_engine._kernel_ids(task)
        if ids is None:
            continue
        _, kernel_task_id = ids
        try:
            record = await engine.delivery(kernel_task_id)
        except Exception:  # noqa: BLE001 - one unavailable delivery is not fatal
            record = None
        if record is None:
            continue
        result.append(
            {
                "task_key": task.task_key,
                "title": task.title,
                "project": task.project,
                "status": task.status.value,
                "kernel_task_id": kernel_task_id,
                "delivery": record,
            }
        )
    return result


async def runtime_observability(
    session: AsyncSession, principal: Principal
) -> dict[str, Any]:
    require_permission(principal, "task:read")
    rows = (
        await session.execute(
            select(CollabTask)
            .where(
                CollabTask.workspace_id == principal.workspace.id,
                CollabTask.input_context["agent_runtime"]["run_id"].astext.isnot(
                    None
                ),
            )
            .order_by(CollabTask.updated_at.desc())
            .limit(100)
        )
    ).scalars().all()
    runs = [
        {
            "task_key": task.task_key,
            "title": task.title,
            "status": task.status.value,
            "run": (task.input_context or {}).get("agent_runtime"),
        }
        for task in rows
    ]
    health: dict[str, Any] = {"status": "unknown", "providers": {}}
    if code_engine.settings.agent_runtime_url:
        from app.services import agent_runtime

        try:
            client = agent_runtime.AgentRuntimeClient(
                code_engine.settings.agent_runtime_url,
                code_engine.settings.agent_runtime_key,
                code_engine.settings.agent_runtime_timeout,
            )
            health = await client.provider_health()
        except Exception:  # noqa: BLE001 - observability is best effort
            health = {"status": "unreachable", "providers": {}}
    return {"runs": runs, "provider_health": health}


async def usage_summary(
    session: AsyncSession, principal: Principal
) -> dict[str, Any]:
    require_permission(principal, "task:read")
    row = (
        await session.execute(
            text(
                """
                SELECT
                    COUNT(*) AS runs,
                    COALESCE(SUM(model_calls), 0) AS model_calls,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(cost_usd), 0) AS cost_usd
                FROM runtime_usage_metered
                WHERE workspace_id = :workspace_id
                """
            ),
            {"workspace_id": str(principal.workspace.id)},
        )
    ).mappings().one()
    return {
        "workspace_id": str(principal.workspace.id),
        "runs": int(row["runs"] or 0),
        "model_calls": float(row["model_calls"] or 0),
        "prompt_tokens": float(row["prompt_tokens"] or 0),
        "completion_tokens": float(row["completion_tokens"] or 0),
        "cost_usd": float(row["cost_usd"] or 0),
    }


async def get_tenant_limit(session: AsyncSession, tenant_id: str) -> dict[str, Any]:
    row = (
        await session.execute(
            text(
                "SELECT tenant_id, max_model_calls, max_cost_usd, updated_at "
                "FROM tenant_runtime_limits WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": tenant_id},
        )
    ).mappings().first()
    return dict(row) if row else {"tenant_id": tenant_id}


async def set_tenant_limit(
    session: AsyncSession,
    tenant_id: str,
    *,
    max_model_calls: float | None,
    max_cost_usd: float | None,
) -> dict[str, Any]:
    row = (
        await session.execute(
            text(
                """
                INSERT INTO tenant_runtime_limits
                    (tenant_id, max_model_calls, max_cost_usd)
                VALUES (:tenant_id, :calls, :cost)
                ON CONFLICT (tenant_id) DO UPDATE
                SET max_model_calls = EXCLUDED.max_model_calls,
                    max_cost_usd = EXCLUDED.max_cost_usd,
                    updated_at = NOW()
                RETURNING tenant_id, max_model_calls, max_cost_usd, updated_at
                """
            ),
            {
                "tenant_id": tenant_id,
                "calls": max_model_calls,
                "cost": max_cost_usd,
            },
        )
    ).mappings().one()
    await session.commit()
    return dict(row)


__all__ = [
    "create_project",
    "deliveries",
    "execution_graph",
    "get_tenant_limit",
    "list_projects",
    "runtime_observability",
    "set_tenant_limit",
    "usage_summary",
]
