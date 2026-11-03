"""Reconcile Kernel-owned chunks against Kernel truth (Phase 2, item 5).

The commit projection rides the event outbox → inbox. Events are at-least-once,
but a stream can still miss one: a worker restarts between the commit and the
projection, a payload is dropped after retry exhaustion, or the projection throws
once and the row is dead-lettered. Without this sweep the chunk stays
``dispatched`` and its downstream never unlocks, which is the exact failure the
projection exists to remove.

The sweep is cheap and read-mostly: it selects only bound, unmerged chunks,
asks the Kernel about *those* node ids, and replays the same idempotent
projection the event path uses. Replays are safe — ``diagram_chunk_projections``
holds one row per (chunk, commit).

Only the Kernel decides whether a node is committed. A chunk the Kernel does not
know, or that is merely LEASED, is left alone.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("agentgraph.diagram_reconcile")

#: Bound, still open, and old enough that an in-flight event is not a miss yet.
_CANDIDATES = """
SELECT c.id, c.chunk_key, c.task_key, c.kernel_task_id, c.kernel_workspace_id,
       c.scope_hash, d.namespace, t.project, COALESCE(t.repo, d.repo) AS repo
FROM diagram_chunks c
JOIN diagrams d ON d.id = c.diagram_id
LEFT JOIN collab_tasks t ON t.workspace_id = c.workspace_id AND t.task_key = c.task_key
WHERE c.workspace_id = :ws
  AND c.kernel_task_id IS NOT NULL
  AND c.state IN ('dispatched', 'rework')
  AND c.updated_at < NOW() - make_interval(secs => :grace)
ORDER BY c.updated_at
LIMIT :limit
"""


async def reconcile_once(limit: int = 50, grace_seconds: int = 120,
                         workspace_id: Any = None,
                         admin_key: str | None = None) -> dict[str, int]:
    """One sweep over stale bound chunks. Replays projections, never merges twice.

    ``workspace_id`` narrows a run to one tenant (used by tests and by an
    operator chasing a specific graph); the loop sweeps everything.
    """
    from app.database import async_session_factory
    from app.services import diagrams, kernel_delivery
    from app.services.task_lifecycle import kernel_binding_for

    stats = {"candidates": 0, "projected": 0, "skipped": 0, "errors": 0}
    async with async_session_factory() as session:
        selector = (
            "SELECT DISTINCT workspace_id FROM diagram_chunks "
            "WHERE kernel_task_id IS NOT NULL AND state IN ('dispatched', 'rework') "
            "AND updated_at < NOW() - make_interval(secs => :g)"
            + (" AND workspace_id = :ws" if workspace_id is not None else ""))
        workspaces = (await session.execute(
            text(selector), {"g": grace_seconds, "ws": workspace_id})).scalars().all()
        for workspace_id in workspaces:
            rows = (await session.execute(text(_CANDIDATES), {
                "ws": workspace_id, "grace": grace_seconds, "limit": limit,
            })).mappings().all()
            for row in rows:
                stats["candidates"] += 1
                binding = await kernel_binding_for(session, workspace_id,
                                                   row["project"], row["repo"])
                if binding is None:
                    stats["skipped"] += 1
                    logger.info("no Kernel binding for workspace %s", workspace_id)
                    continue
                try:
                    outcome = await _reconcile_chunk(
                        session, dict(row), binding, kernel_delivery, diagrams,
                        admin_key)
                except Exception:  # noqa: BLE001 - one bad chunk must not stop the pass
                    stats["errors"] += 1
                    logger.exception("reconcile failed for chunk %s", row["chunk_key"])
                    continue
                stats["projected" if outcome else "skipped"] += 1
    return stats


async def _reconcile_chunk(
    session: AsyncSession, chunk: dict[str, Any], binding: dict[str, Any],
    kernel_delivery, diagrams, admin_key: str | None = None,
) -> bool:
    """Ask the Kernel about this chunk's node; replay the projection on COMMITTED."""
    plane = kernel_delivery.plane_for(binding, admin_key=admin_key)
    try:
        kernel = await plane.task(str(chunk["kernel_task_id"]))
    except Exception as exc:  # noqa: BLE001 - a missing node is not a projection error
        logger.info("kernel does not answer for node %s: %s", chunk["kernel_task_id"], exc)
        return False
    finally:
        await plane.aclose()
    if not kernel:
        logger.info("kernel has no evidence for node %s", chunk["kernel_task_id"])
        return False
    if str(kernel.get("status") or "") != "COMMITTED":
        logger.info("node %s is %s, not COMMITTED", chunk["kernel_task_id"], kernel.get("status"))
        return False
    commit_sha = str(kernel.get("result_commit_sha") or "")
    if not commit_sha:
        logger.info("node %s is COMMITTED without a commit sha", chunk["kernel_task_id"])
        return False
    result = await diagrams.project_kernel_commit(
        session, int(chunk["id"]), commit_sha,
        dedupe_key=f"kernel:{chunk['kernel_task_id']}:{commit_sha}",
        task_status="COMMITTED",
    )
    await session.commit()
    if result.get("projected"):
        logger.info("reconciled chunk %s from Kernel commit %s",
                    chunk["chunk_key"], commit_sha[:12])
        return True
    return False


async def reconcile_loop(interval_seconds: int = 300) -> None:
    while True:
        try:
            stats = await reconcile_once()
            if stats["candidates"]:
                logger.info("diagram reconcile: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - background resilience
            logger.exception("diagram reconcile pass failed")
        await asyncio.sleep(interval_seconds)
