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
from datetime import datetime
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
    ) -> dict[str, Any]:
        run_id = uuid4()
        correlation = correlation_id or f"corr_{uuid4().hex}"
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
                         budget, correlation_id, parent_run_id, delegation_key)
                    VALUES
                        (:run_id, :tenant_id, :workspace_id, :product_task_id,
                         :goal, :workflow_id, :workflow_version, 'CREATED', 1,
                         CAST(:budget AS JSONB), :correlation_id,
                         :parent_run_id, :delegation_key)
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
                        SELECT status, version FROM {self.schema}.agent_runs
                        WHERE run_id = :run_id
                        FOR UPDATE
                        """
                    ),
                    {"run_id": str(run_id)},
                )
            ).mappings().first()
            if row is None:
                raise RunNotFound(f"run {run_id} not found")
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
    ) -> dict[str, Any]:
        async with self._sessions() as session:
            await session.execute(
                text(
                    f"SELECT run_id FROM {self.schema}.agent_runs "
                    "WHERE run_id = :run_id FOR UPDATE"
                ),
                {"run_id": str(run_id)},
            )
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
    ) -> dict[str, Any]:
        step_id = uuid4()
        async with self._sessions() as session:
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
    ) -> dict[str, Any]:
        async with self._sessions() as session:
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
        self, run_id: UUID | str, **deltas: float
    ) -> dict[str, Any]:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        f"""
                        SELECT usage FROM {self.schema}.agent_runs
                        WHERE run_id = :run_id
                        FOR UPDATE
                        """
                    ),
                    {"run_id": str(run_id)},
                )
            ).mappings().first()
            if row is None:
                raise RunNotFound(f"run {run_id} not found")
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
                        SELECT run_id FROM {self.schema}.agent_runs
                        WHERE status = 'RUNNING'
                          AND (
                              lease_expires_at IS NULL
                              OR lease_expires_at < NOW()
                          )
                        ORDER BY updated_at ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                        """
                    )
                )
            ).mappings().first()
            if row is None:
                await session.commit()
                return None
            run_id = row["run_id"]
            await session.execute(
                text(
                    f"""
                    UPDATE {self.schema}.agent_runs
                    SET lease_owner = :worker_id,
                        lease_token_hash = :token_hash,
                        lease_expires_at =
                            NOW() + (:lease_seconds * INTERVAL '1 second'),
                        heartbeat_at = NOW(),
                        updated_at = NOW()
                    WHERE run_id = :run_id
                    """
                ),
                {
                    "worker_id": worker_id,
                    "token_hash": token_hash,
                    "lease_seconds": max(5, lease_seconds),
                    "run_id": run_id,
                },
            )
            await session.commit()
        return {
            "run": await self.get_run(run_id),
            "lease_token": token,
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
