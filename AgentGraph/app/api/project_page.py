"""Former standalone Projects & Delivery console (ADR-018).

Projects are a product concept (a grouping of tasks and repositories) and stay
in the unified console; the separate delivery console is an internal operator
view at ``/dashboard/internal/projects/``.  The old URL redirects.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings

router = APIRouter(include_in_schema=False)
_PAGE = Path(__file__).resolve().parent.parent / "templates" / "projects.html"


def _enabled() -> bool:
    settings = get_settings()
    return settings.internal_views_enabled or not settings.is_production


@router.get("/dashboard/projects/")
async def projects_page() -> RedirectResponse:
    return RedirectResponse("/dashboard/", status_code=302)


@router.get("/dashboard/internal/projects/", response_class=HTMLResponse)
async def internal_projects_page() -> str:
    if not _enabled():
        raise HTTPException(status_code=404, detail="Not found")
    return _PAGE.read_text(encoding="utf-8")


__all__ = ["router"]
