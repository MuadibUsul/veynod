"""Veynod Kernel as AgentGraph's code engine.

The engine is headless: AgentGraph owns identity, tasks, audit and the human
console; the Kernel owns repositories, AST contracts, symbol locks, the
deterministic gate and the atomic commit. This module is the only AgentGraph
code that talks to the Kernel administrative API.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, principal_for_admin, require_permission
from app.config import get_settings
from app.database import async_session_factory
from app.models import CollabTask, TaskStatus, Workspace
from app.schemas import (
    AcceptanceItem,
    TaskCreateRequest,
)
from app.services import quality
from app.services import tasks as task_service
from contracts import kernel_http

logger = logging.getLogger("agentgraph.code_engine")
settings = get_settings()
_PROJECTION_LOCK = asyncio.Lock()


class CodeRunCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    repo_path: str = Field(min_length=1, max_length=1024)
    symbol: str = Field(min_length=1, max_length=512)
    test_symbol: str = Field(min_length=1, max_length=512)
    spec: str = Field(min_length=10, max_length=20_000)
    priority: int = Field(default=100, ge=0, le=999)


class CodeEngineError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class KernelEngineClient:
    def __init__(self, base_url: str, admin_key: str, timeout: float = 45) -> None:
        self.base_url = base_url.rstrip("/")
        self.admin_key = admin_key
        self.timeout = timeout

    async def _request(self, method: str, path: str, *, json: Any = None,
                       params: dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                _status, body = await kernel_http.request(
                    client, method, self.base_url + path,
                    auth=kernel_http.KernelAuth(admin_key=self.admin_key),
                    params=params, json_body=json)
            except kernel_http.KernelHTTPError as exc:
                raise CodeEngineError(
                    str(exc), 502 if exc.unreachable or exc.status >= 500 else exc.status,
                ) from exc
        return body

    async def create_artifact(
        self, workspace_id: str, symbol: str, *, kind: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/admin/artifacts",
            json={
                "workspace_id": workspace_id,
                "symbol_path": symbol,
                "kind": kind,
                "status": "FROZEN",
            },
        )

    async def create_task(self, workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/admin/tasks",
            json={"workspace_id": workspace_id, **payload},
        )

    async def list_tasks(self, workspace_id: str) -> list[dict[str, Any]]:
        result = await self._request(
            "GET",
            "/v1/admin/tasks",
            params={"workspace_id": workspace_id, "limit": 2000},
        )
        return list(result) if isinstance(result, list) else []

    async def task(self, workspace_id: str, task_id: str) -> dict[str, Any] | None:
        for task in await self.list_tasks(workspace_id):
            if str(task.get("task_id")) == str(task_id):
                return task
        return None

    async def evidence(self, task_id: str) -> dict[str, Any]:
        result = await self._request(
            "GET", f"/v1/admin/tasks/{task_id}/evidence"
        )
        return result if isinstance(result, dict) else {}

    async def delivery(self, task_id: str) -> dict[str, Any] | None:
        try:
            result = await self._request(
                "GET", f"/v1/admin/tasks/{task_id}/delivery"
            )
        except CodeEngineError as exc:
            if exc.status_code == 404:
                return None
            raise
        return result if isinstance(result, dict) else None

    async def cancel_task(self, task_id: str, reason: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/v1/admin/tasks/{task_id}/cancel",
            json={"reason": reason},
        )

    async def latest_event_seq(self, workspace_id: str) -> int:
        payload = await self._request(
            "GET", "/v1/dag", params={"workspace_id": workspace_id}
        )
        return int(payload.get("latest_event_seq") or 0)

    async def events(
        self, workspace_id: str, after_seq: int, limit: int = 200
    ) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            "/v1/dag/events",
            params={
                "workspace_id": workspace_id,
                "after_seq": after_seq,
                "limit": limit,
            },
        )
        return payload if isinstance(payload, dict) else {}


def _admin_keys() -> dict[str, str]:
    raw = (settings.kernel_admin_keys or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.error("KERNEL_ADMIN_KEYS is not valid JSON; using the default key")
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(base).rstrip("/"): str(key)
        for base, key in parsed.items()
        if base and key
    }


def _engine(base_url: str | None = None) -> KernelEngineClient:
    """Client for the Kernel instance a task is bound to (not a global)."""
    base = (base_url or settings.kernel_url).rstrip("/")
    return KernelEngineClient(base, _admin_keys().get(base, settings.kernel_admin_key))


def _workspace_id() -> str:
    if not settings.kernel_workspace_id:
        raise CodeEngineError(
            "KERNEL_WORKSPACE_ID is not configured for this AgentGraph deployment", 503
        )
    return settings.kernel_workspace_id


def _task_dict(task: Any) -> dict[str, Any]:
    return task.model_dump(mode="json") if hasattr(task, "model_dump") else dict(task)


def _status(task: Any) -> str:
    value = getattr(task, "status", "")
    return value.value if hasattr(value, "value") else str(value)


def gate_updates_from_evidence(
    evidence: dict[str, Any], commit_sha: str | None
) -> list[dict[str, Any]]:
    """Map durable Kernel gate results to AgentGraph gate records.

    Returns an empty list when the evidence is incomplete or belongs to a
    different commit.  The caller must not advance the product task in that
    case; a missing or mismatched evidence row is not a PASS.
    """
    submission = evidence.get("submission") or {}
    intent = evidence.get("merge_intent") or {}
    if submission.get("status") != "COMMITTED" and intent.get("status") != "MERGED":
        return []
    merged_sha = (
        intent.get("merged_commit_sha")
        or submission.get("result_commit_sha")
        or evidence.get("result_commit_sha")
    )
    if not merged_sha or not commit_sha or str(merged_sha) != str(commit_sha):
        return []

    results = (
        evidence.get("gate_results")
        or intent.get("gate_results")
        or submission.get("gate_results")
        or []
    )
    if not isinstance(results, list) or not results:
        return []

    groups: dict[str, list[dict[str, Any]]] = {"CI": [], "TESTS": []}
    for item in results:
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage") or "")
        if not stage:
            continue
        kind = "TESTS" if stage == "UNIT_TEST_SUITE" else "CI"
        groups[kind].append(item)

    submission_id = submission.get("submission_id") or ""
    run_url = (
        f"veynod-kernel://submission/{submission_id}"
        if submission_id
        else f"veynod-kernel://task/{evidence.get('task_id', '')}"
    )
    updates: list[dict[str, Any]] = []
    for kind in ("CI", "TESTS"):
        items = groups[kind]
        if not items:
            continue
        passed = sum(1 for item in items if item.get("passed") is True)
        failed = len(items) - passed
        stages = [str(item.get("stage")) for item in items]
        updates.append(
            {
                "gate_kind": kind,
                "status": "PASS" if failed == 0 else "FAIL",
                "evidence": {
                    "run_url": run_url,
                    "commit_sha": str(merged_sha),
                    "summary": (
                        f"Kernel {kind} gate "
                        f"{'passed' if failed == 0 else 'failed'}: "
                        + ", ".join(stages)
                    ),
                    "counts": {"passed": passed, "failed": failed},
                    "stages": stages,
                },
            }
        )
    return updates


async def create_run(
    session: AsyncSession, principal: Principal, request: CodeRunCreate
) -> dict[str, Any]:
    """Create the product task and start the one Agent Run that will execute it.

    The Kernel task is created by the Runtime through ``KernelToolRuntime``
    (pull_task_bundle), not here.
    """
    require_permission(principal, "task:create")
    task_key = "code-" + uuid4().hex[:16]
    task = await task_service.create_task(
        session,
        principal,
        TaskCreateRequest(
            task_key=task_key,
            title=request.title,
            repo=request.repo_path,
            description=request.spec,
            status="READY",
            priority=request.priority,
            input_context={
                "code_engine": "veynod-kernel",
                "symbol": request.symbol,
                "test_symbol": request.test_symbol,
            },
            acceptance=[
                AcceptanceItem(
                    id="frozen_tests",
                    text="The frozen test passes in the Veynod Kernel gate.",
                    kind="AUTO",
                )
            ],
        ),
    )
    from app.services import task_lifecycle

    started = await task_lifecycle.start_run(session, principal, task_key)
    refreshed = await task_service.get_task(session, principal, task_key)
    return {
        "id": task_key,
        "agentgraph_task_key": task_key,
        "agentgraph_task_id": str(task.id),
        "kernel_workspace_id": (started.get("kernel") or {}).get("workspace_id"),
        "kernel_task_id": None,
        "run_id": started.get("run_id"),
        "trace_id": started.get("trace_id"),
        "status": _status(refreshed),
        "kernel_status": "PENDING",
        "title": refreshed.title,
        "repo_path": request.repo_path,
        "symbol": request.symbol,
        "test_symbol": request.test_symbol,
    }


def _kernel_ids(task: Any) -> tuple[str, str] | None:
    context = getattr(task, "input_context", None) or {}
    if context.get("code_engine") != "veynod-kernel":
        return None
    kernel = context.get("kernel") or {}
    workspace_id, task_id = kernel.get("workspace_id"), kernel.get("task_id")
    if not workspace_id or not task_id:
        return None
    return str(workspace_id), str(task_id)


async def cancel_kernel_run(task: Any, reason: str) -> bool:
    """Propagate a product-task cancellation to the Kernel.

    Returns False for non-Kernel tasks.  Raises CodeEngineError (or an HTTP
    error) if the Kernel cannot be reached; the caller must not mark a code
    task cancelled while its Kernel task may still merge.
    """
    ids = _kernel_ids(task)
    if ids is None:
        return False
    _workspace_id, kernel_task_id = ids
    await _engine().cancel_task(kernel_task_id, reason)
    return True


async def list_runs(
    session: AsyncSession, principal: Principal
) -> list[dict[str, Any]]:
    require_permission(principal, "task:read")
    rows = list(
        (
            await session.execute(
                select(CollabTask)
                .where(
                    CollabTask.workspace_id == principal.workspace.id,
                    CollabTask.input_context["code_engine"].astext == "veynod-kernel",
                )
                .order_by(CollabTask.created_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    engine = _engine()
    kernel_cache: dict[str, dict[str, dict[str, Any]]] = {}
    output: list[dict[str, Any]] = []
    for task in rows:
        ids = _kernel_ids(task)
        if ids is None:
            continue
        workspace_id, task_id = ids
        if workspace_id not in kernel_cache:
            kernel_cache[workspace_id] = {
                str(item.get("task_id")): item for item in await engine.list_tasks(workspace_id)
            }
        kernel = kernel_cache[workspace_id].get(task_id, {})
        output.append(
            {
                "id": task.task_key,
                "title": task.title,
                "agentgraph_task_key": task.task_key,
                "kernel_workspace_id": workspace_id,
                "kernel_task_id": task_id,
                "status": _status(task),
                "kernel_status": str(kernel.get("status") or "UNKNOWN"),
                "commit_sha": kernel.get("result_commit_sha"),
                "updated_at": task.updated_at.isoformat() if task.updated_at else None,
            }
        )
    return output


_SYNCABLE_TASK_STATUSES = {"TODO", "READY", "IN_PROGRESS"}


def lifecycle_action(kernel_status: str, task_status: str) -> str | None:
    """Map a Kernel terminal state to the AgentGraph projection action.

    Only not-yet-reviewed product tasks are moved.  A task already under
    human review must not be silently failed/cancelled by a late Kernel event.
    """
    if task_status not in _SYNCABLE_TASK_STATUSES:
        return None
    if kernel_status == "CANCELLED":
        return "cancel"
    if kernel_status == "FAILED":
        return "fail"
    if kernel_status == "COMMITTED":
        return "commit"
    return None


async def _sync_lifecycle(
    session: AsyncSession, principal: Principal, task: Any, kernel: dict[str, Any]
) -> Any:
    """Legacy Kernel-only tasks converge through the single lifecycle writer."""
    from app.services import task_lifecycle

    return await task_lifecycle.project_kernel_task(session, principal, task, kernel)


async def get_run(
    session: AsyncSession, principal: Principal, task_key: str
) -> dict[str, Any]:
    require_permission(principal, "task:read")
    task = await task_service.get_task(session, principal, task_key)
    ids = _kernel_ids(task)
    if ids is None:
        raise CodeEngineError(f"{task_key} is not a Kernel code run", 404)
    workspace_id, task_id = ids
    kernel = await _engine().task(workspace_id, task_id)
    if kernel is None:
        raise CodeEngineError("Kernel task disappeared", 502)
    before = _status(task)
    task = await _sync_lifecycle(session, principal, task, kernel)
    after = _status(task)
    return {
        "id": task.task_key,
        "title": task.title,
        "agentgraph_task_key": task.task_key,
        "agentgraph_task_id": str(task.id),
        "agentgraph_status": after,
        "kernel_workspace_id": workspace_id,
        "kernel_task_id": task_id,
        "kernel_status": str(kernel.get("status") or "UNKNOWN"),
        "commit_sha": kernel.get("result_commit_sha"),
        "merge_mode": kernel.get("merge_mode"),
        "status": "SYNCED" if after != before or after == "REVIEWING" else before,
        "repo_path": task.repo,
        "symbol": (task.input_context or {}).get("symbol"),
        "test_symbol": (task.input_context or {}).get("test_symbol"),
    }


async def audit_run(
    session: AsyncSession, principal: Principal, task_key: str
) -> dict[str, Any]:
    require_permission(principal, "task:read")
    task = await task_service.get_task(session, principal, task_key)
    if _kernel_ids(task) is None:
        raise CodeEngineError(f"{task_key} is not a Kernel code run", 404)
    return await quality.audit_view(session, principal, task_key)


_PROJECTABLE_STATUSES = (
    TaskStatus.TODO,
    TaskStatus.READY,
    TaskStatus.IN_PROGRESS,
)


async def project_code_runs_once(limit: int | None = None) -> dict[str, int]:
    async with _PROJECTION_LOCK:
        return await _project_code_runs_once(limit)


async def _project_code_runs_once(limit: int | None = None) -> dict[str, int]:
    """Project Kernel terminal states into AgentGraph, without a GET request.

    The batch is bounded and per-workspace Kernel listings are cached.  A
    failure for one task never stops the rest of the batch.
    """
    batch = limit or settings.code_projection_batch_size
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(CollabTask)
                .where(
                    CollabTask.input_context["code_engine"].astext
                    == "veynod-kernel",
                    CollabTask.status.in_(_PROJECTABLE_STATUSES),
                )
                .order_by(CollabTask.updated_at.asc())
                .limit(batch)
            )
        ).scalars().all()
        refs = [(task.id, task.workspace_id) for task in rows]

    stats = {
        "scanned": len(refs),
        "synced": 0,
        "cancel": 0,
        "fail": 0,
        "commit": 0,
        "skipped": 0,
        "errors": 0,
    }
    if not refs:
        return stats

    engine = _engine()
    kernel_cache: dict[str, dict[str, dict[str, Any]]] = {}
    for task_id, workspace_id in refs:
        try:
            async with async_session_factory() as session:
                task = await session.get(CollabTask, task_id)
                workspace = await session.get(Workspace, workspace_id)
                if task is None or workspace is None:
                    stats["skipped"] += 1
                    continue
                ids = _kernel_ids(task)
                if ids is None:
                    stats["skipped"] += 1
                    continue
                kernel_workspace, kernel_task_id = ids
                if kernel_workspace not in kernel_cache:
                    kernel_cache[kernel_workspace] = {
                        str(item.get("task_id")): item
                        for item in await engine.list_tasks(kernel_workspace)
                    }
                kernel = kernel_cache[kernel_workspace].get(kernel_task_id)
                if kernel is None:
                    stats["skipped"] += 1
                    continue
                action = lifecycle_action(
                    str(kernel.get("status") or ""), _status(task)
                )
                if action is None:
                    stats["skipped"] += 1
                    continue
                before = _status(task)
                principal = principal_for_admin(workspace)
                after = await _sync_lifecycle(session, principal, task, kernel)
                if _status(after) != before:
                    stats["synced"] += 1
                    stats[action] += 1
                else:
                    stats["skipped"] += 1
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one run must not stop the loop
            stats["errors"] += 1
            logger.exception("code projection failed for task %s", task_id)
    return stats


def event_task_ids(events: list[dict[str, Any]]) -> list[str]:
    """Extract unique Kernel task ids from event envelopes, in order."""
    ids: list[str] = []
    for event in events:
        payload = event.get("payload") or {}
        task_id = payload.get("task_id") if isinstance(payload, dict) else None
        if task_id:
            value = str(task_id)
            if value not in ids:
                ids.append(value)
    return ids


async def _event_cursor(engine: KernelEngineClient, kernel_workspace: str) -> int:
    async with async_session_factory() as session:
        value = await session.scalar(
            text(
                "SELECT last_event_seq FROM code_projection_cursors "
                "WHERE kernel_workspace_id = CAST(:kernel_ws AS UUID)"
            ),
            {"kernel_ws": kernel_workspace},
        )
    if value is not None:
        return int(value)
    # First sight of this Kernel workspace: its history is not replayed, so
    # anything it finished before now would never arrive as an event (a commit
    # landing before the first poll left its chunk `dispatched`). Read the head
    # first, then bring what is already bound to it up to the Kernel's state:
    # whatever happens after the head still arrives as an event.
    latest = await engine.latest_event_seq(kernel_workspace)
    await _catch_up_bound_work(kernel_workspace)
    async with async_session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO code_projection_cursors
                    (kernel_workspace_id, last_event_seq, updated_at)
                VALUES (CAST(:kernel_ws AS UUID), :seq, NOW())
                ON CONFLICT (kernel_workspace_id) DO NOTHING
                """
            ),
            {"kernel_ws": kernel_workspace, "seq": latest},
        )
        await session.commit()
    return latest


