"""Configuration for the Veynod Platform gateway.

The gateway owns no domain data. It composes two bounded contexts:

* AgentGraph (Veynod 3.x): identity, workspaces, tasks, audit, console.
* Veynod Kernel (4.x): repositories, AST contracts, symbol locks, gate, commit.

Both backends keep their own database and schema. Only this gateway's SQLite
file stores the mapping between the two task ids.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


@dataclass(frozen=True)
class Settings:
    agentgraph_url: str
    agentgraph_admin_token: str | None
    agentgraph_workspace_id: str | None
    agentgraph_workspace_key: str | None
    kernel_url: str
    kernel_admin_key: str
    kernel_workspace_id: str | None
    db_path: Path
    sync_interval_seconds: float
    request_timeout_seconds: float
    demo_mode: bool

    @property
    def agentgraph_console(self) -> str:
        return f"{self.agentgraph_url.rstrip('/')}/dashboard/"

    @property
    def kernel_console(self) -> str:
        return f"{self.kernel_url.rstrip('/')}/dashboard"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        agentgraph_url=_env("AGENTGRAPH_URL", "http://127.0.0.1:8000") or "http://127.0.0.1:8000",
        agentgraph_admin_token=_env("AGENTGRAPH_ADMIN_TOKEN"),
        agentgraph_workspace_id=_env("AGENTGRAPH_WORKSPACE_ID"),
        agentgraph_workspace_key=_env("AGENTGRAPH_WORKSPACE_KEY"),
        kernel_url=_env("KERNEL_URL", "http://127.0.0.1:8001") or "http://127.0.0.1:8001",
        kernel_admin_key=_env("KERNEL_ADMIN_KEY", "dev-admin") or "dev-admin",
        kernel_workspace_id=_env("KERNEL_WORKSPACE_ID"),
        db_path=Path(_env("PLATFORM_DB_PATH", ".run/platform.db") or ".run/platform.db"),
        sync_interval_seconds=float(_env("PLATFORM_SYNC_INTERVAL_SECONDS", "2") or "2"),
        request_timeout_seconds=float(_env("PLATFORM_REQUEST_TIMEOUT", "30") or "30"),
        demo_mode=(_env("PLATFORM_DEMO_MODE", "true") or "true").lower() in {"1", "true", "yes"},
    )


__all__ = ["Settings", "get_settings"]
