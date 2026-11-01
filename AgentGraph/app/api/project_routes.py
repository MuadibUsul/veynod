"""Projects, execution graph, delivery and runtime observability (Phase 7)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, RoleScope, get_principal
from app.database import get_db
from app.services import projects

router = APIRouter(prefix="/v1", tags=["Projects"])


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    repo: str | None = Field(default=None, max_length=256)
    description: str = Field(default="", max_length=20_000)


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