async def _catch_up_bound_work(kernel_workspace: str) -> int:
    """Sync every task and open chunk bound to a Kernel workspace, once.

    Idempotent (the lifecycle writer and the chunk projection dedupe), so two
    consumers meeting the same new workspace at once is harmless.
    """
    async with async_session_factory() as session:
        task_ids = (
            await session.execute(
                select(CollabTask.input_context["kernel"]["task_id"].astext).where(
                    CollabTask.input_context["kernel"]["workspace_id"].astext
                    == str(kernel_workspace),
                    CollabTask.status.in_(_PROJECTABLE_STATUSES),
                )
            )
        ).scalars().all()
        chunk_ids = (
            await session.execute(
                text(
                    "SELECT kernel_task_id::text FROM diagram_chunks "
                    "WHERE kernel_workspace_id::text = :ws AND kernel_task_id IS NOT NULL "
                    "AND state IN ('dispatched', 'rework')"
                ),
                {"ws": str(kernel_workspace)},
            )
        ).scalars().all()
    moved = 0
    chunks = {str(item) for item in chunk_ids if item}
    for kernel_task_id in sorted({str(item) for item in task_ids if item} | chunks):
        try:
            stats = await sync_kernel_task_event(kernel_workspace, kernel_task_id)
            moved += stats["synced"] + stats.get("diagram", 0)
            if kernel_task_id in chunks and not stats.get("diagram"):
                # A chunk whose task row does not carry the Kernel ids.
                kernel = await _engine().task(kernel_workspace, kernel_task_id)
                if kernel and str(kernel.get("status") or "") == "COMMITTED":
                    async with async_session_factory() as session:
                        moved += await _project_chunk(session, kernel_task_id, kernel)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one task must not stop the catch-up
            logger.exception(
                "catch-up failed for kernel task %s in %s", kernel_task_id, kernel_workspace
            )
    if moved:
        logger.info("caught up %s item(s) on first sight of kernel workspace %s",
                    moved, kernel_workspace)
    return moved


