"""Former standalone Code console.

ADR-018: Code is a section of a task, not a product.  The old URL redirects
into the unified console; the detailed console survives only as an internal
operator view at ``/dashboard/internal/code/`` and only when enabled (never in
production unless explicitly turned on).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings

router = APIRouter(include_in_schema=False)
_PAGE = Path(__file__).resolve().parent.parent / "templates" / "code.html"


def _enabled() -> bool:
    settings = get_settings()
    return settings.internal_views_enabled or not settings.is_production


@router.get("/dashboard/code/")
async def code_page() -> RedirectResponse:
    return RedirectResponse("/dashboard/", status_code=302)


@router.get("/dashboard/internal/code/", response_class=HTMLResponse)
async def internal_code_page() -> str:
    if not _enabled():
        raise HTTPException(status_code=404, detail="Not found")
    return _PAGE.read_text(encoding="utf-8")


__all__ = ["router"]
