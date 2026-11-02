"""Durable Agent Runtime store (Phase 2A).

PostgreSQL schema ``agent_runtime`` with runs, steps, checkpoints, events,
transactional outbox and idempotency records.  No model/tool execution here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .errors import IdempotencyConflict, LeaseLost, RunConflict, RunNotFound
from .states import TERMINAL, RunState, validate_transition

logger = logging.getLogger("agent_runtime.store")

_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _split_sql(sql: str) -> list[str]:
    lines = [
        line for line in sql.splitlines()
        if not line.strip().startswith("--")
    ]
    return [part.strip() for part in "\n".join(lines).split(";") if part.strip()]


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _check_fence(row: Any, fence: int | None, run_id: Any) -> None:
    """Reject a write from a worker whose lease was taken over.

    ``lease_epoch`` is incremented on every claim, so the epoch a worker holds
    is a fencing token: once another worker claims the Run, the old worker's
    epoch no longer matches and every fenced write fails with LeaseLost.
    """
    if fence is None:
        return
    if row["lease_token_hash"] is None or int(row["lease_epoch"]) != int(fence):
        raise LeaseLost(f"run {run_id} lease was taken over (fencing)")


class RunStore:
    def __init__(self, database_url: str, schema: str = "agent_runtime") -> None:
        if not _SCHEMA_RE.match(schema):
            raise ValueError(f"invalid runtime schema name {schema!r}")
        self.schema = schema
        self._engine = create_async_engine(
            database_url, pool_pre_ping=True, pool_size=5, max_overflow=5
        )
        self._sessions = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def close(self) -> None:
        await self._engine.dispose()

    # --------------------------------------------------------------- schema
    async def init_schema(self, reset: bool = False) -> None:
        schema = self.schema
        async with self._engine.begin() as conn:
            if reset:
                await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
            await conn.execute(
                text(
                    f"""
                    CREATE TABLE IF NOT EXISTS {schema}.schema_migrations (
                        version TEXT PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
            )
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                version = path.stem
                applied = await conn.scalar(
                    text(
                        f"SELECT 1 FROM {schema}.schema_migrations "
                        "WHERE version = :version"
                    ),
                    {"version": version},
                )
                if applied:
                    continue
                migration = path.read_text(encoding="utf-8").replace(
                    "{{schema}}", schema
                )
                for statement in _split_sql(migration):
                    await conn.execute(text(statement))
                await conn.execute(
                    text(
                        f"INSERT INTO {schema}.schema_migrations (version) "
                        "VALUES (:version) ON CONFLICT DO NOTHING"
                    ),
                    {"version": version},
                )

    # --------------------------------------------------------------- helpers
    async def _publish(
        self,
        session: AsyncSession,
        run_id: UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        row = (
            await session.execute(
                text(
                    f"""
                    INSERT INTO {self.schema}.run_events
                        (run_id, event_type, payload)
                    VALUES (:run_id, :event_type, CAST(:payload AS JSONB))
                    RETURNING event_seq
                    """
                ),
                {
                    "run_id": run_id,
                    "event_type": event_type,
                    "payload": _json(payload),
                },
            )
        ).scalar_one()
        await session.execute(
            text(
                f"""
                INSERT INTO {self.schema}.runtime_outbox
                    (event_seq, run_id, payload)
                VALUES (
                    :event_seq, :run_id,
                    CAST(:payload AS JSONB)
                )
                """
            ),
            {
                "event_seq": int(row),
                "run_id": run_id,
                "payload": _json(
                    {
                        "event_seq": int(row),
                        "run_id": str(run_id),
                        "event_type": event_type,
                        "payload": payload,
                    }
                ),
            },
        )
        return int(row)

    async def _idempotent(
        self,
        session: AsyncSession,
        key: str,
        run_id: UUID | None,
        command: str,
    ) -> dict[str, Any] | None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": key},
        )
        row = (
            await session.execute(
                text(
                    f"""
                    SELECT run_id, command, result
                    FROM {self.schema}.runtime_idempotency
                    WHERE key = :key
                    """
                ),
                {"key": key},
            )
        ).mappings().first()
        if row is None:
            return None
        if run_id is not None and str(row["run_id"]) != str(run_id):
            raise IdempotencyConflict(
                f"idempotency key {key!r} is bound to a different run"
            )
        if row["command"] != command:
            raise IdempotencyConflict(
                f"idempotency key {key!r} is bound to a different command"
            )
        return _loads(row["result"], {})

    async def _remember(
        self,
        session: AsyncSession,
        key: str,
        run_id: UUID,
        command: str,
        result: dict[str, Any],
    ) -> None:
        await session.execute(
            text(
                f"""
                INSERT INTO {self.schema}.runtime_idempotency
                    (key, run_id, command, result)
                VALUES (:key, :run_id, :command, CAST(:result AS JSONB))
                """
            ),
            {
                "key": key,
                "run_id": run_id,
                "command": command,
                "result": _json(result),
            },
        )

    @staticmethod
    def _run_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        data["run_id"] = str(data["run_id"])
        data["tenant_id"] = str(data["tenant_id"])
        data["workspace_id"] = str(data["workspace_id"])
        if data.get("workflow_id"):
            data["workflow_id"] = str(data["workflow_id"])
        if data.get("parent_run_id"):
            data["parent_run_id"] = str(data["parent_run_id"])
        data["budget"] = _loads(data.get("budget"), {})
        data["kernel"] = _loads(data.get("kernel"), {})
        return data

    # ---------------------------------------------------------------- create
    async def create_run(
        self,
        *,
        tenant_id: UUID | str,
        workspace_id: UUID | str,
        product_task_id: str | None = None,
        goal: str | None = None,
        workflow_id: UUID | str | None = None,
        workflow_version: int | None = None,
        budget: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
        parent_run_id: UUID | str | None = None,
        delegation_key: str | None = None,
        project_id: str | None = None,
        repo_path: str | None = None,
        trace_id: str | None = None,
        kernel: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_id = uuid4()
        correlation = correlation_id or f"corr_{uuid4().hex}"
        trace = trace_id or uuid4().hex
        async with self._sessions() as session:
            if idempotency_key:
                existing = await self._idempotent(
                    session, idempotency_key, None, "create_run"
                )
                if existing is not None:
                    await session.commit()
                    return existing
            await session.execute(
                text(
                    f"""
                    INSERT INTO {self.schema}.agent_runs
                        (run_id, tenant_id, workspace_id, product_task_id,
                         goal, workflow_id, workflow_version, status, version,
                         budget, correlation_id, parent_run_id, delegation_key,
                         project_id, repo_path, trace_id, kernel)
                    VALUES
                        (:run_id, :tenant_id, :workspace_id, :product_task_id,
                         :goal, :workflow_id, :workflow_version, 'CREATED', 1,
                         CAST(:budget AS JSONB), :correlation_id,
                         :parent_run_id, :delegation_key,
                         :project_id, :repo_path, :trace_id,
                         CAST(:kernel AS JSONB))
                    """
                ),
                {
                    "run_id": run_id,
                    "tenant_id": str(tenant_id),
                    "workspace_id": str(workspace_id),
                    "product_task_id": product_task_id,
                    "goal": goal,
                    "workflow_id": str(workflow_id) if workflow_id else None,
                    "workflow_version": workflow_version,
                    "budget": _json(budget or {}),
                    "correlation_id": correlation,
                    "parent_run_id": str(parent_run_id) if parent_run_id else None,
                    "delegation_key": delegation_key,
                    "project_id": project_id,
                    "repo_path": repo_path,
                    "trace_id": trace,
                    "kernel": _json(kernel or {}),
                },
            )
            event_seq = await self._publish(
                session,
                run_id,
                "AgentRunCreated",
                {
                    "run_id": str(run_id),
                    "workspace_id": str(workspace_id),
                    "correlation_id": correlation,
                    "trace_id": trace,
                    "product_task_id": product_task_id,
                },
            )
            result = {
                "run_id": str(run_id),
                "status": RunState.CREATED.value,
                "version": 1,
                "event_seq": event_seq,
            }
            if idempotency_key:
                await self._remember(
                    session, idempotency_key, run_id, "create_run", result
                )
            await session.commit()
        run = await self.get_run(run_id)
        run["event_seq"] = event_seq
        return run

    # ------------------------------------------------------------------ read
    async def get_run(self, run_id: UUID | str) -> dict[str, Any]:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT * FROM {self.schema}.agent_runs "
                        "WHERE run_id = :run_id"
                    ),
                    {"run_id": str(run_id)},
                )
            ).mappings().first()
        if row is None:
            raise RunNotFound(f"run {run_id} not found")
        return self._run_dict(row)

    async def list_events(
        self, run_id: UUID | str, after_seq: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT event_seq, run_id, event_type, payload, created_at
                        FROM {self.schema}.run_events
                        WHERE run_id = :run_id AND event_seq > :after
                        ORDER BY event_seq ASC
                        LIMIT :limit
                        """
                    ),
                    {"run_id": str(run_id), "after": after_seq, "limit": limit},
                )
            ).mappings().all()
        return [
            {
                "event_seq": int(row["event_seq"]),
                "run_id": str(row["run_id"]),
                "event_type": row["event_type"],
                "payload": _loads(row["payload"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def latest_event_seq(self) -> int:
        async with self._sessions() as session:
            value = await session.scalar(
                text(f"SELECT COALESCE(MAX(event_seq), 0) FROM {self.schema}.run_events")
            )
        return int(value or 0)

    async def list_events_global(
        self, after_seq: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Events across all Runtime runs, for the AgentGraph inbox."""
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT e.event_seq, e.run_id, e.event_type, e.payload,
                               e.created_at, r.tenant_id, r.workspace_id,
                               r.product_task_id, r.status AS run_status
                        FROM {self.schema}.run_events e
                        JOIN {self.schema}.agent_runs r ON r.run_id = e.run_id
                        WHERE e.event_seq > :after
                        ORDER BY e.event_seq ASC
                        LIMIT :limit
                        """
                    ),
                    {"after": after_seq, "limit": max(1, min(1000, limit))},
                )
            ).mappings().all()
        return [
            {
                "event_seq": int(row["event_seq"]),
                "run_id": str(row["run_id"]),
                "event_type": row["event_type"],
                "payload": _loads(row["payload"], {}),
                "created_at": row["created_at"],
                "tenant_id": str(row["tenant_id"]),
                "workspace_id": str(row["workspace_id"]),
                "product_task_id": row["product_task_id"],
                "run_status": row["run_status"],
            }
            for row in rows
        ]

    async def list_checkpoints(self, run_id: UUID | str) -> list[dict[str, Any]]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT checkpoint_id, version, state, created_at
                        FROM {self.schema}.run_checkpoints
                        WHERE run_id = :run_id
                        ORDER BY version ASC
                        """
                    ),
                    {"run_id": str(run_id)},
                )
            ).mappings().all()
        return [
            {
                "checkpoint_id": str(row["checkpoint_id"]),
                "version": row["version"],
                "state": _loads(row["state"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def list_steps(
        self, run_id: UUID | str, limit: int = 50
    ) -> list[dict[str, Any]]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT step_id, step_no, kind, status, payload,
                               idempotency_key, created_at
                        FROM {self.schema}.agent_steps
                        WHERE run_id = :run_id
                        ORDER BY step_no DESC
                        LIMIT :limit
                        """
                    ),
                    {"run_id": str(run_id), "limit": max(1, min(500, limit))},
                )
            ).mappings().all()
        return [
            {
                "step_id": str(row["step_id"]),
                "step_no": int(row["step_no"]),
                "kind": row["kind"],
                "status": row["status"],
                "payload": _loads(row["payload"], {}),
                "idempotency_key": row["idempotency_key"],
                "created_at": row["created_at"],
            }
            for row in reversed(rows)
        ]

    async def list_children(self, parent_run_id: UUID | str) -> list[dict[str, Any]]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT * FROM {self.schema}.agent_runs
                        WHERE parent_run_id = :parent_run_id
                        ORDER BY created_at ASC
                        """
                    ),
                    {"parent_run_id": str(parent_run_id)},
                )
            ).mappings().all()
        return [self._run_dict(row) for row in rows]

    async def get_child_by_delegation(
        self, parent_run_id: UUID | str, delegation_key: str
    ) -> dict[str, Any] | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        f"""
                        SELECT * FROM {self.schema}.agent_runs
                        WHERE parent_run_id = :parent_run_id
                          AND delegation_key = :delegation_key
                        """
                    ),
                    {
                        "parent_run_id": str(parent_run_id),
                        "delegation_key": delegation_key,
                    },
                )
            ).mappings().first()
        return self._run_dict(row) if row is not None else None

    async def list_runs_by_status(
        self, status: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT * FROM {self.schema}.agent_runs
                        WHERE status = :status
                        ORDER BY updated_at ASC
                        LIMIT :limit
                        """
                    ),
                    {"status": status, "limit": max(1, min(1000, limit))},
                )
            ).mappings().all()
        return [self._run_dict(row) for row in rows]

    # ------------------------------------------------------------ transition
    async def start_run(self, run_id: UUID | str) -> dict[str, Any]:
        """Advance a newly created run through the initialization chain to RUNNING."""
        order = ("CREATED", "QUEUED", "INITIALIZING", "PLANNING", "READY", "RUNNING")
        run = await self.get_run(run_id)
        status = run["status"]
        if status == "RUNNING":
            return run
        if status not in order:
            raise RunConflict(f"run {run_id} is {status}, cannot start")
        for target in order[order.index(status) + 1 :]:
            await self.transition(
                run_id,
                target,
                expected_version=run["version"],
                reason="start requested",
                actor="api",
            )
            run = await self.get_run(run_id)
        return run

    async def transition(
        self,
        run_id: UUID | str,
        target: str | RunState,
        *,
        expected_version: int | None = None,
        reason: str = "",
        actor: str = "runtime",
        idempotency_key: str | None = None,
        fence: int | None = None,
    ) -> dict[str, Any]:
        destination = target.value if isinstance(target, RunState) else str(target)
        command = f"transition:{destination}"
        async with self._sessions() as session:
            if idempotency_key:
                existing = await self._idempotent(
                    session, idempotency_key, UUID(str(run_id)), command
                )
                if existing is not None:
                    await session.commit()
                    return existing
            row = (
                await session.execute(
                    text(
                        f"""
                        SELECT status, version, lease_epoch, lease_token_hash
                        FROM {self.schema}.agent_runs
                        WHERE run_id = :run_id
                        FOR UPDATE
                        """
                    ),
                    {"run_id": str(run_id)},
                )
            ).mappings().first()
            if row is None:
                raise RunNotFound(f"run {run_id} not found")
            _check_fence(row, fence, run_id)
            current = str(row["status"])
            version = int(row["version"])
            validate_transition(current, destination)
            if expected_version is not None and version != expected_version:
                raise RunConflict(
                    f"expected version {expected_version}, current {version}"
                )
            new_version = version + 1
            terminated = destination in {state.value for state in TERMINAL}
            started = destination == RunState.RUNNING.value
            await session.execute(
                text(
                    f"""
                    UPDATE {self.schema}.agent_runs
                    SET status = :status,
                        version = :version,
                        updated_at = NOW(),
                        started_at = CASE
                            WHEN :started AND started_at IS NULL THEN NOW()
                            ELSE started_at
                        END,
                        finished_at = CASE
                            WHEN :terminated THEN COALESCE(finished_at, NOW())
                            ELSE finished_at
                        END,
                        last_error = CASE
                            WHEN :failed THEN :reason
                            ELSE last_error
                        END
                    WHERE run_id = :run_id AND version = :old_version
                    """
                ),
                {
                    "status": destination,
                    "version": new_version,
                    "started": started,
                    "terminated": terminated,
                    "failed": destination == RunState.FAILED.value,
                    "reason": reason[:8000],
                    "run_id": str(run_id),
                    "old_version": version,
                },
            )
            event_seq = await self._publish(
                session,
                UUID(str(run_id)),
                "AgentRunStateChanged",
                {
                    "run_id": str(run_id),
                    "from": current,
                    "to": destination,
                    "version": new_version,
                    "reason": reason[:8000],
                    "actor": actor,
                },
            )
            result = {
                "run_id": str(run_id),
                "status": destination,
                "previous_status": current,
                "version": new_version,
                "event_seq": event_seq,
            }
            if idempotency_key:
                await self._remember(
                    session, idempotency_key, UUID(str(run_id)), command, result
                )
            await session.commit()
        return result

    # ------------------------------------------------------------ checkpoint
    async def checkpoint(
        self,
        run_id: UUID | str,
        state: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        fence: int | None = None,
    ) -> dict[str, Any]:
        async with self._sessions() as session:
            locked = (
                await session.execute(
                    text(
                        f"SELECT run_id, lease_epoch, lease_token_hash "
                        f"FROM {self.schema}.agent_runs "
                        "WHERE run_id = :run_id FOR UPDATE"
                    ),
                    {"run_id": str(run_id)},
                )
            ).mappings().first()
            if locked is None:
                raise RunNotFound(f"run {run_id} not found")
            _check_fence(locked, fence, run_id)
            if idempotency_key:
                existing = (
                    await session.execute(
                        text(
                            f"""
                            SELECT checkpoint_id, version, state, created_at
                            FROM {self.schema}.run_checkpoints
                            WHERE run_id = :run_id AND idempotency_key = :key
                            """
                        ),
                        {"run_id": str(run_id), "key": idempotency_key},
                    )
                ).mappings().first()
                if existing is not None:
                    await session.commit()
                    return {
                        "checkpoint_id": str(existing["checkpoint_id"]),
                        "version": int(existing["version"]),
                        "state": _loads(existing["state"], {}),
                        "created_at": existing["created_at"],
                    }
            version = (
                await session.scalar(
                    text(
                        f"""
                        SELECT COALESCE(MAX(version), 0) + 1
                        FROM {self.schema}.run_checkpoints
                        WHERE run_id = :run_id
                        """
                    ),
                    {"run_id": str(run_id)},
                )
            ) or 1
            checkpoint_id = uuid4()
            await session.execute(
                text(
                    f"""
                    INSERT INTO {self.schema}.run_checkpoints
                        (checkpoint_id, run_id, version, state, idempotency_key)
                    VALUES (
                        :checkpoint_id, :run_id, :version,
                        CAST(:state AS JSONB), :key
                    )
                    """
                ),
                {
                    "checkpoint_id": checkpoint_id,
                    "run_id": str(run_id),
                    "version": int(version),
                    "state": _json(state),
                    "key": idempotency_key,
                },
            )
            event_seq = await self._publish(
                session,
                UUID(str(run_id)),
                "AgentRunCheckpointed",
                {
                    "run_id": str(run_id),
                    "checkpoint_id": str(checkpoint_id),
                    "version": int(version),
                },
            )
            await session.commit()
        return {
            "checkpoint_id": str(checkpoint_id),
            "version": int(version),
            "state": state,
            "event_seq": event_seq,
        }


    # ------------------------------------------------------------ execution
    async def next_step_no(self, run_id: UUID | str) -> int:
        async with self._sessions() as session:
            value = await session.scalar(
                text(
                    f"""
                    SELECT COALESCE(MAX(step_no), 0) + 1
                    FROM {self.schema}.agent_steps
                    WHERE run_id = :run_id
                    """
                ),
                {"run_id": str(run_id)},
            )
        return int(value or 1)

    async def record_step(
        self,
        run_id: UUID | str,
        step_no: int,
        kind: str,
        status: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        fence: int | None = None,
    ) -> dict[str, Any]:
        step_id = uuid4()
        async with self._sessions() as session:
            if fence is not None:
                locked = (
                    await session.execute(
                        text(
                            f"SELECT lease_epoch, lease_token_hash "
                            f"FROM {self.schema}.agent_runs "
                            "WHERE run_id = :run_id FOR UPDATE"
                        ),
                        {"run_id": str(run_id)},
                    )
                ).mappings().first()
                if locked is None:
                    raise RunNotFound(f"run {run_id} not found")
                _check_fence(locked, fence, run_id)
            row = (
                await session.execute(
                    text(
                        f"""
                        INSERT INTO {self.schema}.agent_steps
                            (step_id, run_id, step_no, kind, status, payload,
                             idempotency_key)
                        VALUES (
                            :step_id, :run_id, :step_no, :kind, :status,
                            CAST(:payload AS JSONB), :idempotency_key
                        )
                        ON CONFLICT DO NOTHING
                        RETURNING step_id, step_no, kind, status, payload,
                                  idempotency_key, created_at
                        """
                    ),
                    {
                        "step_id": step_id,
                        "run_id": str(run_id),
                        "step_no": step_no,
                        "kind": kind,
                        "status": status,
                        "payload": _json(payload or {}),
                        "idempotency_key": idempotency_key,
                    },
                )
            ).mappings().first()
            if row is None and idempotency_key:
                row = (
                    await session.execute(
                        text(
                            f"""
                            SELECT step_id, step_no, kind, status, payload,
                                   idempotency_key, created_at
                            FROM {self.schema}.agent_steps
                            WHERE run_id = :run_id
                              AND idempotency_key = :idempotency_key
                            """
                        ),
                        {
                            "run_id": str(run_id),
                            "idempotency_key": idempotency_key,
                        },
                    )
                ).mappings().first()
            await session.commit()
        if row is None:
            raise RunConflict(
                f"step {step_no} already exists for run {run_id}"
            )
        return {
            "step_id": str(row["step_id"]),
            "step_no": int(row["step_no"]),
            "kind": row["kind"],
            "status": row["status"],
            "payload": _loads(row["payload"], {}),
            "idempotency_key": row["idempotency_key"],
            "created_at": row["created_at"],
        }

    async def begin_invocation(
        self,
        run_id: UUID | str,
        *,
        kind: str,
        name: str,
        idempotency_key: str,
        request: dict[str, Any] | None = None,
        step_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        invocation_id = uuid4()
        async with self._sessions() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": idempotency_key},
            )
            existing = (
                await session.execute(
                    text(
                        f"""
                        SELECT * FROM {self.schema}.invocations
                        WHERE idempotency_key = :key
                        """
                    ),
                    {"key": idempotency_key},
                )
            ).mappings().first()
            if existing is not None:
                await session.commit()
                return self._invocation_dict(existing)
            row = (
                await session.execute(
                    text(
                        f"""
                        INSERT INTO {self.schema}.invocations
                            (invocation_id, run_id, step_id, kind, name,
                             idempotency_key, request, status)
                        VALUES (
                            :invocation_id, :run_id, :step_id, :kind, :name,
                            :key, CAST(:request AS JSONB), 'PENDING'
                        )
                        RETURNING *
                        """
                    ),
                    {
                        "invocation_id": invocation_id,
                        "run_id": str(run_id),
                        "step_id": str(step_id) if step_id else None,
                        "kind": kind,
                        "name": name,
                        "key": idempotency_key,
                        "request": _json(request or {}),
                    },
                )
            ).mappings().first()
            await session.commit()
        return self._invocation_dict(row)

    async def finish_invocation(
        self,
        invocation_id: UUID | str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
        fence: int | None = None,
    ) -> dict[str, Any]:
        async with self._sessions() as session:
            if fence is not None:
                owner = await session.scalar(
                    text(
                        f"SELECT run_id FROM {self.schema}.invocations "
                        "WHERE invocation_id = :invocation_id"
                    ),
                    {"invocation_id": str(invocation_id)},
                )
                if owner is None:
                    raise RunNotFound(f"invocation {invocation_id} not found")
                locked = (
                    await session.execute(
                        text(
                            f"SELECT lease_epoch, lease_token_hash "
                            f"FROM {self.schema}.agent_runs "
                            "WHERE run_id = :run_id FOR UPDATE"
                        ),
                        {"run_id": str(owner)},
                    )
                ).mappings().first()
                if locked is None:
                    raise RunNotFound(f"run {owner} not found")
                _check_fence(locked, fence, str(owner))
            row = (
                await session.execute(
                    text(
                        f"""
                        UPDATE {self.schema}.invocations
                        SET status = :status,
                            result = CAST(:result AS JSONB),
                            error = :error,
                            usage = CAST(:usage AS JSONB),
                            finished_at = NOW()
                        WHERE invocation_id = :invocation_id
                        RETURNING *
                        """
                    ),
                    {
                        "invocation_id": str(invocation_id),
                        "status": status,
                        "result": _json(result or {}),
                        "error": (error or "")[:8000] or None,
                        "usage": _json(usage or {}),
                    },
                )
            ).mappings().first()
            await session.commit()
        if row is None:
            raise RunNotFound(f"invocation {invocation_id} not found")
        return self._invocation_dict(row)

    async def add_usage(
        self,
        run_id: UUID | str,
        *,
        fence: int | None = None,
        **deltas: float,
    ) -> dict[str, Any]:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        f"""
                        SELECT usage, lease_epoch, lease_token_hash
                        FROM {self.schema}.agent_runs
                        WHERE run_id = :run_id
                        FOR UPDATE
                        """
                    ),
                    {"run_id": str(run_id)},
                )
            ).mappings().first()
            if row is None:
                raise RunNotFound(f"run {run_id} not found")
            _check_fence(row, fence, run_id)
            usage = _loads(row["usage"], {})
            for key, delta in deltas.items():
                usage[key] = float(usage.get(key, 0) or 0) + float(delta)
            await session.execute(
                text(
                    f"""
                    UPDATE {self.schema}.agent_runs
                    SET usage = CAST(:usage AS JSONB), updated_at = NOW()
                    WHERE run_id = :run_id
                    """
                ),
                {"usage": _json(usage), "run_id": str(run_id)},
            )
            await session.commit()
        return usage

    async def tenant_usage(self, tenant_id: UUID | str) -> dict[str, float]:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        f"""
                        SELECT
                            COALESCE(SUM((usage->>'model_calls')::numeric), 0)
                                AS model_calls,
                            COALESCE(SUM((usage->>'prompt_tokens')::numeric), 0)
                                AS prompt_tokens,
                            COALESCE(SUM((usage->>'completion_tokens')::numeric), 0)
                                AS completion_tokens,
                            COALESCE(SUM((usage->>'cost_usd')::numeric), 0)
                                AS cost_usd
                        FROM {self.schema}.agent_runs
                        WHERE tenant_id = :tenant_id
                        """
                    ),
                    {"tenant_id": str(tenant_id)},
                )
            ).mappings().first()
        return {
            key: float(row[key] or 0)
            for key in (
                "model_calls",
                "prompt_tokens",
                "completion_tokens",
                "cost_usd",
            )
        }

    async def invocation_health(
        self, window_minutes: int = 15
    ) -> list[dict[str, Any]]:
        """Model/tool invocation outcomes in a window, for provider health."""
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT kind, status, COUNT(*) AS count,
                               COALESCE(
                                   AVG(EXTRACT(EPOCH FROM
                                       (finished_at - created_at)) * 1000),
                                   0
                               ) AS avg_latency_ms,
                               MAX(finished_at) AS last_finished_at
                        FROM {self.schema}.invocations
                        WHERE created_at >= NOW() - make_interval(mins => :mins)
                        GROUP BY kind, status
                        ORDER BY kind, status
                        """
                    ),
                    {"mins": max(1, int(window_minutes))},
                )
            ).mappings().all()
        return [
            {
                "kind": row["kind"],
                "status": row["status"],
                "count": int(row["count"]),
                "avg_latency_ms": round(float(row["avg_latency_ms"] or 0), 2),
                "last_finished_at": row["last_finished_at"],
            }
            for row in rows
        ]

    async def status_counts(self) -> dict[str, int]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT status, count(*) AS n
                        FROM {self.schema}.agent_runs
                        GROUP BY status
                        """
                    )
                )
            ).mappings().all()
        return {str(row["status"]): int(row["n"]) for row in rows}

    # ---------------------------------------------------------------- leases
    # A worker can die between "I am calling a tool" and "the tool returned".
    # Those runs are not RUNNING, but they are recoverable: the lease expired
    # and nothing else will ever pick them up unless a claim normalises them.
    RECOVERABLE = frozenset({"RUNNING", "WAITING_TOOL", "WAITING_MODEL", "CHECKPOINTING"})

    async def claim_run(
        self, worker_id: str, lease_seconds: int = 60
    ) -> dict[str, Any] | None:
        token = secrets.token_urlsafe(32)
        token_hash = _token_hash(token)
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        f"""
                        SELECT run_id, status, version FROM {self.schema}.agent_runs
                        WHERE status = ANY(CAST(:recoverable AS TEXT[]))
                          AND (
                              lease_expires_at IS NULL
                              OR lease_expires_at < NOW()
                          )
                        ORDER BY updated_at ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                        """
                    ),
                    {"recoverable": sorted(self.RECOVERABLE)},
                )
            ).mappings().first()
            if row is None:
                await session.commit()
                return None
            run_id = row["run_id"]
            previous_status = str(row["status"])
            recovered = previous_status != "RUNNING"
            epoch = (
                await session.execute(
                    text(
                        f"""
                        UPDATE {self.schema}.agent_runs
                        SET lease_owner = :worker_id,
                            lease_token_hash = :token_hash,
                            lease_epoch = lease_epoch + 1,
                            lease_expires_at =
                                NOW() + (:lease_seconds * INTERVAL '1 second'),
                            heartbeat_at = NOW(),
                            updated_at = NOW(),
                            status = CASE
                                WHEN :recovered THEN 'RUNNING'
                                ELSE status
                            END,
                            version = CASE
                                WHEN :recovered THEN version + 1
                                ELSE version
                            END
                        WHERE run_id = :run_id
                        RETURNING lease_epoch, version
                        """
                    ),
                    {
                        "worker_id": worker_id,
                        "token_hash": token_hash,
                        "lease_seconds": max(5, lease_seconds),
                        "run_id": run_id,
                        "recovered": recovered,
                    },
                )
            ).mappings().one()
            if recovered:
                await self._publish(
                    session,
                    UUID(str(run_id)),
                    "AgentRunStateChanged",
                    {
                        "run_id": str(run_id),
                        "from": previous_status,
                        "to": "RUNNING",
                        "version": int(epoch["version"]),
                        "reason": "recovered after the previous worker was lost",
                        "actor": "claim",
                    },
                )
            await session.commit()
        return {
            "run": await self.get_run(run_id),
            "lease_token": token,
            "lease_epoch": int(epoch["lease_epoch"]),
            "recovered_from": previous_status if recovered else None,
        }

    async def assert_lease(self, run_id: UUID | str, token: str) -> None:
        async with self._sessions() as session:
            ok = await session.scalar(
                text(
                    f"""
                    SELECT 1 FROM {self.schema}.agent_runs
                    WHERE run_id = :run_id
                      AND status NOT IN (
                          'COMPLETED', 'FAILED', 'CANCELLED',
                          'TIMED_OUT', 'BUDGET_EXCEEDED'
                      )
                      AND lease_token_hash = :token_hash
                      AND lease_expires_at > NOW()
                    """
                ),
                {"run_id": str(run_id), "token_hash": _token_hash(token)},
            )
            await session.commit()
        if not ok:
            raise LeaseLost(f"run {run_id} lease is lost or expired")

    async def renew_lease(
        self, run_id: UUID | str, token: str, lease_seconds: int = 60
    ) -> None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        f"""
                        UPDATE {self.schema}.agent_runs
                        SET lease_expires_at =
                                NOW() + (:lease_seconds * INTERVAL '1 second'),
                            heartbeat_at = NOW(),
                            updated_at = NOW()
                        WHERE run_id = :run_id
                          AND status NOT IN (
                              'COMPLETED', 'FAILED', 'CANCELLED',
                              'TIMED_OUT', 'BUDGET_EXCEEDED'
                          )
                          AND lease_token_hash = :token_hash
                        RETURNING run_id
                        """
                    ),
                    {
                        "lease_seconds": max(5, lease_seconds),
                        "run_id": str(run_id),
                        "token_hash": _token_hash(token),
                    },
                )
            ).first()
            await session.commit()
        if row is None:
            raise LeaseLost(f"run {run_id} lease is lost or expired")

    async def release_lease(self, run_id: UUID | str, token: str) -> None:
        async with self._sessions() as session:
            await session.execute(
                text(
                    f"""
                    UPDATE {self.schema}.agent_runs
                    SET lease_owner = NULL,
                        lease_token_hash = NULL,
                        lease_expires_at = NULL,
                        updated_at = NOW()
                    WHERE run_id = :run_id
                      AND lease_token_hash = :token_hash
                    """
                ),
                {"run_id": str(run_id), "token_hash": _token_hash(token)},
            )
            await session.commit()

    # ------------------------------------------------- external (async) work
    async def defer_external(
        self,
        run_id: UUID | str,
        *,
        tool: str,
        idempotency_key: str,
        request: dict[str, Any] | None = None,
        submission_id: str | None = None,
        kernel_workspace_id: str | None = None,
        kernel_task_id: str | None = None,
        deadline_seconds: int = 3600,
        step_id: UUID | str | None = None,
        fence: int | None = None,
    ) -> dict[str, Any]:
        """Persist a deferred tool call; the Run must leave RUNNING after this."""
        external_id = uuid4()
        async with self._sessions() as session:
            if fence is not None:
                locked = (
                    await session.execute(
                        text(
                            f"SELECT lease_epoch, lease_token_hash "
                            f"FROM {self.schema}.agent_runs "
                            "WHERE run_id = :run_id FOR UPDATE"
                        ),
                        {"run_id": str(run_id)},
                    )
                ).mappings().first()
                if locked is None:
                    raise RunNotFound(f"run {run_id} not found")
                _check_fence(locked, fence, run_id)
            await session.execute(
                text(
                    f"""
                    INSERT INTO {self.schema}.external_invocations
                        (external_id, run_id, step_id, tool, idempotency_key,
                         submission_id, kernel_workspace_id, kernel_task_id,
                         status, request, deadline)
                    VALUES (
                        :external_id, :run_id, :step_id, :tool, :key,
                        :submission_id, :kernel_workspace_id, :kernel_task_id,
                        'PENDING', CAST(:request AS JSONB),
                        NOW() + (:deadline_seconds * INTERVAL '1 second')
                    )
                    ON CONFLICT (run_id, idempotency_key) DO NOTHING
                    """
                ),
                {
                    "external_id": external_id,
                    "run_id": str(run_id),
                    "step_id": str(step_id) if step_id else None,
                    "tool": tool,
                    "key": idempotency_key,
                    "submission_id": str(submission_id) if submission_id else None,
                    "kernel_workspace_id": (
                        str(kernel_workspace_id) if kernel_workspace_id else None
                    ),
                    "kernel_task_id": (
                        str(kernel_task_id) if kernel_task_id else None
                    ),
                    "request": _json(request or {}),
                    "deadline_seconds": max(30, int(deadline_seconds)),
                },
            )
            row = (
                await session.execute(
                    text(
                        f"""
                        SELECT * FROM {self.schema}.external_invocations
                        WHERE run_id = :run_id AND idempotency_key = :key
                        """
                    ),
                    {"run_id": str(run_id), "key": idempotency_key},
                )
            ).mappings().first()
            await self._publish(
                session,
                UUID(str(run_id)),
                "AgentRunDeferred",
                {
                    "run_id": str(run_id),
                    "external_id": str(row["external_id"]) if row else None,
                    "tool": tool,
                    "submission_id": str(submission_id) if submission_id else None,
                },
            )
            await session.commit()
        if row is None:
            raise RunNotFound(f"external invocation for {run_id} not found")
        return self._external_dict(row)

    async def get_external(
        self, run_id: UUID | str, idempotency_key: str
    ) -> dict[str, Any] | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        f"""
                        SELECT * FROM {self.schema}.external_invocations
                        WHERE run_id = :run_id AND idempotency_key = :key
                        """
                    ),
                    {"run_id": str(run_id), "key": idempotency_key},
                )
            ).mappings().first()
        return self._external_dict(row) if row else None

    async def list_external(self, run_id: UUID | str) -> list[dict[str, Any]]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT * FROM {self.schema}.external_invocations
                        WHERE run_id = :run_id
                        ORDER BY created_at ASC
                        """
                    ),
                    {"run_id": str(run_id)},
                )
            ).mappings().all()
        return [self._external_dict(row) for row in rows]

    async def finalize_external(
        self,
        external_id: UUID | str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        f"""
                        UPDATE {self.schema}.external_invocations
                        SET status = :status,
                            result = CAST(:result AS JSONB),
                            error = :error,
                            updated_at = NOW()
                        WHERE external_id = :external_id
                          AND status = 'PENDING'
                        RETURNING *
                        """
                    ),
                    {
                        "external_id": str(external_id),
                        "status": status,
                        "result": _json(result or {}),
                        "error": (error or "")[:8000] or None,
                    },
                )
            ).mappings().first()
            await session.commit()
        return self._external_dict(row) if row else None

    # --------------------------------------------------------------- wakeups
    async def add_wakeup(
        self,
        *,
        kind: str,
        ref_id: str,
        kernel_event_seq: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        async with self._sessions() as session:
            result = await session.execute(
                text(
                    f"""
                    INSERT INTO {self.schema}.runtime_wakeups
                        (kind, ref_id, kernel_event_seq, payload)
                    VALUES (:kind, :ref_id, :seq, CAST(:payload AS JSONB))
                    ON CONFLICT (kind, ref_id, kernel_event_seq) DO NOTHING
                    RETURNING wakeup_id
                    """
                ),
                {
                    "kind": kind,
                    "ref_id": str(ref_id),
                    "seq": kernel_event_seq,
                    "payload": _json(payload or {}),
                },
            )
            inserted = result.first() is not None
            await session.commit()
        return inserted

    async def consume_wakeups(self, limit: int = 200) -> dict[str, int]:
        """Apply unconsumed wakeups to deferred invocations and their runs.

        Idempotent: a wakeup is marked consumed in the same transaction that
        finalizes the matching invocation.  A Run leaves WAITING_EXTERNAL only
        once, so a duplicate wakeup has no second side effect.
        """
        stats = {"wakeups": 0, "finalized": 0, "resumed": 0, "skipped": 0}
        async with self._sessions() as session:
            wakeups = (
                await session.execute(
                    text(
                        f"""
                        SELECT * FROM {self.schema}.runtime_wakeups
                        WHERE consumed_at IS NULL
                        ORDER BY wakeup_id ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT :limit
                        """
                    ),
                    {"limit": max(1, min(1000, limit))},
                )
            ).mappings().all()
            for wakeup in wakeups:
                stats["wakeups"] += 1
                payload = _loads(wakeup["payload"], {})
                if wakeup["kind"] != "SUBMISSION_FINAL":
                    stats["skipped"] += 1
                else:
                    externals = (
                        await session.execute(
                            text(
                                f"""
                                UPDATE {self.schema}.external_invocations
                                SET status = 'FINAL',
                                    result = CAST(:result AS JSONB),
                                    updated_at = NOW()
                                WHERE submission_id = :submission_id
                                  AND status = 'PENDING'
                                RETURNING external_id, run_id
                                """
                            ),
                            {
                                "submission_id": str(wakeup["ref_id"]),
                                "result": _json(payload),
                            },
                        )
                    ).mappings().all()
                    for external in externals:
                        stats["finalized"] += 1
                        run_id = str(external["run_id"])
                        resumed = await self._resume_waiting_run(
                            session, run_id, payload, str(external["external_id"])
                        )
                        if resumed:
                            stats["resumed"] += 1
                await session.execute(
                    text(
                        f"""
                        UPDATE {self.schema}.runtime_wakeups
                        SET consumed_at = NOW()
                        WHERE wakeup_id = :wakeup_id
                        """
                    ),
                    {"wakeup_id": wakeup["wakeup_id"]},
                )
            await session.commit()
        return stats

    async def _resume_waiting_run(
        self,
        session: AsyncSession,
        run_id: str,
        external_result: dict[str, Any],
        external_id: str,
    ) -> bool:
        row = (
            await session.execute(
                text(
                    f"""
                    SELECT status, version FROM {self.schema}.agent_runs
                    WHERE run_id = :run_id FOR UPDATE
                    """
                ),
                {"run_id": run_id},
            )
        ).mappings().first()
        if row is None or str(row["status"]) != RunState.WAITING_EXTERNAL.value:
            return False
        version = int(row["version"]) + 1
        await session.execute(
            text(
                f"""
                UPDATE {self.schema}.agent_runs
                SET status = 'RUNNING', version = :version, updated_at = NOW()
                WHERE run_id = :run_id
                """
            ),
            {"version": version, "run_id": run_id},
        )
        await self._publish(
            session,
            UUID(run_id),
            "AgentRunStateChanged",
            {
                "run_id": run_id,
                "from": RunState.WAITING_EXTERNAL.value,
                "to": RunState.RUNNING.value,
                "version": version,
                "reason": "external result available",
                "actor": "wakeup",
            },
        )
        await self._publish(
            session,
            UUID(run_id),
            "AgentRunExternalResult",
            {
                "run_id": run_id,
                "external_id": external_id,
                "result": external_result,
            },
        )
        checkpoint_version = (
            await session.scalar(
                text(
                    f"""
                    SELECT COALESCE(MAX(version), 0) + 1
                    FROM {self.schema}.run_checkpoints WHERE run_id = :run_id
                    """
                ),
                {"run_id": run_id},
            )
        ) or 1
        await session.execute(
            text(
                f"""
                INSERT INTO {self.schema}.run_checkpoints
                    (checkpoint_id, run_id, version, state, idempotency_key)
                VALUES (
                    :checkpoint_id, :run_id, :version, CAST(:state AS JSONB),
                    :key
                )
                ON CONFLICT (run_id, idempotency_key) DO NOTHING
                """
            ),
            {
                "checkpoint_id": uuid4(),
                "run_id": run_id,
                "version": int(checkpoint_version),
                "state": _json(
                    {
                        "external_result": external_result,
                        "external_id": external_id,
                    }
                ),
                "key": f"external:{external_id}",
            },
        )
        return True

    async def expire_external(self, limit: int = 100) -> dict[str, int]:
        """Fail Runs whose deferred call passed its deadline (safety net)."""
        stats = {"expired": 0, "runs_failed": 0}
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT external_id, run_id FROM
                            {self.schema}.external_invocations
                        WHERE status = 'PENDING' AND deadline < NOW()
                        ORDER BY deadline ASC
                        LIMIT :limit
                        FOR UPDATE SKIP LOCKED
                        """
                    ),
                    {"limit": max(1, min(1000, limit))},
                )
            ).mappings().all()
            for row in rows:
                await session.execute(
                    text(
                        f"""
                        UPDATE {self.schema}.external_invocations
                        SET status = 'FAILED', error = 'external deadline exceeded',
                            updated_at = NOW()
                        WHERE external_id = :external_id
                        """
                    ),
                    {"external_id": row["external_id"]},
                )
                stats["expired"] += 1
                run_row = (
                    await session.execute(
                        text(
                            f"""
                            SELECT status, version FROM {self.schema}.agent_runs
                            WHERE run_id = :run_id FOR UPDATE
                            """
                        ),
                        {"run_id": str(row["run_id"])},
                    )
                ).mappings().first()
                if (
                    run_row is not None
                    and str(run_row["status"])
                    == RunState.WAITING_EXTERNAL.value
                ):
                    await session.execute(
                        text(
                            f"""
                            UPDATE {self.schema}.agent_runs
                            SET status = 'FAILED',
                                version = version + 1,
                                updated_at = NOW(),
                                finished_at = COALESCE(finished_at, NOW()),
                                last_error = 'external deadline exceeded'
                            WHERE run_id = :run_id
                            """
                        ),
                        {"run_id": str(row["run_id"])},
                    )
                    await self._publish(
                        session,
                        UUID(str(row["run_id"])),
                        "AgentRunStateChanged",
                        {
                            "run_id": str(row["run_id"]),
                            "from": RunState.WAITING_EXTERNAL.value,
                            "to": RunState.FAILED.value,
                            "reason": "external deadline exceeded",
                            "actor": "reaper",
                        },
                    )
                    stats["runs_failed"] += 1
            await session.commit()
        return stats

    async def wakeup_cursor(self, source_instance: str) -> int:
        async with self._sessions() as session:
            value = await session.scalar(
                text(
                    f"""
                    SELECT last_event_seq FROM {self.schema}.runtime_wakeup_cursors
                    WHERE source_instance = :instance
                    """
                ),
                {"instance": source_instance},
            )
            await session.commit()
        return int(value or 0)

    async def set_wakeup_cursor(self, source_instance: str, seq: int) -> None:
        async with self._sessions() as session:
            await session.execute(
                text(
                    f"""
                    INSERT INTO {self.schema}.runtime_wakeup_cursors
                        (source_instance, last_event_seq, updated_at)
                    VALUES (:instance, :seq, NOW())
                    ON CONFLICT (source_instance) DO UPDATE
                    SET last_event_seq = GREATEST(
                            {self.schema}.runtime_wakeup_cursors.last_event_seq,
                            EXCLUDED.last_event_seq
                        ),
                        updated_at = NOW()
                    """
                ),
                {"instance": source_instance, "seq": max(0, int(seq))},
            )
            await session.commit()

    # --------------------------------------------------------- kernel tasks
    async def remember_kernel_task(
        self,
        *,
        run_id: UUID | str,
        kernel_task_id: UUID | str,
        kernel_workspace_id: UUID | str,
        request_key: str | None = None,
    ) -> None:
        async with self._sessions() as session:
            await session.execute(
                text(
                    f"""
                    INSERT INTO {self.schema}.kernel_tasks
                        (run_id, kernel_task_id, kernel_workspace_id, request_key)
                    VALUES (
                        :run_id, :task_id, :workspace_id, :request_key
                    )
                    ON CONFLICT (run_id) DO UPDATE
                    SET kernel_task_id = EXCLUDED.kernel_task_id,
                        kernel_workspace_id = EXCLUDED.kernel_workspace_id,
                        request_key = COALESCE(
                            EXCLUDED.request_key,
                            {self.schema}.kernel_tasks.request_key
                        ),
                        created_at = NOW()
                    """
                ),
                {
                    "run_id": str(run_id),
                    "task_id": str(kernel_task_id),
                    "workspace_id": str(kernel_workspace_id),
                    "request_key": request_key,
                },
            )
            await session.commit()

    async def kernel_task(self, run_id: UUID | str) -> dict[str, Any] | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        f"""
                        SELECT run_id, kernel_task_id, kernel_workspace_id,
                               request_key, created_at
                        FROM {self.schema}.kernel_tasks WHERE run_id = :run_id
                        """
                    ),
                    {"run_id": str(run_id)},
                )
            ).mappings().first()
            await session.commit()
        if row is None:
            return None
        data = dict(row)
        data["run_id"] = str(data["run_id"])
        return data

    # ----------------------------------------------------------- kern leases
    async def remember_kernel_lease(
        self,
        *,
        run_id: UUID | str,
        kernel_task_id: UUID | str,
        kernel_workspace_id: UUID | str,
        lease_token: str,
        expires_at: Any | None = None,
    ) -> None:
        async with self._sessions() as session:
            await session.execute(
                text(
                    f"""
                    INSERT INTO {self.schema}.kernel_leases
                        (run_id, kernel_task_id, kernel_workspace_id,
                         lease_token, expires_at)
                    VALUES (
                        :run_id, :kernel_task_id, :kernel_workspace_id,
                        :lease_token, :expires_at
                    )
                    ON CONFLICT (run_id, kernel_task_id) DO UPDATE
                    SET lease_token = EXCLUDED.lease_token,
                        kernel_workspace_id = EXCLUDED.kernel_workspace_id,
                        expires_at = EXCLUDED.expires_at,
                        created_at = NOW()
                    """
                ),
                {
                    "run_id": str(run_id),
                    "kernel_task_id": str(kernel_task_id),
                    "kernel_workspace_id": str(kernel_workspace_id),
                    "lease_token": lease_token,
                    "expires_at": expires_at,
                },
            )
            await session.commit()

    async def kernel_lease(
        self, run_id: UUID | str, kernel_task_id: UUID | str
    ) -> str | None:
        async with self._sessions() as session:
            value = await session.scalar(
                text(
                    f"""
                    SELECT lease_token FROM {self.schema}.kernel_leases
                    WHERE run_id = :run_id AND kernel_task_id = :task_id
                    """
                ),
                {"run_id": str(run_id), "task_id": str(kernel_task_id)},
            )
            await session.commit()
        return str(value) if value else None

    async def latest_kernel_lease(self, run_id: UUID | str) -> dict[str, Any] | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        f"""
                        SELECT run_id, kernel_task_id, kernel_workspace_id,
                               lease_token, expires_at
                        FROM {self.schema}.kernel_leases
                        WHERE run_id = :run_id
                        ORDER BY created_at DESC
                        LIMIT 1
                        """
                    ),
                    {"run_id": str(run_id)},
                )
            ).mappings().first()
            await session.commit()
        if row is None:
            return None
        data = dict(row)
        for key in ("run_id", "kernel_task_id", "kernel_workspace_id"):
            data[key] = str(data[key])
        return data

    async def list_kernel_workspaces(self) -> list[str]:
        """Workspaces that currently have a live Run bound to a Kernel."""
        sources = await self.list_kernel_sources()
        return sorted({source["workspace_id"] for source in sources})

    async def list_kernel_sources(self) -> list[dict[str, str]]:
        """Distinct (Kernel instance, workspace) pairs a live Run is bound to.

        A Run carries its binding (``kernel`` JSON): the base URL and the
        workspace it executes in.  The event watcher must poll exactly those
        instances, never a process-wide default, or a second Kernel's events
        are never consumed.
        """
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT DISTINCT
                               kernel->>'base_url' AS base_url,
                               kernel->>'workspace_id' AS workspace_id
                        FROM {self.schema}.agent_runs
                        WHERE status NOT IN (
                            'COMPLETED', 'FAILED', 'CANCELLED',
                            'TIMED_OUT', 'BUDGET_EXCEEDED'
                        )
                          AND kernel->>'base_url' IS NOT NULL
                          AND kernel->>'base_url' <> ''
                          AND kernel->>'workspace_id' IS NOT NULL
                        ORDER BY base_url, workspace_id
                        """
                    )
                )
            ).all()
            await session.commit()
        return [
            {"base_url": str(row[0]).rstrip("/"), "workspace_id": str(row[1])}
            for row in rows
            if row[0] and row[1]
        ]

    # ------------------------------------------------------------ tool audit
    async def record_tool_audit(
        self,
        *,
        run_id: UUID | str | None,
        tool: str,
        idempotency_key: str,
        status: str,
        latency_ms: int,
        error_class: str | None = None,
        kernel_status: int | None = None,
        correlation: dict[str, Any] | None = None,
    ) -> None:
        async with self._sessions() as session:
            await session.execute(
                text(
                    f"""
                    INSERT INTO {self.schema}.runtime_tool_audit
                        (run_id, tool, idempotency_key, status, latency_ms,
                         error_class, kernel_status, correlation)
                    VALUES (
                        :run_id, :tool, :key, :status, :latency_ms,
                        :error_class, :kernel_status, CAST(:correlation AS JSONB)
                    )
                    """
                ),
                {
                    "run_id": str(run_id) if run_id else None,
                    "tool": tool,
                    "key": idempotency_key[:200],
                    "status": status,
                    "latency_ms": max(0, int(latency_ms)),
                    "error_class": error_class,
                    "kernel_status": kernel_status,
                    "correlation": _json(correlation or {}),
                },
            )
            await session.commit()

    @staticmethod
    def _external_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        for key in ("external_id", "run_id", "step_id", "submission_id",
                    "kernel_workspace_id", "kernel_task_id"):
            if data.get(key):
                data[key] = str(data[key])
        data["request"] = _loads(data.get("request"), {})
        data["result"] = _loads(data.get("result"), {})
        return data

    @staticmethod
    def _invocation_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        data["invocation_id"] = str(data["invocation_id"])
        data["run_id"] = str(data["run_id"])
        if data.get("step_id"):
            data["step_id"] = str(data["step_id"])
        data["request"] = _loads(data.get("request"), {})
        data["result"] = _loads(data.get("result"), {})
        data["usage"] = _loads(data.get("usage"), {})
        return data


__all__ = ["RunStore"]
