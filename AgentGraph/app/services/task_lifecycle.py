"""Unified product task lifecycle (ADR-015).

AgentGraph is the only writer of a Product Task's status for execution-driven
changes.  Every other module (Runtime adapter, Kernel adapter, event
projection, scheduler) reads Runtime/Kernel state and calls this module.

Guarantees:

* **Single active Run** per task (``collab_tasks.active_run_id`` + partial
  unique index).  Starting a second Run returns the first one.
* **Monotonic** projection: a terminal product state is never rewritten by a
  late Run/Kernel event; review states are never failed by a stale result.
* **Idempotent**: every projection writes a ``lifecycle_projections`` row keyed
  by ``(source:ref:action)``; a replayed event is a no-op.
* **Evidence-gated**: a Run is only advanced to REVIEWING when the Kernel's
  durable evidence exists for the committed SHA.  A missing row is not a PASS.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, principal_for_admin
from app.config import get_settings
from app.errors import ApiError
from app.models import CollabTask, TaskStatus, Workspace
from app.schemas import (
    AcceptanceItem,  # noqa: F401  (documented dependency)
    GateResultRequest,
    HandoffBrief,
    TaskClaimRequest,
    TaskHandoffRequest,
)
from app.services import quality
from app.services import tasks as task_service
from app.services.agent_runtime import AgentRuntimeClient, AgentRuntimeError
from app.services.common import append_event, record_audit, task_snapshot, utcnow

logger = logging.getLogger("agentgraph.task_lifecycle")
settings = get_settings()

TERMINAL = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
REVIEW_STATES = {
    TaskStatus.REVIEWING,
    TaskStatus.HANDOFF,
    TaskStatus.AWAITING_HUMAN,
    TaskStatus.WAITING_HUMAN,
    TaskStatus.ESCALATED_TO_HUMAN,
}
# A task in one of these states may be moved by an execution result.
PROJECTABLE = {TaskStatus.TODO, TaskStatus.READY, TaskStatus.IN_PROGRESS}


class LifecycleError(RuntimeError):
    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


# ------------------------------------------------------------------ transitions
async def transition_task_fenced(
    session: AsyncSession,
    *,
    workspace_id: Any,
    task_key: str,
    new_status: TaskStatus | str,
    expected_epoch: int | None = None,
    expected_status: tuple[TaskStatus | str, ...] | None = None,
    clear_lease: bool = False,
    **fields: Any,
) -> dict[str, Any] | None:
    """Move one task's status under a compare-and-set, and report what happened.

    This is the guard the delivery paths call instead of writing
    ``UPDATE collab_tasks SET status`` by hand. ``expected_epoch`` is the
    lease epoch the caller believes it holds: a holder whose lease was taken
    over (epoch moved on) writes nowhere and gets ``None`` back, so the
    successor's state cannot be overwritten by a late writer.

    ``fields`` are extra column assignments (``review_feedback``,
    ``claimed_by``, ...) and go in the same statement, so a transition and its
    bookkeeping never split.
    """
    clauses = ["workspace_id = :ws", "task_key = :k"]
    params: dict[str, Any] = {"ws": workspace_id, "k": task_key,
                              "s": TaskStatus(new_status).value}
    if expected_epoch is not None:
        clauses.append("lease_epoch = :e")
        params["e"] = int(expected_epoch)
    if expected_status is not None:
        allowed = [TaskStatus(item).value for item in expected_status]
        clauses.append("status = ANY(CAST(:from AS task_status[]))")
        params["from"] = allowed
    assignments = ["status = :s", "version = version + 1", "updated_at = NOW()"]
    if clear_lease:
        assignments += ["claimed_by = NULL", "lease_owner = NULL", "lease_expires_at = NULL"]
    for index, (column, value) in enumerate(fields.items()):
        assignments.append(f"{column} = :f{index}")
        params[f"f{index}"] = value
    row = (await session.execute(
        text(f"UPDATE collab_tasks SET {', '.join(assignments)} "
             f"WHERE {' AND '.join(clauses)} "
             "RETURNING task_key, status::text, version, lease_epoch"),
        params,
    )).mappings().one_or_none()
    return dict(row) if row is not None else None


async def end_task_lease_fenced(
    session: AsyncSession, *, workspace_id: Any, task_key: str, epoch: int | None,
    reason: str,
) -> bool:
    """End the active Product lease of a task, at the epoch the caller holds."""
    clause, params = "", {"ws": workspace_id, "k": task_key, "r": reason[:2000]}
    if epoch is not None:
        clause, params["e"] = " AND t.lease_epoch = :e", int(epoch)
    ended = (await session.execute(text(
        "UPDATE leases l SET state = 'released', ended_at = NOW(), end_reason = :r "
        "FROM collab_tasks t WHERE l.resource_kind = 'task' "
        "AND l.resource_id = t.id AND l.state = 'active' "
        "AND t.workspace_id = :ws AND t.task_key = :k" + clause), params)).rowcount
    return bool(ended)


# ------------------------------------------------------------------ collisions
async def _once(
    session: AsyncSession,
    *,
    dedupe_key: str,
    task_key: str,
    action: str,
    run_id: str | None = None,
    event_seq: int | None = None,
) -> bool:
    """Claim a projection side effect exactly once.  True means 'do it'."""
    inserted = await session.execute(
        text(
            """
            INSERT INTO lifecycle_projections
                (dedupe_key, task_key, action, run_id, event_seq)
            VALUES (:key, :task_key, :action, :run_id, :event_seq)
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING dedupe_key
            """
        ),
        {
            "key": dedupe_key[:400],
            "task_key": task_key,
            "action": action,
            "run_id": str(run_id) if run_id else None,
            "event_seq": event_seq,
        },
    )
    row = inserted.first()
    return row is not None


async def _forget(session: AsyncSession, dedupe_key: str) -> None:
    """Release a projection claim when the side effect could not happen.

    Without this, a terminal event that arrived before the Kernel's evidence
    row would be remembered as "done" and never retried.
    """
    await session.execute(
        text("DELETE FROM lifecycle_projections WHERE dedupe_key = :key"),
        {"key": dedupe_key[:400]},
    )


# ------------------------------------------------------------------ bindings
async def kernel_binding_for(
    session: AsyncSession,
    workspace_id: Any,
    project: str | None,
    repo_path: str | None,
) -> dict[str, Any] | None:
    """Resolve ``(workspace, project, repo)`` to a Kernel workspace.

    Order: exact project+repo, exact repo (project ''), then the deployment
    default from settings.  A missing binding means "this repository is not
    wired to a Kernel yet" and the Run keeps working without code execution.
    """
    from app.services.kernel_binding_policy import validate_workspace_assignment

    rows = (
        await session.execute(
            text(
                """
                SELECT kernel_base_url, kernel_workspace_id, credential_ref,
                       worker_hub_id, project, repo_path
                FROM kernel_bindings
                WHERE workspace_id = :workspace_id AND status = 'ACTIVE'
                  AND repo_path = :repo AND project IN (:project, '')
                ORDER BY (project = :project) DESC, updated_at DESC
                LIMIT 2
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "project": project or "",
                "repo": repo_path or "",
            },
        )
    ).mappings().all()
    for row in rows:
        if row["repo_path"] == (repo_path or "") and row["project"] == (
            project or ""
        ):
            binding = _binding(row)
            try:
                validate_workspace_assignment(
                    str(workspace_id), binding["workspace_id"],
                    binding["base_url"], binding["credential_ref"])
            except ApiError:
                continue
            return binding
    for row in rows:
        if row["repo_path"] == (repo_path or "") and row["project"] == "":
            binding = _binding(row)
            try:
                validate_workspace_assignment(
                    str(workspace_id), binding["workspace_id"],
                    binding["base_url"], binding["credential_ref"])
            except ApiError:
                continue
            return binding
    if settings.kernel_url and settings.kernel_workspace_id:
        binding = {
            "base_url": settings.kernel_url,
            "workspace_id": settings.kernel_workspace_id,
            "credential_ref": "KERNEL_RUNTIME_TOKEN",
            "worker_hub_id": "veynod-runtime",
        }
        try:
            validate_workspace_assignment(
                str(workspace_id), binding["workspace_id"],
                binding["base_url"], binding["credential_ref"])
        except ApiError:
            return None
        return binding
    return None


