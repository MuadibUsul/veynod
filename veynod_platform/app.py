"""Veynod Platform gateway — DEPRECATED (ADR-018).

This module is a third product surface with its own SQLite store. It is not
part of Veynod: the product is AgentGraph, the Agent Runtime executes, and the
Kernel is headless. Kept for one release as reference; do not deploy it.

One product, two bounded contexts:
  * AgentGraph: identity, workspace, task, audit, console.
  * Veynod Kernel: repository, AST contract, symbol lock, gate, commit.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .clients import AgentGraphClient, IntegrationError, KernelClient
from .config import get_settings
from .service import PlatformService
from .store import PlatformStore

logger = logging.getLogger("veynod.platform")
settings = get_settings()
store = PlatformStore(settings.db_path)
service = PlatformService(
    settings,
    store,
    AgentGraphClient(settings.agentgraph_url, timeout=settings.request_timeout_seconds),
    KernelClient(
        settings.kernel_url,
        settings.kernel_admin_key,
        timeout=settings.request_timeout_seconds,
    ),
)


class CodeRunCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    repo_path: str = Field(min_length=1, max_length=1024)
    symbol: str = Field(min_length=1, max_length=512)
    test_symbol: str = Field(min_length=1, max_length=512)
    spec: str = Field(min_length=10, max_length=20_000)
    priority: int = Field(default=100, ge=0, le=999)


async def _sync_loop() -> None:
    while True:
        await asyncio.sleep(settings.sync_interval_seconds)
        try:
            await service.sync_active_runs()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("platform sync iteration failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init()
    task = asyncio.create_task(_sync_loop(), name="veynod-platform-sync")
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="Veynod Platform",
    version="0.1.0",
    description=(
        "Composition gateway: AgentGraph control plane + Veynod Kernel code plane. "
        "The two products keep separate databases and schemas."
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_TEMPLATE = (Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _TEMPLATE.replace("{{ agentgraph_console }}", settings.agentgraph_console).replace(
        "{{ kernel_console }}", settings.kernel_console
    )


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return await service.health()


@app.get("/v1/platform/health")
async def platform_health() -> dict[str, Any]:
    return await service.health()


@app.get("/v1/platform/config")
async def platform_config() -> dict[str, Any]:
    return {
        "agentgraph_console": settings.agentgraph_console,
        "kernel_console": settings.kernel_console,
        "agentgraph_url": settings.agentgraph_url,
        "kernel_url": settings.kernel_url,
        "kernel_workspace_id": settings.kernel_workspace_id,
        "boundary": {
            "agentgraph": ["identity", "workspace", "task", "audit", "console"],
            "kernel": ["repo", "artifact", "symbol-lock", "gate", "commit"],
        },
    }


@app.get("/v1/platform/code-runs")
async def list_code_runs() -> list[dict[str, Any]]:
    return store.list_runs()


@app.post("/v1/platform/code-runs")
async def create_code_run(body: CodeRunCreate) -> dict[str, Any]:
    try:
        return await service.create_code_run(
            title=body.title,
            repo_path=body.repo_path,
            symbol=body.symbol,
            test_symbol=body.test_symbol,
            spec=body.spec,
            priority=body.priority,
        )
    except IntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/v1/platform/code-runs/{run_id}")
async def get_code_run(run_id: str) -> dict[str, Any]:
    try:
        return await service.refresh_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id}") from exc
    except IntegrationError as exc:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}") from exc
        run["sync_error"] = str(exc)
        return run
    except Exception as exc:  # noqa: BLE001 - status must stay readable
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}") from exc
        run["sync_error"] = str(exc)[:1000]
        return run


@app.post("/v1/platform/code-runs/{run_id}/sync")
async def sync_code_run(run_id: str) -> dict[str, Any]:
    try:
        return await service.sync_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id}") from exc
    except IntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/v1/platform/code-runs/{run_id}/audit")
async def audit_code_run(run_id: str) -> dict[str, Any]:
    try:
        return await service.audit_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id}") from exc
    except IntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


__all__ = ["app"]
