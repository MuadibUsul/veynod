"""The human console's Code page.

Served as a small static document so it can live beside the Next.js export
without a second frontend build. It uses the same AgentGraph session token.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(include_in_schema=False)
_PAGE = Path(__file__).resolve().parent.parent / "templates" / "code.html"


@router.get("/dashboard/code/", response_class=HTMLResponse)
async def code_page() -> str:
    return _PAGE.read_text(encoding="utf-8")


__all__ = ["router"]
