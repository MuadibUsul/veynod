"""Projects, execution graph, delivery and runtime observability (Phase 7)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, RoleScope, get_principal, is_manager
from app.database import get_db
from app.errors import Forbidden
from app.services import projects

router = APIRouter(prefix="/v1", tags=["Projects"])


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    repo: str | None = Field(default=None, max_length=256)
    description: str = Field(default="", max_length=20_000)


class RepositoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(min_length=1, max_length=1024)
    default_branch: str = Field(default="main", max_length=128)
    kernel_workspace_id: str = Field(min_length=1, max_length=64)
    kernel_base_url: str | None = Field(default=None, max_length=1024)
    credential_ref: str = Field(
        default="KERNEL_RUNTIME_TOKEN", max_length=128
    )


class TenantLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_model_calls: float | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0)


@router.get("/projects", summary="List product projects")
async def list_projects(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    return await projects.list_projects(session, principal)


@router.post("/projects", status_code=201, summary="Create or update a project")
async def create_project(
    body: ProjectCreate,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await projects.create_project(
        session,
        principal,
        name=body.name,
        repo=body.repo,
        description=body.description,
    )


@router.get(
    "/projects/{project}/repositories",
    summary="Repositories bound to a project",
)
async def list_project_repositories(
    project: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    from sqlalchemy import text

    from app.services import task_lifecycle  # noqa: F401  (import check)

    rows = (
        await session.execute(
            text(
                """
                SELECT r.id, r.project_id, r.repo_path, r.default_branch,
                       r.kernel_workspace_id,
                       b.kernel_base_url, b.binding_id
                FROM project_repositories r
                LEFT JOIN kernel_bindings b
                  ON b.workspace_id = r.workspace_id
                 AND b.repo_path = r.repo_path
                 AND b.project = :project
                WHERE r.workspace_id = :workspace_id
                  AND r.project_id = (
                      SELECT project_id FROM projects
                      WHERE workspace_id = :workspace_id AND name = :project
                  )
                ORDER BY r.created_at ASC
                """
            ),
            {
                "workspace_id": principal.workspace.id,
                "project": project,
            },
        )
    ).mappings().all()
    return [dict(row) for row in rows]


@router.post(
    "/projects/{project}/repositories",
    status_code=201,
    summary="Bind a repository (and its Kernel workspace) to a project",
)
async def add_project_repository(
    project: str,
    body: RepositoryCreate,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from sqlalchemy import text

    from app.services import task_lifecycle

    if not is_manager(principal):
        raise Forbidden("only a workspace owner or admin can bind a repository")
    from app.config import get_settings

    settings = get_settings()
    from app.services.kernel_binding_policy import validate_workspace_assignment

    validate_workspace_assignment(
        str(principal.workspace.id), body.kernel_workspace_id,
        body.kernel_base_url or settings.kernel_url, body.credential_ref)
    project_row = (
        await session.execute(
            text(
                "SELECT project_id FROM projects "
                "WHERE workspace_id = :ws AND name = :name"
            ),
            {"ws": principal.workspace.id, "name": project},
        )
    ).mappings().first()
    if project_row is None:
        raise HTTPException(status_code=404, detail=f"project {project} not found")
    row = (
        await session.execute(
            text(
                """
                INSERT INTO project_repositories
                    (workspace_id, project_id, repo_path, default_branch,
                     kernel_workspace_id)
                VALUES (:ws, :project_id, :repo, :branch,
                        CAST(:kernel_ws AS UUID))
                ON CONFLICT (project_id, repo_path) DO UPDATE
                SET default_branch = EXCLUDED.default_branch,
                    kernel_workspace_id = EXCLUDED.kernel_workspace_id
                RETURNING id, repo_path, default_branch, kernel_workspace_id
                """
            ),
            {
                "ws": principal.workspace.id,
                "project_id": project_row["project_id"],
                "repo": body.repo_path,
                "branch": body.default_branch,
                "kernel_ws": body.kernel_workspace_id,
            },
        )
    ).mappings().one()
    await task_lifecycle.upsert_kernel_binding(
        session,
        workspace_id=principal.workspace.id,
        project=project,
        repo_path=body.repo_path,
        kernel_base_url=body.kernel_base_url or settings.kernel_url,
        kernel_workspace_id=body.kernel_workspace_id,
        credential_ref=body.credential_ref,
    )
    await session.commit()
    return dict(row)


@router.get("/execution-graph", summary="Execution graph for a project or workspace")
async def execution_graph(
    project: str | None = Query(default=None),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await projects.execution_graph(session, principal, project)


@router.get("/projects/{project}/execution", summary="Project execution graph")
async def project_execution(
    project: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await projects.execution_graph(session, principal, project)


@router.get("/deliveries", summary="Committed deliveries")
async def list_deliveries(
    project: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    return await projects.deliveries(
        session, principal, project=project, limit=limit
    )


@router.get("/projects/{project}/deliveries", summary="Project deliveries")
async def project_deliveries(
    project: str,
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    return await projects.deliveries(
        session, principal, project=project, limit=limit
    )


@router.get("/runtime/observability", summary="Agent Runs and provider health")
async def runtime_observability(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await projects.runtime_observability(session, principal)


@router.get("/usage/runtime", summary="Runtime usage metered into the product")
async def runtime_usage(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await projects.usage_summary(session, principal)


@router.get("/tenant/runtime-limit", summary="Current tenant Runtime limit")
async def get_runtime_limit(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await projects.get_tenant_limit(session, str(principal.tenant_id))


@router.put("/tenant/runtime-limit", summary="Set tenant Runtime limit")
async def set_runtime_limit(
    body: TenantLimit,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if principal.role not in {RoleScope.COMMANDER, RoleScope.SUPER_ADMIN}:
        raise HTTPException(status_code=403, detail="commander role required")
    return await projects.set_tenant_limit(
        session,
        str(principal.tenant_id),
        max_model_calls=body.max_model_calls,
        max_cost_usd=body.max_cost_usd,
    )


__all__ = ["router"]
