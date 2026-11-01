"""Python lifecycle for the Rust authoritative in-memory scheduler.

The Rust `SchedulerCore` owns the task graph, lock indexes and READY heap. This
module loads a workspace snapshot into it, applies incremental syncs after
committed mutations, and exposes claim/revert calls to the SQLAlchemy
scheduler. Postgres remains the durable log and the single-row ACID fence.

Concurrency notes:
* Rust upserts are versioned by the DB `updated_at`; a sync that read the DB
  before a claim cannot overwrite the newer in-memory LEASED state.
* Workspace loading is guarded by a per-workspace asyncio lock.
* Claims are serialized by the existing per-workspace Postgres advisory lock;
  the Rust decision happens inside that critical section.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import session_scope
from app.engine import rust_bridge
from app.models import DagTask, Workspace

logger = logging.getLogger(__name__)

_LOAD_BATCH = 10_000


def _task_tuple(task: DagTask) -> tuple:
    return (
        str(task.task_id),
        str(task.workspace_id),
        task.type.value,
        task.status.value,
        int(task.priority),
        int(task.created_at.timestamp() * 1_000_000),
        int(task.updated_at.timestamp() * 1_000_000),
        list(task.write_locks),
        list(task.ancestor_locks),
        task.lease_token_hash,
        task.leased_by_runner,
    )


class SchedulerCoreManager:
    def __init__(self) -> None:
        self._core: Any = None
        self._loaded: set[UUID] = set()
        self._load_locks: dict[UUID, asyncio.Lock] = {}
        self.authority_id = f"api-{os.getpid()}-{secrets.token_hex(4)}"
        self._authority_tokens: dict[UUID, int] = {}
        self._authority_until: dict[UUID, float] = {}
        self._authority_negative_until: dict[UUID, float] = {}
        self._authority_locks: dict[UUID, asyncio.Lock] = {}

    # ------------------------------------------------------------------ core
    def _ensure_core(self) -> Any:
        if not rust_bridge.available():
            return None
        if self._core is None:
            self._core = rust_bridge.core().SchedulerCore()
        return self._core

    def available(self) -> bool:
        return rust_bridge.available()

    def reset(self) -> None:
        if self._core is not None:
            self._core.reset()
        self._loaded.clear()
        self._load_locks.clear()
        self._authority_tokens.clear()
        self._authority_until.clear()
        self._authority_negative_until.clear()
        self._authority_locks.clear()

    def stats(self, workspace_id: UUID) -> dict[str, int]:
        core = self._ensure_core()
        if core is None:
            return {"tasks": 0, "ready": 0, "active": 0, "blocked": 0}
        return dict(core.stats(str(workspace_id)))

    def ready_count(self, workspace_id: UUID) -> int:
        core = self._ensure_core()
        if core is None:
            return 0
        return int(core.ready_count(str(workspace_id)))

    # ------------------------------------------------------------- authority
    async def acquire_authority(
        self, session: AsyncSession, workspace_id: UUID
    ) -> bool:
        """Elect one authoritative Rust-core process per repository.

        A per-workspace lock prevents a renewal burst from queueing on the
        scheduler_authorities row: one caller renews, the rest observe the
        in-memory lease and return without a DB round trip.
        """
        lock = self._authority_locks.get(workspace_id)
        if lock is None:
            lock = asyncio.Lock()
            self._authority_locks[workspace_id] = lock
        async with lock:
            return await self._acquire_authority_locked(session, workspace_id)

    async def _acquire_authority_locked(
        self, session: AsyncSession, workspace_id: UUID
    ) -> bool:
        """Elect/renew under the per-workspace authority lock."""
        existing = self._authority_tokens.get(workspace_id)
        now = time.monotonic()
        if existing is not None:
            if self._authority_until.get(workspace_id, 0.0) > now:
                return True
            row = await session.execute(
                text(
                    """
                    UPDATE scheduler_authorities
                    SET lease_expires_at =
                            NOW() + (:lease_seconds * INTERVAL '1 second'),
                        updated_at = NOW()
                    WHERE workspace_id = :workspace_id
                      AND authority_id = :authority_id
                      AND fencing_token = :token
                    RETURNING fencing_token
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "authority_id": self.authority_id,
                    "token": existing,
                    "lease_seconds": settings.scheduler_authority_lease_seconds,
                },
            )
            if row.first() is not None:
                self._authority_until[workspace_id] = now + max(
                    5.0, settings.scheduler_authority_lease_seconds / 2.0
                )
                return True
            self._authority_tokens.pop(workspace_id, None)
            self._authority_until.pop(workspace_id, None)

        if self._authority_negative_until.get(workspace_id, 0.0) > now:
            return False

        token = secrets.randbits(62) + 1
        row = await session.execute(
            text(
                """
                INSERT INTO scheduler_authorities (
                    workspace_id, authority_id, fencing_token, lease_expires_at
                ) VALUES (
                    :workspace_id, :authority_id, :token,
                    NOW() + (:lease_seconds * INTERVAL '1 second')
                )
                ON CONFLICT (workspace_id) DO UPDATE
                SET authority_id = EXCLUDED.authority_id,
                    fencing_token = EXCLUDED.fencing_token,
                    lease_expires_at = EXCLUDED.lease_expires_at,
                    updated_at = NOW()
                WHERE scheduler_authorities.lease_expires_at < NOW()
                RETURNING fencing_token
                """
            ),
            {
                "workspace_id": workspace_id,
                "authority_id": self.authority_id,
                "token": token,
                "lease_seconds": settings.scheduler_authority_lease_seconds,
            },
        )
        if row.first() is None:
            self._authority_negative_until[workspace_id] = now + min(
                5.0, max(1.0, settings.scheduler_authority_lease_seconds / 2.0)
            )
            return False
        self._authority_tokens[workspace_id] = token
        self._authority_until[workspace_id] = now + max(
            5.0, settings.scheduler_authority_lease_seconds / 2.0
        )
        self._authority_negative_until.pop(workspace_id, None)
        return True

    # ------------------------------------------------------------------ load
    async def ensure_loaded(
        self, session: AsyncSession, workspace_id: UUID
    ) -> Any | None:
        core = self._ensure_core()
        if core is None:
            return None
        if workspace_id in self._loaded:
            return core

        lock = self._load_locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            if workspace_id in self._loaded:
                return core

            load_started = datetime.now(timezone.utc)
            last_created = None
            last_id = None
            while True:
                statement = (
                    select(DagTask)
                    .where(DagTask.workspace_id == workspace_id)
                    .order_by(DagTask.created_at.asc(), DagTask.task_id.asc())
                    .limit(_LOAD_BATCH)
                    .execution_options(populate_existing=True)
                )
                if last_created is not None:
                    statement = statement.where(
                        or_(
                            DagTask.created_at > last_created,
                            and_(
                                DagTask.created_at == last_created,
                                DagTask.task_id > last_id,
                            ),
                        )
                    )
                rows = (await session.execute(statement)).scalars().all()
                if rows:
                    core.upsert_tasks([_task_tuple(task) for task in rows])
                    last_created = rows[-1].created_at
                    last_id = rows[-1].task_id
                if len(rows) < _LOAD_BATCH:
                    break

            # Mark loaded first so concurrent admin inserts are synced by their
            # own post-commit sync, then catch anything modified during the load.
            self._loaded.add(workspace_id)
            catch_up = (
                await session.execute(
                    select(DagTask)
                    .where(
                        DagTask.workspace_id == workspace_id,
                        DagTask.updated_at >= load_started,
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalars().all()
            if catch_up:
                core.upsert_tasks([_task_tuple(task) for task in catch_up])

            logger.info(
                "Scheduler core loaded workspace %s (%d initial tasks).",
                workspace_id,
                len(rows),
            )
            return core

    async def reload_workspace(
        self, session: AsyncSession, workspace_id: UUID
    ) -> Any | None:
        core = self._ensure_core()
        if core is None:
            return None
        core.reset_workspace(str(workspace_id))
        self._loaded.discard(workspace_id)
        return await self.ensure_loaded(session, workspace_id)

    async def sync_tasks(
        self, session: AsyncSession, workspace_id: UUID, task_ids: list[UUID]
    ) -> None:
        """Write-through sync after a committed DB mutation.

        Skipped while a workspace is not loaded; the initial load will include
        the rows. Versioning protects against a sync racing a claim.
        """
        core = self._ensure_core()
        if core is None or workspace_id not in self._loaded:
            return
        ids = list(dict.fromkeys(task_ids))
        if not ids:
            return
        try:
            rows = (
                await session.execute(
                    select(DagTask)
                    .where(DagTask.task_id.in_(ids))
                    .execution_options(populate_existing=True)
                )
            ).scalars().all()
            if rows:
                core.upsert_tasks([_task_tuple(task) for task in rows])
        except Exception:  # noqa: BLE001 - the DB fence will resync on failure
            logger.exception("Scheduler core sync failed for workspace %s", workspace_id)

    # ----------------------------------------------------------------- claim
    def claim(
        self,
        workspace_id: UUID,
        supported_types: list[str],
        runner_id: str,
        lease_token_hash: str,
        claim_version: int,
    ) -> str | None:
        core = self._ensure_core()
        if core is None:
            return None
        return core.claim_next(
            str(workspace_id),
            supported_types,
            runner_id,
            lease_token_hash,
            int(claim_version),
        )

    def revert(
        self, workspace_id: UUID, task_id: UUID, lease_token_hash: str
    ) -> bool:
        core = self._ensure_core()
        if core is None:
            return False
        return bool(
            core.revert_claim(str(workspace_id), str(task_id), lease_token_hash)
        )

    async def warmup(self) -> None:
        if not rust_bridge.available():
            return
        async with session_scope() as session:
            workspace_ids = list(
                (
                    await session.execute(select(Workspace.workspace_id))
                ).scalars().all()
            )
        for workspace_id in workspace_ids:
            try:
                async with session_scope() as session:
                    await self.ensure_loaded(session, workspace_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Scheduler core warmup failed for workspace %s", workspace_id
                )


scheduler_core_manager = SchedulerCoreManager()

__all__ = ["SchedulerCoreManager", "scheduler_core_manager"]
