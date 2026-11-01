"""Code delivery routes.

The Kernel is AgentGraph's code engine: these routes call it directly. The
human console authenticates once through AgentGraph; the Kernel is headless.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, get_principal
from app.database import get_db
from app.services import code_engine

router = APIRouter(prefix="/v1/code", tags=["Code delivery"])


def _raise(exc: code_engine.CodeEngineError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/runs", summary="List code delivery runs")
async def list_code_runs(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> Any:
    return await code_engine.list_runs(session, principal)


@router.post("/runs", summary="Create a code delivery run")
async def create_code_run(
    body: code_engine.CodeRunCreate,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> Any:
    try:
        return await code_engine.create_run(session, principal, body)
    except code_engine.CodeEngineError as exc:
        _raise(exc)


@router.get("/runs/{run_id}", summary="Read a code delivery run")
async def get_code_run(
    run_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> Any:
    try:
        return await code_engine.get_run(session, principal, run_id)
    except code_engine.CodeEngineError as exc:
        _raise(exc)


@router.post("/runs/{run_id}/sync", summary="Refresh and sync a code delivery run")
async def sync_code_run(
    run_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> Any:
    try:
        return await code_engine.get_run(session, principal, run_id)
    except code_engine.CodeEngineError as exc:
        _raise(exc)


@router.get("/runs/{run_id}/audit", summary="AgentGraph audit view for a code run")
async def audit_code_run(
    run_id: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> Any:
    try:
        return await code_engine.audit_run(session, principal, run_id)
    except code_engine.CodeEngineError as exc:
        _raise(exc)


__all__ = ["router"]