def merge_kernel_workspace_ids(
    configured: str | None, discovered: list[str]
) -> list[str]:
    values = {str(value) for value in [configured, *discovered] if value}
    return sorted(values)


async def _kernel_workspace_ids() -> list[str]:
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT DISTINCT candidate FROM (
                        SELECT input_context->'kernel'->>'workspace_id' AS candidate
                        FROM collab_tasks
                        WHERE input_context->>'code_engine' = 'veynod-kernel'
                        UNION
                        SELECT correlation->>'kernel_workspace_id'
                        FROM collab_tasks
                        WHERE correlation ? 'kernel_workspace_id'
                        UNION
                        SELECT kernel_workspace_id::text
                        FROM kernel_bindings
                        WHERE status = 'ACTIVE'
                        UNION
                        SELECT kernel_workspace_id::text
                        FROM code_projection_cursors
                    ) candidates
                    WHERE candidate IS NOT NULL AND candidate <> ''
                    """
                )
            )
        ).all()
    discovered = [str(row[0]) for row in rows if row[0]]
    return merge_kernel_workspace_ids(settings.kernel_workspace_id, discovered)


async def sync_kernel_task_event(
    kernel_workspace: str, kernel_task_id: str
) -> dict[str, int]:
    """Sync every AgentGraph task bound to one Kernel task.

    Used by the durable event dispatcher; safe to call repeatedly.
    """
    stats = {"synced": 0, "cancel": 0, "fail": 0, "commit": 0, "skipped": 0}
    kernel = await _engine().task(kernel_workspace, kernel_task_id)
    if kernel is None:
        stats["skipped"] += 1
        return stats
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(CollabTask).where(
                    CollabTask.input_context["kernel"]["task_id"].astext
                    == str(kernel_task_id)
                )
            )
        ).scalars().all()
        for task in rows:
            workspace = await session.get(Workspace, task.workspace_id)
            if workspace is None:
                stats["skipped"] += 1
                continue
            action = lifecycle_action(str(kernel.get("status") or ""), _status(task))
            if action is None:
                # The Product Task may already be REVIEWING (an earlier event
                # delivered it); the chunk still has to learn about the commit.
                if str(kernel.get("status") or "") == "COMMITTED":
                    stats["diagram"] = stats.get("diagram", 0) + await _project_chunk(
                        session, kernel_task_id, kernel
                    )
                stats["skipped"] += 1
                continue
            before = _status(task)
            principal = principal_for_admin(workspace)
            after = await _sync_lifecycle(session, principal, task, kernel)
            if _status(after) != before:
                stats["synced"] += 1
                stats[action] += 1
            else:
                stats["skipped"] += 1
            if action == "commit":
                stats["diagram"] = stats.get("diagram", 0) + await _project_chunk(
                    session, kernel_task_id, kernel
                )
    return stats


async def _project_chunk(
    session: AsyncSession, kernel_task_id: str, kernel: dict[str, Any]
) -> int:
    """Deliver the Kernel commit to the Diagram chunk that owns this node.

    The chunk is the plan object; without this the graph showed `dispatched`
    forever and downstream chunks never unlocked, so a human had to hand-merge
    an already committed change.
    """
    from app.services import diagrams

    commit_sha = str(kernel.get("result_commit_sha") or "")
    if not commit_sha:
        return 0
    chunk_id = await session.scalar(
        text("SELECT id FROM diagram_chunks WHERE kernel_task_id = CAST(:t AS UUID)"),
        {"t": str(kernel_task_id)},
    )
    if chunk_id is None:
        return 0
    result = await diagrams.project_kernel_commit(
        session,
        int(chunk_id),
        commit_sha,
        dedupe_key=f"kernel:{kernel_task_id}:{commit_sha}",
        task_status=str(kernel.get("status") or ""),
    )
    await session.commit()
    return 1 if result.get("projected") else 0


async def _project_one_kernel_workspace(
    engine: KernelEngineClient, kernel_workspace: str, limit: int
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "events": 0,
        "tasks": 0,
        "synced": 0,
        "cancel": 0,
        "fail": 0,
        "commit": 0,
        "skipped": 0,
        "errors": 0,
        "cursor": 0,
    }
    cursor = await _event_cursor(engine, kernel_workspace)
    response = await engine.events(kernel_workspace, cursor, limit)
    events = response.get("events") or []
    next_seq = int(response.get("next_after_seq") or cursor)
    stats["events"] = len(events)
    stats["cursor"] = next_seq
    task_ids = event_task_ids(events)
    stats["tasks"] = len(task_ids)

    if task_ids:
        kernel_cache = {
            str(item.get("task_id")): item
            for item in await engine.list_tasks(kernel_workspace)
        }
        for kernel_task_id in task_ids:
            kernel = kernel_cache.get(kernel_task_id)
            if kernel is None:
                stats["skipped"] += 1
                continue
            try:
                async with async_session_factory() as session:
                    rows = (
                        await session.execute(
                            select(CollabTask).where(
                                CollabTask.input_context["kernel"][
                                    "task_id"
                                ].astext
                                == kernel_task_id
                            )
                        )
                    ).scalars().all()
                    for task in rows:
                        workspace = await session.get(
                            Workspace, task.workspace_id
                        )
                        if workspace is None:
                            stats["skipped"] += 1
                            continue
                        action = lifecycle_action(
                            str(kernel.get("status") or ""), _status(task)
                        )
                        if action is None:
                            stats["skipped"] += 1
                            continue
                        before = _status(task)
                        principal = principal_for_admin(workspace)
                        after = await _sync_lifecycle(
                            session, principal, task, kernel
                        )
                        if _status(after) != before:
                            stats["synced"] += 1
                            stats[action] += 1
                        else:
                            stats["skipped"] += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one task must not stop the batch
                stats["errors"] += 1
                logger.exception(
                    "event projection failed for kernel task %s",
                    kernel_task_id,
                )

    async with async_session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO code_projection_cursors
                    (kernel_workspace_id, last_event_seq, updated_at)
                VALUES (CAST(:kernel_ws AS UUID), :seq, NOW())
                ON CONFLICT (kernel_workspace_id) DO UPDATE
                SET last_event_seq = GREATEST(
                        code_projection_cursors.last_event_seq,
                        EXCLUDED.last_event_seq
                    ),
                    updated_at = NOW()
                """
            ),
            {"kernel_ws": kernel_workspace, "seq": next_seq},
        )
        await session.commit()
    return stats


