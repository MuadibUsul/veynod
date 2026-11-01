"""Durable cross-domain event inbox and projection dispatcher (Phase 3).

Kernel and Agent Runtime publish ordered event streams.  AgentGraph ingests
them into ``event_inbox`` with an at-least-once cursor, projects each event
into product state, and retries failures with bounded backoff.  Exhausted
events become DEAD and can be replayed by an operator.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from app.config import get_settings
from app.database import async_session_factory
from app.services import agent_runtime, code_engine

logger = logging.getLogger("agentgraph.event_dispatch")
settings = get_settings()

SOURCE_KERNEL = "kernel"
SOURCE_RUNTIME = "runtime"
MAX_ATTEMPTS = 5
_STALE_PROCESSING_SECONDS = 300


@dataclass(frozen=True)
class InboxRow:
    inbox_id: int
    source: str
    source_instance: str
    source_seq: int
    event_type: str
    workspace_id: str | None
    payload: dict[str, Any]
    attempts: int


async def enqueue_kernel_events(
    kernel_workspace: str, events: list[dict[str, Any]]
) -> int:
    if not events:
        return 0
    inserted = 0
    async with async_session_factory() as session:
        for event in events:
            seq = int(event.get("event_seq") or 0)
            if not seq:
                continue
            result = await session.execute(
                text(
                    """
                    INSERT INTO event_inbox
                        (source, source_instance, source_seq, event_type,
                         workspace_id, payload)
                    VALUES ('kernel', :instance, :seq, :event_type,
                            :workspace, CAST(:payload AS JSONB))
                    ON CONFLICT (source, source_instance, source_seq) DO NOTHING
                    """
                ),
                {
                    "instance": str(kernel_workspace),
                    "seq": seq,
                    "event_type": str(event.get("event_type") or "UNKNOWN"),
                    "workspace": str(kernel_workspace),
                    "payload": _json(event.get("payload") or {}),
                },
            )
            inserted += max(0, result.rowcount or 0)
        await session.commit()
    return inserted


async def enqueue_runtime_events(
    base_url: str, events: list[dict[str, Any]]
) -> int:
    if not events:
        return 0
    inserted = 0
    async with async_session_factory() as session:
        for event in events:
            seq = int(event.get("event_seq") or 0)
            if not seq:
                continue
            payload = dict(event.get("payload") or {})
            payload.setdefault("run_id", event.get("run_id"))
            payload.setdefault("run_status", event.get("run_status"))
            result = await session.execute(
                text(
                    """
                    INSERT INTO event_inbox
                        (source, source_instance, source_seq, event_type,
                         workspace_id, payload)
                    VALUES ('runtime', :instance, :seq, :event_type,
                            :workspace, CAST(:payload AS JSONB))
                    ON CONFLICT (source, source_instance, source_seq) DO NOTHING
                    """
                ),
                {
                    "instance": str(base_url),
                    "seq": seq,
                    "event_type": str(event.get("event_type") or "UNKNOWN"),
                    "workspace": event.get("workspace_id"),
                    "payload": _json(payload),
                },
            )
            inserted += max(0, result.rowcount or 0)
        await session.commit()
    return inserted


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str)


async def ingest_kernel_once(limit: int = 500) -> dict[str, int]:
    stats = {"workspaces": 0, "events": 0, "enqueued": 0, "errors": 0}
    engine = code_engine._engine()
    workspaces = await code_engine._kernel_workspace_ids()
    stats["workspaces"] = len(workspaces)
    for kernel_workspace in workspaces:
        try:
            cursor = await code_engine._event_cursor(engine, kernel_workspace)
            response = await engine.events(kernel_workspace, cursor, limit)
            events = list(response.get("events") or [])
            next_seq = int(response.get("next_after_seq") or cursor)
            stats["events"] += len(events)
            stats["enqueued"] += await enqueue_kernel_events(
                kernel_workspace, events
            )
            if next_seq > cursor:
                async with async_session_factory() as session:
                    await session.execute(
                        text(
                            """
                            INSERT INTO code_projection_cursors
                                (kernel_workspace_id, last_event_seq, updated_at)
                            VALUES (CAST(:workspace AS UUID), :seq, NOW())
                            ON CONFLICT (kernel_workspace_id) DO UPDATE
                            SET last_event_seq = GREATEST(
                                    code_projection_cursors.last_event_seq,
                                    EXCLUDED.last_event_seq
                                ),
                                updated_at = NOW()
                            """
                        ),
                        {"workspace": kernel_workspace, "seq": next_seq},
                    )
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one workspace must not stop the rest
            stats["errors"] += 1
            logger.exception(
                "kernel event ingest failed for workspace %s", kernel_workspace
            )
    return stats


async def _runtime_base_urls() -> list[str]:
    """The Runtime service this deployment talks to, when a Run is active."""
    async with async_session_factory() as session:
        active = await session.scalar(
            text("SELECT 1 FROM collab_tasks WHERE active_run_id IS NOT NULL LIMIT 1")
        )
    base = (settings.agent_runtime_url or "").strip()
    if active and base:
        return [base]
    return []


async def _runtime_cursor(base_url: str) -> int:
    async with async_session_factory() as session:
        value = await session.scalar(
            text(
                "SELECT last_event_seq FROM runtime_projection_cursors "
                "WHERE runtime_base_url = :base_url"
            ),
            {"base_url": base_url},
        )
        if value is not None:
            return int(value)
        client = agent_runtime.AgentRuntimeClient(
            base_url,
            settings.agent_runtime_key,
            settings.agent_runtime_timeout,
        )
        latest = await client.latest_event_seq()
        await session.execute(
            text(
                """
                INSERT INTO runtime_projection_cursors
                    (runtime_base_url, last_event_seq, updated_at)
                VALUES (:base_url, :seq, NOW())
                ON CONFLICT (runtime_base_url) DO NOTHING
                """
            ),
            {"base_url": base_url, "seq": latest},
        )
        await session.commit()
        return latest


async def ingest_runtime_once(limit: int = 500) -> dict[str, int]:
    stats = {"instances": 0, "events": 0, "enqueued": 0, "errors": 0}
    bases = await _runtime_base_urls()
    stats["instances"] = len(bases)
    for base_url in bases:
        try:
            cursor = await _runtime_cursor(base_url)
            client = agent_runtime.AgentRuntimeClient(
                base_url,
                settings.agent_runtime_key,
                settings.agent_runtime_timeout,
            )
            response = await client.events(cursor, limit)
            events = list(response.get("events") or [])
            next_seq = int(response.get("next_after_seq") or cursor)
            stats["events"] += len(events)
            stats["enqueued"] += await enqueue_runtime_events(base_url, events)
            if next_seq > cursor:
                async with async_session_factory() as session:
                    await session.execute(
                        text(
                            """
                            INSERT INTO runtime_projection_cursors
                                (runtime_base_url, last_event_seq, updated_at)
                            VALUES (:base_url, :seq, NOW())
                            ON CONFLICT (runtime_base_url) DO UPDATE
                            SET last_event_seq = GREATEST(
                                    runtime_projection_cursors.last_event_seq,
                                    EXCLUDED.last_event_seq
                                ),
                                updated_at = NOW()
                            """
                        ),
                        {"base_url": base_url, "seq": next_seq},
                    )
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            stats["errors"] += 1
            logger.exception("runtime event ingest failed for %s", base_url)
    return stats


async def _due_rows(limit: int) -> list[InboxRow]:
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    UPDATE event_inbox
                    SET status = 'PROCESSING',
                        next_attempt_at = NOW() + make_interval(secs => 300)
                    WHERE inbox_id IN (
                        SELECT inbox_id FROM event_inbox
                        WHERE (status = 'PENDING' AND next_attempt_at <= NOW())
                           OR (status = 'PROCESSING'
                               AND next_attempt_at <= NOW()
                               AND attempts < :max_attempts)
                        ORDER BY next_attempt_at, inbox_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT :limit
                    )
                    RETURNING inbox_id, source, source_instance, source_seq,
                              event_type, workspace_id, payload, attempts
                    """
                ),
                {"limit": max(1, min(200, limit)), "max_attempts": MAX_ATTEMPTS},
            )
        ).mappings().all()
        await session.commit()
    return [
        InboxRow(
            inbox_id=int(row["inbox_id"]),
            source=row["source"],
            source_instance=row["source_instance"],
            source_seq=int(row["source_seq"]),
            event_type=row["event_type"],
            workspace_id=row["workspace_id"],
            payload=dict(row["payload"] or {}),
            attempts=int(row["attempts"] or 0),
        )
        for row in rows
    ]