def _binding(row: Any) -> dict[str, Any]:
    return {
        "base_url": row["kernel_base_url"],
        "workspace_id": str(row["kernel_workspace_id"]),
        "credential_ref": row["credential_ref"],
        "worker_hub_id": row["worker_hub_id"],
    }


async def upsert_kernel_binding(
    session: AsyncSession,
    *,
    workspace_id: Any,
    project: str,
    repo_path: str,
    kernel_base_url: str,
    kernel_workspace_id: str,
    credential_ref: str = "KERNEL_RUNTIME_TOKEN",
    worker_hub_id: str = "veynod-runtime",
) -> dict[str, Any]:
    from app.services.kernel_binding_policy import validate_binding

    kernel_base_url, credential_ref = validate_binding(kernel_base_url, credential_ref)
    row = (
        await session.execute(
            text(
                """
                INSERT INTO kernel_bindings
                    (workspace_id, project, repo_path, kernel_base_url,
                     kernel_workspace_id, credential_ref, worker_hub_id)
                VALUES (
                    :workspace_id, :project, :repo, :base_url,
                    CAST(:kernel_workspace AS UUID), :credential_ref,
                    :worker_hub_id
                )
                ON CONFLICT (workspace_id, project, repo_path) DO UPDATE
                SET kernel_base_url = EXCLUDED.kernel_base_url,
                    kernel_workspace_id = EXCLUDED.kernel_workspace_id,
                    credential_ref = EXCLUDED.credential_ref,
                    worker_hub_id = EXCLUDED.worker_hub_id,
                    status = 'ACTIVE',
                    updated_at = NOW()
                RETURNING binding_id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "project": project or "",
                "repo": repo_path,
                "base_url": kernel_base_url,
                "kernel_workspace": kernel_workspace_id,
                "credential_ref": credential_ref,
                "worker_hub_id": worker_hub_id,
            },
        )
    ).mappings().first()
    return dict(row) if row else {}


# ------------------------------------------------------------------ run start
def _client() -> AgentRuntimeClient:
    if not settings.agent_runtime_url:
        raise LifecycleError(
            "AGENT_RUNTIME_URL is not configured for this deployment", 503
        )
    return AgentRuntimeClient(
        settings.agent_runtime_url,
        settings.agent_runtime_key,
        settings.agent_runtime_timeout,
    )


async def start_run(
    session: AsyncSession,
    principal: Principal,
    task_key: str,
    *,
    budget: dict[str, Any] | None = None,
    goal: str | None = None,
) -> dict[str, Any]:
    """Start the one active Agent Run for this product task."""
    # Unlocked pre-check only: the authoritative single-run decision is made
    # under the row lock *after* the two Runtime HTTP calls, so the task row is
    # never held while a remote service is slow.
    task = (
        await session.execute(
            select(CollabTask).where(
                CollabTask.workspace_id == principal.workspace.id,
                CollabTask.task_key == task_key,
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise LifecycleError(f"{task_key} not found", 404)
    if task.status in TERMINAL:
        raise LifecycleError(f"{task_key} is {task.status.value}", 409)
    if task.status in REVIEW_STATES:
        raise LifecycleError(
            f"{task_key} is {task.status.value}; it is not open for execution", 409
        )
    existing = (task.correlation or {}).get("run_id")
    if task.active_run_id and existing:
        return await read_run(session, principal, task_key)
    # One execution owner: a task already claimed on a computer (with a live
    # product lease) belongs to that attempt.  A Run would provision a second
    # Kernel node for the same write set and stall behind the host's lease.
    if task.claimed_by and task.lease_owner and (
        task.status is TaskStatus.IN_PROGRESS
    ):
        raise LifecycleError(
            f"{task_key} is claimed by {task.claimed_by}; "
            "release it before starting a Run", 409
        )

    binding = await kernel_binding_for(
        session, principal.workspace.id, task.project, task.repo
    )
    from app.services.kernel_delivery import work_scope_for

    work_scope = work_scope_for(task)
    if work_scope is not None:
        # The Runtime may use its configured default Kernel endpoint when no
        # project binding exists. The frozen scope must survive that fallback.
        binding = {**(binding or {}), "work_scope": work_scope}
    trace_id = uuid4().hex
    client = _client()
    effective_budget = dict(budget or {})
    effective_budget.setdefault(
        "max_model_calls", settings.runtime_default_max_model_calls
    )
    effective_budget.setdefault(
        "max_cost_usd", settings.runtime_default_max_cost_usd
    )
    created = await client.create_run(
        tenant_id=str(principal.tenant_id),
        workspace_id=str(principal.workspace.id),
        product_task_id=task.task_key,
        goal=goal or task.description,
        budget=effective_budget,
        project_id=task.project,
        repo_path=task.repo,
        trace_id=trace_id,
        kernel=binding or {},
    )
    run_id = str(created.get("run_id") or "")
    if not run_id:
        raise LifecycleError("Agent Runtime did not return run_id", 502)

    async def _discard(reason: str) -> None:
        """Never leave a started Run with no product task pointing at it."""
        try:
            await client.cancel_run(run_id, reason)
        except Exception:  # noqa: BLE001 - compensation is best effort
            logger.exception("could not cancel orphan run %s", run_id)

    try:
        started = await client.start_run(run_id)
    except Exception:
        await _discard("run activation failed")
        raise

    try:
        # Re-lock only after the remote calls: the row lock is held for the
        # local transaction, not for the duration of two HTTP round trips.
        task = (
            await session.execute(
                select(CollabTask)
                .where(CollabTask.id == task.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if task is None:
            await _discard("product task disappeared")
            raise LifecycleError(f"{task_key} not found", 404)
        winner = (task.correlation or {}).get("run_id")
        if task.active_run_id or winner:
            # A concurrent call bound this task first; ours is a duplicate.
            await _discard("duplicate run for the same product task")
            await session.rollback()
            return await read_run(session, principal, task_key)

        before = task_snapshot(task)
        correlation = dict(task.correlation or {})
        correlation.update(
            {
                "run_id": run_id,
                "trace_id": trace_id,
                "kernel_workspace_id": (binding or {}).get("workspace_id"),
                "product_task_id": task.task_key,
                "updated_at": utcnow().isoformat(),
            }
        )
        task.correlation = correlation
        task.active_run_id = UUID(run_id)
        # The product status is not forced to IN_PROGRESS: that state carries a
        # human/agent lease by invariant (chk_in_progress_requires_lease).  The
        # Run is the execution truth until it completes and this module projects it.
        task.version += 1
        task.updated_at = utcnow()
        await session.flush()
        await record_audit(
            session,
            workspace_id=principal.workspace.id,
            session_batch_id=uuid4(),
            agent_signature=principal.agent_signature,
            target_table="collab_tasks",
            target_record_id=task.id,
            operation="UPDATE",
            before=before,
            after=task_snapshot(task),
        )
        await append_event(
            session,
            workspace_id=principal.workspace.id,
            agent=principal.agent_signature,
            event_type="TASK_RUN_STARTED",
            payload={
                "task_key": task.task_key,
                "run_id": run_id,
                "trace_id": trace_id,
                "kernel_workspace_id": correlation.get("kernel_workspace_id"),
                "runtime_status": started.get("status"),
            },
            namespace=task.namespace,
        )
        await session.commit()
    except IntegrityError:
        # The partial unique index on active_run_id is the final backstop for
        # two concurrent starts: keep the winner, drop ours.
        await session.rollback()
        await _discard("duplicate run for the same product task")
        return await read_run(session, principal, task_key)
    except Exception:
        await session.rollback()
        await _discard("binding the run to the product task failed")
        raise

    return {
        "task_key": task.task_key,
        "run_id": run_id,
        "trace_id": trace_id,
        "runtime_status": started.get("status"),
        "agentgraph_status": task.status.value,
        "budget": started.get("budget") or budget or {},
        "kernel": binding,
    }


# ------------------------------------------------------------------ run read
async def read_run(
    session: AsyncSession, principal: Principal, task_key: str
) -> dict[str, Any]:
    task = await load_task_row(session, principal.workspace.id, task_key)
    if task is None:
        raise LifecycleError(f"{task_key} not found", 404)
    correlation = task.correlation or {}
    run_id = correlation.get("run_id")
    if not run_id:
        raise LifecycleError(f"{task_key} has no Agent Run", 404)
    client = _client()
    run = await client.get_run(run_id)
    return {
        "task_key": task.task_key,
        "run_id": run_id,
        "trace_id": correlation.get("trace_id"),
        "runtime": run,
        "runtime_status": run.get("status"),
        "agentgraph_status": task.status.value,
        "kernel_workspace_id": correlation.get("kernel_workspace_id"),
        "kernel_task_id": correlation.get("kernel_task_id"),
    }


async def _kernel_task_id_for(session: AsyncSession, task: CollabTask) -> str | None:
    correlation = task.correlation or {}
    task_id = correlation.get("kernel_task_id")
    if task_id:
        return str(task_id)
    run_id = correlation.get("run_id")
    if not run_id:
        return None
    try:
        externals = await _client().externals(str(run_id))
    except AgentRuntimeError:
        return None
    for item in reversed(externals):
        if item.get("kernel_task_id"):
            return str(item["kernel_task_id"])
    return None


async def cancel_kernel_task(
    session: AsyncSession, task: CollabTask, reason: str
) -> bool:
    """Propagate a user cancellation into the Kernel before the task is closed."""
    task_id = await _kernel_task_id_for(session, task)
    if not task_id:
        return False
    binding = await kernel_binding_for(
        session, task.workspace_id, task.project, task.repo
    )
    if binding is None:
        return False
    if not settings.kernel_service_key:
        logger.warning(
            "KERNEL_SERVICE_KEY is not configured; kernel task %s not cancelled",
            task_id,
        )
        return False
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                binding["base_url"].rstrip("/")
                + f"/v1/runtime/tasks/{task_id}/cancel",
                headers={
                    "X-Veynod-Service-Key": settings.kernel_service_key,
                    "X-Workspace-Id": binding["workspace_id"],
                },
                json={"reason": reason[:4000]},
            )
        if response.status_code >= 400:
            logger.warning(
                "kernel cancel for %s returned %s: %s",
                task_id,
                response.status_code,
                response.text[:300],
            )
            return False
        return True
    except Exception:  # noqa: BLE001 - cancellation of the run still proceeds
        logger.exception("could not cancel kernel task %s", task_id)
        return False


async def trace_view(
    session: AsyncSession, principal: Principal, task_key: str
) -> dict[str, Any]:
    """One task_key -> every fact about it, from all three planes.

    This is the operator entry point: no querying three databases by hand.
    Sections are additive; a plane that is unreachable reports its error in
    place instead of failing the whole view.
    """
    task = await load_task_row(session, principal.workspace.id, task_key)
    if task is None:
        raise LifecycleError(f"{task_key} not found", 404)
    correlation = dict(task.correlation or {})
    run_id = correlation.get("run_id")
    binding = await kernel_binding_for(
        session, task.workspace_id, task.project, task.repo
    )
    view: dict[str, Any] = {
        "task": {
            "task_key": task.task_key,
            "title": task.title,
            "status": task.status.value,
            "version": task.version,
            "project": task.project,
            "repo": task.repo,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        },
        "correlation": correlation,
        "ids": {
            "tenant_id": str(principal.tenant_id),
            "workspace_id": str(principal.workspace.id),
            "project": task.project,
            "task_key": task.task_key,
            "run_id": run_id,
            "trace_id": correlation.get("trace_id"),
            "kernel_base_url": (binding or {}).get("base_url"),
            "kernel_workspace_id": correlation.get("kernel_workspace_id")
            or (binding or {}).get("workspace_id"),
            "kernel_task_id": correlation.get("kernel_task_id"),
            "submission_id": correlation.get("submission_id"),
            "commit_sha": correlation.get("commit_sha"),
        },
        "run": None,
        "kernel": None,
        "timeline": [],
    }
    timeline: list[dict[str, Any]] = []
    if run_id:
        client = _client()
        try:
            run = await client.get_run(str(run_id))
            steps = await client.steps(str(run_id))
            externals = await client.externals(str(run_id))
            events = await client.run_events(str(run_id))
            view["run"] = {
                "run_id": str(run_id),
                "status": run.get("status"),
                "version": run.get("version"),
                "usage": run.get("usage") or {},
                "budget": run.get("budget") or {},
                "last_error": run.get("last_error"),
                "steps": steps[-40:],
                "externals": externals,
                "event_count": len(events),
            }
            for event in events[-100:]:
                timeline.append(
                    {
                        "at": event.get("created_at"),
                        "source": "runtime",
                        "type": event.get("event_type"),
                        "detail": (event.get("payload") or {}).get("to")
                        or (event.get("payload") or {}).get("tool"),
                    }
                )
        except AgentRuntimeError as exc:
            view["run"] = {"error": str(exc)}
    kernel_task_id = correlation.get("kernel_task_id")
    if not kernel_task_id and view.get("run"):
        for item in reversed(view["run"].get("externals") or []):
            if item.get("kernel_task_id"):
                kernel_task_id = item["kernel_task_id"]
                break
    if kernel_task_id:
        from app.services import code_engine

        try:
            evidence = await code_engine._engine(
                (binding or {}).get("base_url")
            ).evidence(str(kernel_task_id))  # noqa: SLF001
            view["kernel"] = {
                "kernel_task_id": kernel_task_id,
                "submission": evidence.get("submission"),
                "merge_intent": evidence.get("merge_intent"),
                "delivery": evidence.get("delivery"),
                "gate_results": evidence.get("gate_results"),
                "evidence_complete": evidence.get("evidence_complete"),
            }
            submission = evidence.get("submission") or {}
            if submission.get("created_at"):
                timeline.append(
                    {
                        "at": submission.get("created_at"),
                        "source": "kernel",
                        "type": f"submission.{submission.get('status')}",
                        "detail": submission.get("submission_id"),
                    }
                )
            delivery = evidence.get("delivery") or {}
            if delivery.get("created_at"):
                timeline.append(
                    {
                        "at": delivery.get("created_at"),
                        "source": "kernel",
                        "type": "delivery.commit",
                        "detail": delivery.get("commit_sha"),
                    }
                )
        except Exception as exc:  # noqa: BLE001 - the view stays useful
            view["kernel"] = {"error": str(exc)}
    rows = (
        await session.execute(
            text(
                """
                SELECT event_type, payload, created_at
                FROM timeline_streams
                WHERE workspace_id = :workspace_id
                  AND payload->>'task_key' = :task_key
                ORDER BY created_at DESC
                LIMIT 100
                """
            ),
            {
                "workspace_id": str(principal.workspace.id),
                "task_key": task.task_key,
            },
        )
    ).mappings().all()
    for row in rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        timeline.append(
            {
                "at": row["created_at"].isoformat() if row["created_at"] else None,
                "source": "product",
                "type": row["event_type"],
                "detail": payload.get("run_id") or payload.get("reason"),
            }
        )
    timeline.sort(key=lambda item: str(item.get("at") or ""))
    view["timeline"] = timeline[-200:]
    return view


async def load_task_row(
    session: AsyncSession, workspace_id: Any, task_key: str
) -> CollabTask | None:
    """The ORM row (not the API projection) for lifecycle/projection code."""
    return (
        await session.execute(
            select(CollabTask).where(
                CollabTask.workspace_id == workspace_id,
                CollabTask.task_key == task_key,
            )
        )
    ).scalar_one_or_none()


async def _kernel_panel(session: AsyncSession, task: CollabTask) -> dict[str, Any] | None:
    """Live gate/merge for the console, including tasks that have no Agent Run."""
    from app.services.kernel_delivery import kernel_section, refresh_delivery

    delivery = (task.correlation or {}).get("kernel_delivery")
    kernel_task_id = (task.correlation or {}).get("kernel_task_id") or (
        (delivery or {}).get("kernel_task_id") if isinstance(delivery, dict) else None
    )
    if kernel_task_id:
        try:
            delivery = await refresh_delivery(session, task)
        except Exception as exc:  # noqa: BLE001 - the panel stays readable
            logger.warning("kernel delivery refresh failed for %s: %s", task.task_key, exc)
    return kernel_section(delivery if isinstance(delivery, dict) else None)


# ---------------------------------------------------------- execution view
async def execution_view(
    session: AsyncSession, principal: Principal, task_key: str
) -> dict[str, Any]:
    """Everything the Task Detail needs, from the three owners, read-only.

    The product task comes from AgentGraph, the loop from the Agent Runtime and
    the code/gate/commit facts from the Kernel.  No writes happen here.
    """
    task = await load_task_row(session, principal.workspace.id, task_key)
    if task is None:
        raise LifecycleError(f"{task_key} not found", 404)
    correlation = task.correlation or {}
    run_id = correlation.get("run_id")
    view: dict[str, Any] = {
        "task": {
            "task_key": task.task_key,
            "title": task.title,
            "status": task.status.value,
            "project": task.project,
            "repo": task.repo,
            "version": task.version,
            "updated_at": task.updated_at.isoformat()
            if task.updated_at
            else None,
        },
        "correlation": correlation,
        "loop": None,
        "code": None,
        "verification": None,
        "delivery": None,
        "kernel": None,
    }
    view["kernel"] = await _kernel_panel(session, task)
    if not run_id:
        return view
    client = _client()
    try:
        run = await client.get_run(str(run_id))
        steps = await client.steps(str(run_id))
        externals = await client.externals(str(run_id))
    except AgentRuntimeError as exc:
        view["loop"] = {"error": str(exc)}
        return view
    current = None
    for step in steps:
        if step.get("kind") == "TOOL":
            current = {
                "tool": (step.get("payload") or {}).get("tool"),
                "status": step.get("status"),
                "at": step.get("created_at"),
            }
    view["loop"] = {
        "run_id": str(run_id),
        "status": run.get("status"),
        "version": run.get("version"),
        "usage": run.get("usage") or {},
        "budget": run.get("budget") or {},
        "last_error": run.get("last_error"),
        "step_count": len(steps),
        "current_tool": current,
        "waiting_on": (
            "kernel_gate"
            if run.get("status") == "WAITING_EXTERNAL"
            else None
        ),
        "steps": steps[-30:],
        "externals": externals,
    }
    submission_id = next(
        (
            item.get("submission_id")
            for item in reversed(externals)
            if item.get("submission_id")
        ),
        correlation.get("submission_id"),
    )
    kernel_task_id = next(
        (
            item.get("kernel_task_id")
            for item in reversed(externals)
            if item.get("kernel_task_id")
        ),
        correlation.get("kernel_task_id"),
    )
    if kernel_task_id:
        from app.services import code_engine

        binding = await kernel_binding_for(
            session, task.workspace_id, task.project, task.repo
        )
        try:
            evidence = await code_engine._engine(
                (binding or {}).get("base_url")
            ).evidence(str(kernel_task_id))  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001 - the view stays readable
            evidence = {"error": str(exc)}
        view["code"] = {
            "kernel_task_id": kernel_task_id,
            "kernel_workspace_id": correlation.get("kernel_workspace_id"),
            "submission_id": submission_id,
            "commit_sha": (evidence.get("merge_intent") or {}).get(
                "merged_commit_sha"
            )
            or (evidence.get("submission") or {}).get("result_commit_sha"),
            "submission_status": (evidence.get("submission") or {}).get("status"),
        }
        view["verification"] = {
            "gate_results": evidence.get("gate_results") or [],
            "evidence_complete": evidence.get("evidence_complete", False),
        }
        view["delivery"] = evidence.get("delivery")
        from app.services.kernel_delivery import kernel_section

        view["kernel"] = kernel_section(
            (task.correlation or {}).get("kernel_delivery"), evidence
        ) or view["kernel"]
    return view


# -------------------------------------------------------------- projection
def _action_for(runtime_status: str, task_status: TaskStatus) -> str | None:
    if task_status not in PROJECTABLE:
        return None
    if runtime_status == "COMPLETED":
        return "review"
    if runtime_status == "FAILED":
        return "fail"
    if runtime_status == "CANCELLED":
        return "cancel"
    return None


async def project_run(
    session: AsyncSession,
    *,
    workspace: Workspace,
    task: CollabTask,
    run: dict[str, Any],
    source: str = "runtime",
) -> str | None:
    """Apply one Runtime observation to the product task. Returns the action."""
    runtime_status = str(run.get("status") or "")
    action = _action_for(runtime_status, task.status)
    if action is None:
        return None
    run_id = str(run.get("run_id") or "")
    dedupe_key = f"{source}:{run_id}:terminal:{runtime_status}"
    if not await _once(
        session,
        dedupe_key=dedupe_key,
        task_key=task.task_key,
        action=action,
        run_id=run_id,
        event_seq=int(run.get("event_seq") or 0) or None,
    ):
        await session.commit()
        return None

    admin = principal_for_admin(workspace)
    if action == "cancel":
        await cancel_kernel_task(session, task, "Agent Run was cancelled.")
        await _cancel(session, workspace, task, admin, "Agent Run was cancelled.")
        return action
    if action == "fail":
        await _fail(
            session,
            task,
            admin,
            str(run.get("last_error") or "Agent Run failed."),
        )
        return action

    # action == "review": only with durable Kernel evidence.
    evidence = await collect_evidence(session, task, run_id)
    if evidence is None:
        await _forget(session, dedupe_key)
        await append_event(
            session,
            workspace_id=workspace.id,
            agent="task_lifecycle",
            event_type="TASK_EVIDENCE_MISSING",
            payload={"task_key": task.task_key, "run_id": run_id},
            namespace=task.namespace,
        )
        await session.commit()
        logger.warning(
            "run %s completed without durable Kernel evidence; task %s stays %s",
            run_id,
            task.task_key,
            task.status.value,
        )
        return None
    commit_sha, updates, submission_status = evidence
    if commit_sha is None:
        # The Kernel rejected the final submission: record the failing gates
        # and fail the task; a human must not see a silently READY task.
        for update in updates:
            await quality.record_gate_result(
                session,
                admin,
                GateResultRequest(
                    task_key=task.task_key,
                    gate_kind=update["gate_kind"],
                    status=update["status"],
                    source="RUNNER",
                    evidence=update["evidence"],
                ),
            )
        await _fail(
            session,
            task,
            admin,
            f"the Kernel rejected the final submission ({submission_status or 'failed'})",
        )
        return "fail"
    for update in updates:
        if update["status"] != "PASS":
            await append_event(
                session,
                workspace_id=workspace.id,
                agent="task_lifecycle",
                event_type="TASK_GATE_FAILED",
                payload={
                    "task_key": task.task_key,
                    "run_id": run_id,
                    "gate_kind": update["gate_kind"],
                },
                namespace=task.namespace,
            )
        await quality.record_gate_result(
            session,
            admin,
            GateResultRequest(
                task_key=task.task_key,
                gate_kind=update["gate_kind"],
                status=update["status"],
                source="RUNNER",
                evidence=update["evidence"],
            ),
        )
    if any(update["status"] != "PASS" for update in updates):
        # The Run is over and its final evidence contains a failed gate: the
        # execution did not deliver.  A human sees FAILED, never a silent READY.
        failed = [u["gate_kind"] for u in updates if u["status"] != "PASS"]
        await _fail(
            session,
            task,
            admin,
            f"the final deterministic gate failed: {', '.join(failed)}",
        )
        return "fail"
    correlation = dict(task.correlation or {})
    correlation["submission_id"] = run.get("submission_id") or correlation.get(
        "submission_id"
    )
    correlation["kernel_task_id"] = run.get("kernel_task_id") or correlation.get(
        "kernel_task_id"
    )
    correlation["commit_sha"] = commit_sha
    task.correlation = correlation
    await _review(session, task, admin, commit_sha, updates)
    return action


async def collect_evidence(
    session: AsyncSession, task: CollabTask, run_id: str
) -> tuple[str | None, list[dict[str, Any]], str] | None:
    """Read Runtime external results + Kernel evidence for a completed Run.

    Returns ``(commit_sha|None, gate_updates, submission_status)``.  A failed
    submission is *evidence of failure*, not missing evidence: the caller must
    fail the task instead of leaving it READY forever.
    """
    client = _client()
    try:
        externals = await client.externals(run_id)
    except AgentRuntimeError as exc:
        logger.warning("runtime external lookup failed for %s: %s", run_id, exc)
        return None
    submissions = [
        item
        for item in externals
        if item.get("status") == "FINAL" and item.get("submission_id")
    ]
    if not submissions:
        return None
    latest = submissions[-1]
    kernel_task_id = latest.get("kernel_task_id")
    if not kernel_task_id:
        return None
    from app.services import code_engine

    binding = await kernel_binding_for(
        session, task.workspace_id, task.project, task.repo
    )
    try:
        evidence = await code_engine._engine(
            (binding or {}).get("base_url")
        ).evidence(str(kernel_task_id))  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001 - a missing row is not a PASS
        logger.warning(
            "kernel evidence unavailable for %s: %s", kernel_task_id, exc
        )
        return None
    submission = evidence.get("submission") or {}
    intent = evidence.get("merge_intent") or {}
    status = str(submission.get("status") or intent.get("status") or "")
    commit_sha = (
        intent.get("merged_commit_sha")
        or submission.get("result_commit_sha")
        or evidence.get("result_commit_sha")
    )
    if commit_sha:
        updates = code_engine.gate_updates_from_evidence(evidence, str(commit_sha))
        if updates:
            return str(commit_sha), updates, status
    raw = evidence.get("gate_results") or []
    if status in {"FAILED", "REJECTED", "CANCELLED"} and raw:
        updates = [
            {
                "gate_kind": "TESTS"
                if str(item.get("stage") or "") == "UNIT_TEST_SUITE"
                else "CI",
                "status": "PASS" if item.get("passed") is True else "FAIL",
                "evidence": {
                    "run_url": f"veynod-kernel://submission/{submission.get('submission_id', '')}",
                    "summary": f"Kernel gate {item.get('stage')} failed",
                    "stages": [str(item.get("stage"))],
                    "diagnostics": str(item.get("diagnostics") or "")[:2000],
                },
            }
            for item in raw
            if isinstance(item, dict)
        ]
        return None, updates, status or "FAILED"
    return None


# ------------------------------------------------- legacy Kernel projection
async def project_kernel_task(
    session: AsyncSession,
    principal: Principal,
    task: CollabTask,
    kernel: dict[str, Any],
) -> CollabTask:
    """Project a Kernel task that has no Agent Run (legacy code plane).

    Tasks driven by the Runtime are projected from the Run; this path only
    exists so historical Kernel-only tasks keep converging.  It is the same
    monotonic, deduped contract as everything else.
    """
    kernel_status = str(kernel.get("status") or "")
    action = {
        "CANCELLED": "cancel",
        "FAILED": "fail",
        "COMMITTED": "commit",
    }.get(kernel_status)
    if action is None or task.status not in PROJECTABLE:
        return task
    kernel_task_id = str(kernel.get("task_id") or "")
    dedupe_key = f"kernel:{kernel_task_id}:{kernel_status}"
    if not await _once(
        session,
        dedupe_key=dedupe_key,
        task_key=task.task_key,
        action=action,
    ):
        await session.commit()
        return task
    admin = principal_for_admin(principal.workspace)
    if action == "cancel":
        await _cancel(
            session,
            principal.workspace,
            task,
            admin,
            "Veynod Kernel task was cancelled.",
        )
        await session.commit()
        return await task_service.get_task(session, principal, task.task_key)
    if action == "fail":
        await _fail(
            session,
            task,
            admin,
            str(kernel.get("last_error_traceback") or "Veynod Kernel task failed."),
        )
        await session.commit()
        return await task_service.get_task(session, principal, task.task_key)
    commit_sha = kernel.get("result_commit_sha")
    if not commit_sha or not kernel_task_id:
        await _forget(session, dedupe_key)
        await session.commit()
        return task
    from app.services import code_engine

    try:
        evidence = await code_engine._engine().evidence(kernel_task_id)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001 - missing evidence is not a PASS
        logger.warning("kernel evidence unavailable for %s: %s", kernel_task_id, exc)
        await _forget(session, dedupe_key)
        await session.commit()
        return task
    updates = code_engine.gate_updates_from_evidence(evidence, str(commit_sha))
    kinds = {update["gate_kind"] for update in updates}
    if not updates or not {"CI", "TESTS"}.issubset(kinds):
        await _forget(session, dedupe_key)
        await session.commit()
        return task
    for update in updates:
        await quality.record_gate_result(
            session,
            admin,
            GateResultRequest(
                task_key=task.task_key,
                gate_kind=update["gate_kind"],
                status=update["status"],
                source="RUNNER",
                evidence=update["evidence"],
            ),
        )
    if any(update["status"] != "PASS" for update in updates):
        await session.commit()
        return task
    try:
        await _review(session, task, admin, str(commit_sha), updates)
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - evidence still stands
        logger.info("kernel lifecycle not advanced for %s: %s", task.task_key, exc)
        await session.rollback()
    return await task_service.get_task(session, principal, task.task_key)


# ------------------------------------------------------------- transitions
async def _review(
    session: AsyncSession,
    task: CollabTask,
    admin: Principal,
    commit_sha: str,
    updates: list[dict[str, Any]],
) -> None:
    await task_service.claim_task(
        session,
        admin,
        task.task_key,
        TaskClaimRequest(lease_seconds=120, include_context=False),
    )
    await task_service.handoff_task(
        session,
        admin,
        task.task_key,
        TaskHandoffRequest(
            next_status="REVIEWING",
            brief=HandoffBrief(
                summary=(
                    f"Agent Run committed {commit_sha[:12]}; "
                    f"{len(updates)} durable gate result(s) recorded."
                ),
                key_metrics={
                    "commit_sha": commit_sha,
                    "gates_recorded": len(updates),
                    "gates_passed": sum(
                        1 for update in updates if update["status"] == "PASS"
                    ),
                },
                unverified_hypotheses=[],
                risk_flag=False,
            ),
        ),
    )
    await session.execute(
        text("UPDATE collab_tasks SET active_run_id = NULL WHERE id = :id"),
        {"id": task.id},
    )
    await session.commit()


async def _fail(
    session: AsyncSession, task: CollabTask, admin: Principal, reason: str
) -> None:
    await task_service.fail_task(
        session,
        admin,
        task.task_key,
        reason=(reason or "execution failed")[:8000],
        expected_version=task.version,
    )
    await session.execute(
        text("UPDATE collab_tasks SET active_run_id = NULL WHERE id = :id"),
        {"id": task.id},
    )
    await session.commit()


async def _cancel(
    session: AsyncSession,
    workspace: Workspace,
    task: CollabTask,
    admin: Principal,
    reason: str,
) -> None:
    await task_service.cancel_task(
        session,
        workspace.id,
        task.task_key,
        reason=reason,
        expected_version=task.version,
        actor="task_lifecycle",
        propagate_to_kernel=False,
    )
    await session.execute(
        text("UPDATE collab_tasks SET active_run_id = NULL WHERE id = :id"),
        {"id": task.id},
    )
    await session.commit()


async def cancel_run(
    session: AsyncSession,
    principal: Principal,
    task_key: str,
    reason: str,
) -> dict[str, Any]:
    task = await load_task_row(session, principal.workspace.id, task_key)
    if task is None:
        raise LifecycleError(f"{task_key} not found", 404)
    correlation = task.correlation or {}
    run_id = correlation.get("run_id")
    if run_id:
        try:
            run = await _client().get_run(run_id)
            if run.get("status") not in {
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                "TIMED_OUT",
            }:
                await _client().cancel_run(run_id, reason)
        except AgentRuntimeError as exc:
            logger.warning("could not cancel runtime run %s: %s", run_id, exc)
    if task.status not in TERMINAL:
        await cancel_kernel_task(session, task, reason)
        admin = principal_for_admin(principal.workspace)
        await _cancel(session, principal.workspace, task, admin, reason)
        await session.commit()
    refreshed = await task_service.get_task(session, principal, task.task_key)
    return {
        "task_key": refreshed.task_key,
        "run_id": run_id,
        "agentgraph_status": refreshed.status.value,
    }


async def reconcile_active_runs(limit: int = 200) -> dict[str, int]:
    """Project every non-terminal task whose Run is terminal (belt and braces).

    The event inbox is the primary path; this bounded sweep only covers missed
    deliveries.  It never advances a task without Kernel evidence.
    """
    from app.database import async_session_factory

    stats = {"scanned": 0, "projected": 0, "skipped": 0, "errors": 0}
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(CollabTask.id, CollabTask.workspace_id)
                .where(
                    CollabTask.active_run_id.is_not(None),
                    CollabTask.status.in_(list(PROJECTABLE)),
                )
                .order_by(CollabTask.updated_at.asc())
                .limit(max(1, min(1000, limit)))
            )
        ).all()
    stats["scanned"] = len(rows)
    for task_id, workspace_id in rows:
        try:
            async with async_session_factory() as session:
                task = await session.get(CollabTask, task_id)
                workspace = await session.get(Workspace, workspace_id)
                if task is None or workspace is None or not task.active_run_id:
                    stats["skipped"] += 1
                    continue
                run = await _client().get_run(str(task.active_run_id))
                action = await project_run(
                    session, workspace=workspace, task=task, run=run
                )
                if action:
                    stats["projected"] += 1
                else:
                    stats["skipped"] += 1
        except Exception:  # noqa: BLE001 - one task must not stop the sweep
            stats["errors"] += 1
            logger.exception("lifecycle sweep failed for task %s", task_id)
    return stats


async def project_run_event(
    run_id: str, workspace_id: str, event_type: str, payload: dict[str, Any]
) -> dict[str, int]:
    """Project one Runtime event (from the durable inbox) onto its task."""
    from app.database import async_session_factory

    if event_type not in {
        "AgentRunStateChanged",
        "AgentRunCompleted",
        "AgentRunFailed",
    }:
        return {"projected": 0, "skipped": 1}
    status = str(payload.get("to") or payload.get("status") or "")
    if status not in {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}:
        return {"projected": 0, "skipped": 1}
    async with async_session_factory() as session:
        task = (
            await session.execute(
                select(CollabTask)
                .where(CollabTask.active_run_id == UUID(run_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if task is None:
            return {"projected": 0, "skipped": 1}
        workspace = await session.get(Workspace, task.workspace_id)
        if workspace is None:
            return {"projected": 0, "skipped": 1}
        run = await _client().get_run(run_id)
        action = await project_run(
            session,
            workspace=workspace,
            task=task,
            run=run,
            source="runtime-inbox",
        )
        return {"projected": 1 if action else 0, "skipped": 0 if action else 1}


__all__ = [
    "LifecycleError",
    "cancel_kernel_task",
    "cancel_run",
    "execution_view",
    "trace_view",
    "load_task_row",
    "collect_evidence",
    "kernel_binding_for",
    "project_kernel_task",
    "project_run",
    "project_run_event",
    "read_run",
    "reconcile_active_runs",
    "start_run",
    "upsert_kernel_binding",
]
