"""Reusable detached Git worktrees for the gate.

Creating and removing a worktree per submission contends on Git metadata and
pays syscall/lock overhead. The gate borrows from a small per-workspace pool,
resets the worktree with checkout/reset/clean, and never re-runs
``git worktree add/remove`` in the request path.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.database import session_scope
from app.models import Workspace
from app.services.git_ops import GitRepository

logger = logging.getLogger(__name__)


class WorkspaceWorktreePool:
    def __init__(
        self, workspace_id: UUID, repo: GitRepository, size: int
    ) -> None:
        self.workspace_id = workspace_id
        self.repo = repo
        self.size = max(1, size)
        self.root = Path(
            tempfile.mkdtemp(prefix=f"veynod-gate-pool-{workspace_id}-")
        )
        self._queue: asyncio.Queue[Path] = asyncio.Queue()
        self._paths: list[Path] = []
        self._started = False
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            head = await self.repo.head_sha()
            for index in range(self.size):
                path = self.root / f"wt-{index}"
                # Sequential adds on purpose: concurrent worktree add can
                # contend on Git's metadata locks.
                await self.repo.add_worktree(path, head)
                self._paths.append(path)
                self._queue.put_nowait(path)
            self._started = True

    async def acquire(self, base_sha: str) -> Path:
        await self.start()
        path = await self._queue.get()
        try:
            await self.repo.reset_worktree(path, base_sha)
        except Exception:
            self._queue.put_nowait(path)
            raise
        return path

    async def release(self, path: Path) -> None:
        self._queue.put_nowait(path)

    async def close(self) -> None:
        for path in self._paths:
            try:
                await self.repo.remove_worktree(path)
            except Exception:  # noqa: BLE001 - best effort on shutdown
                logger.exception("Failed to remove pooled worktree %s", path)
        shutil.rmtree(self.root, ignore_errors=True)
        self._paths.clear()
        self._queue = asyncio.Queue()
        self._started = False


class WorktreePoolManager:
    def __init__(self) -> None:
        self._pools: dict[tuple[UUID, str], WorkspaceWorktreePool] = {}
        self._lock = asyncio.Lock()

    async def get(
        self, workspace: Workspace, size: int | None = None
    ) -> WorkspaceWorktreePool:
        key = (workspace.workspace_id, workspace.repo_path)
        async with self._lock:
            pool = self._pools.get(key)
            if pool is None:
                pool = WorkspaceWorktreePool(
                    workspace.workspace_id,
                    GitRepository(workspace.repo_path, workspace.main_branch),
                    size or settings.gate_worktree_pool_size,
                )
                self._pools[key] = pool
        await pool.start()
        return pool

    async def warmup(self) -> None:
        async with session_scope() as session:
            workspaces = (
                await session.execute(select(Workspace))
            ).scalars().all()
        for workspace in workspaces:
            try:
                await self.get(workspace)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Worktree pool warmup failed for workspace %s",
                    workspace.workspace_id,
                )

    async def close_all(self) -> None:
        async with self._lock:
            pools = list(self._pools.values())
            self._pools.clear()
        for pool in pools:
            await pool.close()


worktree_pool_manager = WorktreePoolManager()

__all__ = ["WorkspaceWorktreePool", "WorktreePoolManager", "worktree_pool_manager"]