async def _mark_processed(inbox_id: int) -> None:
    async with async_session_factory() as session:
        await session.execute(
            text(
                """
                UPDATE event_inbox
                SET status = 'PROCESSED', processed_at = NOW(), last_error = NULL
                WHERE inbox_id = :inbox_id
                """
            ),
            {"inbox_id": inbox_id},
        )
        await session.commit()


async def _mark_failed(row: InboxRow, error: str) -> None:
    attempts = row.attempts + 1
    dead = attempts >= MAX_ATTEMPTS
    delay = min(300, 2 ** min(attempts, 8))
    async with async_session_factory() as session:
        await session.execute(
            text(
                """
                UPDATE event_inbox
                SET status = :status,
                    attempts = :attempts,
                    last_error = :error,
                    next_attempt_at = NOW() + make_interval(secs => :delay),
                    dead_lettered_at = CASE WHEN :dead THEN NOW() ELSE NULL END
                WHERE inbox_id = :inbox_id
                """
            ),
            {
                "status": "DEAD" if dead else "PENDING",
                "attempts": attempts,
                "error": error[:4000],
                "delay": delay,
                "dead": dead,
                "inbox_id": row.inbox_id,
            },
        )
        await session.commit()


async def _process_row(row: InboxRow) -> None:
    if row.source == SOURCE_KERNEL:
        task_id = str(row.payload.get("task_id") or "")
        if not task_id:
            return
        await code_engine.sync_kernel_task_event(
            row.source_instance, task_id
        )
        return
    if row.source == SOURCE_RUNTIME:
        run_id = str(row.payload.get("run_id") or "")
        if not run_id:
            return
        from app.services import task_lifecycle

        await task_lifecycle.project_run_event(
            run_id,
            row.workspace_id or "",
            row.event_type,
            row.payload,
        )
        return
    raise ValueError(f"unknown event source {row.source}")


