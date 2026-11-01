"""Operator view of the durable cross-domain event inbox (Phase 3)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import require_admin
from app.services import event_dispatch

router = APIRouter(
    prefix="/v1/admin/event-inbox",
    tags=["Event Inbox"],
    dependencies=[Depends(require_admin)],
)


@router.get("/stats", summary="Inbox counts by source/status and dead letters")
async def stats() -> dict[str, Any]:
    return await event_dispatch.inbox_stats()


@router.get("", summary="List recent inbox entries")
async def list_entries(
    status: str | None = Query(default=None),
    source: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return await event_dispatch.list_inbox(status=status, source=source, limit=limit)


@router.post("/{inbox_id}/replay", summary="Replay a failed or dead event")
async def replay(inbox_id: int) -> dict[str, Any]:
    row = await event_dispatch.replay(inbox_id)
    if row is None:
        raise HTTPException(status_code=404, detail="event not found")
    return row


@router.post("/dispatch", summary="Run one dispatch tick now")
async def dispatch_now() -> dict[str, int]:
    return await event_dispatch.dispatch_once()


__all__ = ["router"]
