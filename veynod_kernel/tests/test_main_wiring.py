from __future__ import annotations

from pathlib import Path

from app.engine.kernel_service_impl import PostgresKernelEngineService
from app.main import app
from app.mcp_server.syscalls import get_kernel_engine

ROOT = Path(__file__).resolve().parents[1]


def test_engine_dependency_is_overridden_with_the_postgres_implementation() -> None:
    override = app.dependency_overrides[get_kernel_engine]
    assert override.__func__ is PostgresKernelEngineService.from_session_dep.__func__
    assert override.__self__ is PostgresKernelEngineService


def test_main_assembly_stays_under_fifteen_non_blank_lines() -> None:
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    lines = [line for line in source.splitlines() if line.strip()]
    assert len(lines) <= 15, lines


def _route_paths(routes, prefix: str = "") -> set[str]:
    """Flatten routes, including FastAPI >=0.14x lazily included routers."""
    paths: set[str] = set()
    for route in routes:
        if hasattr(route, "path") and (hasattr(route, "methods") or hasattr(route, "app")):
            paths.add(prefix + route.path)
        inner = getattr(route, "original_router", None)
        if inner is not None:
            context = getattr(route, "include_context", None)
            paths |= _route_paths(inner.routes, prefix + (getattr(context, "prefix", "") or ""))
    return paths


def test_all_six_syscalls_and_ops_routes_are_mounted() -> None:
    paths = _route_paths(app.routes)
    assert {
        "/v1/syscalls/pull_task_bundle",
        "/v1/syscalls/submit_patch",
        "/v1/syscalls/request_task_split",
        "/v1/syscalls/escalate_contract_defect",
        "/v1/syscalls/inject_dependency_task",
        "/v1/syscalls/query_symbol_signature",
        "/mcp",
        "/v1/events/stream",
        "/v1/dag",
    } <= paths
