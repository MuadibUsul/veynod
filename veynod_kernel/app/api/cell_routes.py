"""Cell and repository placement administration (Phase 4)."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_routes import require_admin
from app.database import get_db_session
from app.services import cell_registry

router = APIRouter(
    prefix="/v1/admin",
    tags=["Cells"],
    dependencies=[Depends(require_admin)],
)


class CellCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    endpoint: str | None = Field(default=None, max_length=1024)
    capacity: dict[str, Any] = Field(default_factory=dict)


class RepositoryMove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cell_id: UUID
    expected_epoch: int | None = Field(default=None, ge=1)


@router.get("/cells")
async def list_cells(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[dict[str, Any]]:
    return await cell_registry.list_cells(session)


@router.post("/cells", status_code=status.HTTP_201_CREATED)
async def create_cell(
    body: CellCreate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    try:
        return await cell_registry.create_cell(
            session,
            name=body.name,
            endpoint=body.endpoint,
            capacity=body.capacity,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/repositories")
async def list_repositories(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[dict[str, Any]]:
    return await cell_registry.list_repositories(session)


@router.post("/repositories/{workspace_id}/move")
async def move_repository(
    workspace_id: UUID,
    body: RepositoryMove,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    try:
        row = await cell_registry.move_repository(
            session,
            workspace_id,
            cell_id=body.cell_id,
            expected_epoch=body.expected_epoch,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="repository not found")
    return row


__all__ = ["router"]
