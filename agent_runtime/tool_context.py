"""Execution context for tool runtimes.

``LoopController`` sets this around every ``ToolRuntime.execute`` call so an
adapter can resolve tenant/workspace/project/repository and the Kernel binding
without widening the ``ToolRuntime`` protocol.  Context is per asyncio task;
a worker step never leaks another Run's identity into a tool call.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass(frozen=True)
class ToolContext:
    run_id: str
    tenant_id: str
    workspace_id: str
    product_task_id: str | None = None
    project_id: str | None = None
    repo_path: str | None = None
    trace_id: str | None = None
    kernel: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_run(cls, run: dict[str, Any]) -> "ToolContext":
        kernel = run.get("kernel") or {}
        return cls(
            run_id=str(run.get("run_id") or ""),
            tenant_id=str(run.get("tenant_id") or ""),
            workspace_id=str(run.get("workspace_id") or ""),
            product_task_id=run.get("product_task_id"),
            project_id=run.get("project_id"),
            repo_path=run.get("repo_path"),
            trace_id=run.get("trace_id"),
            kernel=kernel if isinstance(kernel, dict) else {},
        )

    def correlation(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "product_task_id": self.product_task_id,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "kernel_workspace_id": self.kernel.get("workspace_id"),
            "kernel_task_id": self.kernel.get("task_id"),
        }


_CURRENT: ContextVar[ToolContext | None] = ContextVar(
    "veynod_tool_context", default=None
)


def current_tool_context() -> ToolContext | None:
    return _CURRENT.get()


@contextmanager
def tool_context(context: ToolContext | None) -> Iterator[None]:
    token = _CURRENT.set(context)
    try:
        yield
    finally:
        _CURRENT.reset(token)


__all__ = [
    "ToolContext",
    "current_tool_context",
    "tool_context",
]
