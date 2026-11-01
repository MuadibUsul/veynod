"""Product console page for projects, execution graph and delivery."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(include_in_schema=False)
_PAGE = Path(__file__).resolve().parent.parent / "templates" / "projects.html"


@router.get("/dashboard/projects/", response_class=HTMLResponse)
async def projects_page() -> str:
    return _PAGE.read_text(encoding="utf-8")


__all__ = ["router"]
