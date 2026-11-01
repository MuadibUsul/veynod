"""The human console's Agent Runs page (static, session-token based)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(include_in_schema=False)
_PAGE = Path(__file__).resolve().parent.parent / "templates" / "agent_runs.html"


@router.get("/dashboard/agent-runs/", response_class=HTMLResponse)
async def agent_runs_page() -> str:
    return _PAGE.read_text(encoding="utf-8")


__all__ = ["router"]
