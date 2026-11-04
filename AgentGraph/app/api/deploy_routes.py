"""Deploy gate v2: per-project deploy targets, CI releases, runs and reports."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, get_principal
from app.database import get_db
from app.services import deploys

router = APIRouter(prefix="/v1", tags=["Deploys"])

_NAME = r"^[A-Za-z0-9_-]{1,64}$"


class TargetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    branch: str | None = Field(default=None, max_length=128)
    trigger: Literal["auto", "scheduled", "manual"] | None = None
    schedule: dict[str, Any] | None = None
    host_id: str | None = Field(default=None, max_length=128)
    health_url: str | None = Field(default=None, max_length=1024)
    health_timeout_s: int | None = Field(default=None, ge=5, le=1800)
    auto_rollback: bool | None = None


class DeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit: str | None = Field(default=None, min_length=7, max_length=64,
                               description="Leave out for the newest release; an older "
                                           "commit rolls the target back to it.")


class ReleasePost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit: str = Field(min_length=7, max_length=64)
    branch: str = Field(min_length=1, max_length=128)
    manifest: dict[str, dict[str, Any]]
    built: list[str] = Field(default_factory=list, max_length=50)
    source: str | None = Field(default=None, max_length=256)


class DeployReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["running", "deployed", "rolled_back", "failed"]
    note: str | None = Field(default=None, max_length=500)
    log: list[str] = Field(default_factory=list, max_length=200)
    services: dict[str, str] = Field(default_factory=dict)
    health: str | None = Field(default=None, max_length=32)


@router.get("/projects/{project}/deploys",
            summary="A project's deploy targets, recent releases and runs")
async def project_deploys(
    project: str = Path(max_length=128),
    limit: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await deploys.get_project_deploys(session, principal, project, limit)


@router.put("/projects/{project}/deploys/{target}",
            summary="Create or change a deploy target (people only)")
async def put_target(
    body: TargetUpdate,
    project: str = Path(max_length=128),
    target: str = Path(pattern=_NAME),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await deploys.put_target(session, principal, project, target,
                                    body.model_dump(exclude_unset=True))


@router.post("/projects/{project}/deploys/{target}/release-token",
             summary="A new release token for CI (shown once; people only)")
async def release_token(
    project: str = Path(max_length=128),
    target: str = Path(pattern=_NAME),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await deploys.rotate_release_token(session, principal, project, target)


@router.post("/projects/{project}/deploys/{target}/run",
             summary="Deploy the newest release now, or roll back to a named commit")
async def run_deploy(
    body: DeployRequest,
    project: str = Path(max_length=128),
    target: str = Path(pattern=_NAME),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await deploys.request_deploy(session, principal, project, target, body.commit)


@router.post("/deploys/runs/{run_id}/acknowledge",
             summary="A person has seen a failed or rolled-back deploy")
async def acknowledge_run(
    run_id: str = Path(max_length=40),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await deploys.acknowledge(session, principal, run_id)


@router.post("/deploys/releases", status_code=201,
             summary="CI: a merged commit's images are pushed (X-Veynod-Release-Token)")
async def post_release(
    body: ReleasePost,
    x_veynod_release_token: str | None = Header(default=None, max_length=128),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await deploys.post_release(session, x_veynod_release_token, body.model_dump())


@router.post("/runner/placements/{placement_id}/deploy-report",
             summary="The deploy computer reports a run: running, deployed, rolled_back, failed")
async def deploy_report(
    body: DeployReport,
    placement_id: str = Path(max_length=40),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await deploys.report(session, principal, placement_id, body.model_dump())
