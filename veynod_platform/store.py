"""SQLite mapping store.

This is deliberately not a second domain database. It only keeps the
AgentGraph task <-> Kernel task mapping, the workspace cache and the small
amount of state needed for idempotent sync.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlatformStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS code_runs (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    repo_path TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    test_symbol TEXT NOT NULL,
                    spec TEXT NOT NULL,
                    agentgraph_workspace_id TEXT NOT NULL,
                    agentgraph_task_key TEXT NOT NULL,
                    agentgraph_task_id TEXT,
                    kernel_workspace_id TEXT NOT NULL,
                    kernel_task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    kernel_status TEXT,
                    commit_sha TEXT,
                    merge_mode TEXT,
                    agentgraph_synced INTEGER NOT NULL DEFAULT 0,
                    sync_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_code_runs_status
                    ON code_runs(status, updated_at);
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kernel_workspaces (
                    repo_path TEXT PRIMARY KEY,
                    kernel_workspace_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------ kv
    def kv_get(self, key: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM kv WHERE key = ?", (key,)
            ).fetchone()
            return str(row["value"]) if row else None

    def kv_put(self, key: str, value: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO kv(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (key, value, _now()),
            )

    # ------------------------------------------------------- kernel mapping
    def kernel_workspace_get(self, repo_path: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT kernel_workspace_id FROM kernel_workspaces WHERE repo_path = ?",
                (repo_path,),
            ).fetchone()
            return str(row["kernel_workspace_id"]) if row else None

    def kernel_workspace_put(self, repo_path: str, workspace_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO kernel_workspaces(repo_path, kernel_workspace_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(repo_path) DO UPDATE SET
                    kernel_workspace_id=excluded.kernel_workspace_id,
                    updated_at=excluded.updated_at
                """,
                (repo_path, workspace_id, _now()),
            )

    # ------------------------------------------------------------- runs
    def create_run(self, **values: Any) -> dict[str, Any]:
        values.setdefault("created_at", _now())
        values.setdefault("updated_at", _now())
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self._connection() as connection:
            connection.execute(
                f"INSERT INTO code_runs({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
        return self.get_run(str(values["id"])) or {}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM code_runs WHERE id = ?", (run_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM code_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def update_run(self, run_id: str, **values: Any) -> dict[str, Any] | None:
        values["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connection() as connection:
            connection.execute(
                f"UPDATE code_runs SET {assignments} WHERE id = ?",
                (*values.values(), run_id),
            )
        return self.get_run(run_id)


__all__ = ["PlatformStore"]