async def project_code_events_once(limit: int = 200) -> dict[str, Any]:
    """Consume Kernel events after durable per-workspace cursors.

    The task scan remains a periodic fallback; this path avoids full scans
    when the event stream is healthy.  Workspaces are discovered from code
    tasks and existing cursors, so old code planes are not silently ignored.
    """
    stats: dict[str, Any] = {
        "workspaces": 0,
        "events": 0,
        "tasks": 0,
        "synced": 0,
        "cancel": 0,
        "fail": 0,
        "commit": 0,
        "skipped": 0,
        "errors": 0,
        "cursors": {},
    }
    async with _PROJECTION_LOCK:
        engine = _engine()
        workspaces = await _kernel_workspace_ids()
        stats["workspaces"] = len(workspaces)
        for kernel_workspace in workspaces:
            try:
                one = await _project_one_kernel_workspace(
                    engine, kernel_workspace, limit
                )
                for key in (
                    "events",
                    "tasks",
                    "synced",
                    "cancel",
                    "fail",
                    "commit",
                    "skipped",
                    "errors",
                ):
                    stats[key] += one[key]
                stats["cursors"][kernel_workspace] = one["cursor"]
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one workspace must not stop the loop
                stats["errors"] += 1
                logger.exception(
                    "event projection failed for kernel workspace %s",
                    kernel_workspace,
                )
    return stats


async def code_projection_event_loop() -> None:
    while True:
        try:
            stats = await project_code_events_once()
            if stats["synced"] or stats["errors"]:
                logger.info("code event projection: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must survive
            logger.exception("code event projection loop failed")
        await asyncio.sleep(
            max(0.5, settings.code_projection_event_interval_seconds)
        )


async def code_projection_loop() -> None:
    while True:
        try:
            stats = await project_code_runs_once()
            if stats["synced"] or stats["errors"]:
                logger.info("code projection: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must survive
            logger.exception("code projection loop failed")
        await asyncio.sleep(max(5, settings.code_projection_interval_seconds))


__all__ = [
    "CodeEngineError",
    "CodeRunCreate",
    "audit_run",
    "code_projection_loop",
    "create_run",
    "get_run",
    "list_runs",
    "project_code_runs_once",
]