async def dispatch_once(limit: int = 100) -> dict[str, int]:
    stats = {"claimed": 0, "processed": 0, "failed": 0, "dead": 0}
    rows = await _due_rows(limit)
    stats["claimed"] = len(rows)
    for row in rows:
        try:
            await _process_row(row)
            await _mark_processed(row.inbox_id)
            stats["processed"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate one bad event
            logger.exception(
                "event projection failed: source=%s seq=%s type=%s",
                row.source,
                row.source_seq,
                row.event_type,
            )
            await _mark_failed(row, f"{type(exc).__name__}: {exc}")
            if row.attempts + 1 >= MAX_ATTEMPTS:
                stats["dead"] += 1
            else:
                stats["failed"] += 1
    return stats


async def inbox_stats() -> dict[str, Any]:
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT source, status, COUNT(*) AS count
                    FROM event_inbox GROUP BY source, status
                    """
                )
            )
        ).mappings().all()
        oldest = await session.scalar(
            text(
                "SELECT MIN(received_at) FROM event_inbox WHERE status = 'PENDING'"
            )
        )
        dead = await session.scalar(
            text("SELECT COUNT(*) FROM event_inbox WHERE status = 'DEAD'")
        )
    return {
        "by_source_status": [
            {"source": row["source"], "status": row["status"], "count": int(row["count"])}
            for row in rows
        ],
        "oldest_pending": oldest,
        "dead": int(dead or 0),
    }


async def list_inbox(
    status: str | None = None, source: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    clauses = []
    params: dict[str, Any] = {"limit": max(1, min(500, limit))}
    if status:
        clauses.append("status = :status")
        params["status"] = status.upper()
    if source:
        clauses.append("source = :source")
        params["source"] = source
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT inbox_id, source, source_instance, source_seq,
                           event_type, workspace_id, payload, status, attempts,
                           next_attempt_at, last_error, received_at,
                           processed_at, dead_lettered_at
                    FROM event_inbox {where}
                    ORDER BY inbox_id DESC LIMIT :limit
                    """
                ),
                params,
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def replay(inbox_id: int) -> dict[str, Any] | None:
    async with async_session_factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    UPDATE event_inbox
                    SET status = 'PENDING', attempts = 0, next_attempt_at = NOW(),
                        last_error = NULL, processed_at = NULL,
                        dead_lettered_at = NULL
                    WHERE inbox_id = :inbox_id
                    RETURNING inbox_id, source, source_seq, event_type, status
                    """
                ),
                {"inbox_id": inbox_id},
            )
        ).mappings().first()
        await session.commit()
    return dict(row) if row else None


async def event_dispatch_loop() -> None:
    logger.info("event dispatch loop started")
    while True:
        try:
            kernel = await ingest_kernel_once()
            runtime = await ingest_runtime_once()
            dispatched = await dispatch_once()
            if (
                kernel["enqueued"]
                or runtime["enqueued"]
                or dispatched["processed"]
                or dispatched["failed"]
                or kernel["errors"]
                or runtime["errors"]
            ):
                logger.info(
                    "event dispatch: kernel=%s runtime=%s dispatch=%s",
                    kernel,
                    runtime,
                    dispatched,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must survive
            logger.exception("event dispatch loop failed")
        await asyncio.sleep(
            max(0.5, settings.event_dispatch_interval_seconds)
        )


__all__ = [
    "dispatch_once",
    "enqueue_kernel_events",
    "enqueue_runtime_events",
    "event_dispatch_loop",
    "inbox_stats",
    "ingest_kernel_once",
    "ingest_runtime_once",
    "list_inbox",
    "replay",
]
