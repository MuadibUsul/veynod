"""Place tasks on runner hosts (full-managed Loop, step 3: runner-3-scheduler).

Every reaper cycle, for each workspace that has runner hosts:

1. Open placements are reconciled with their task and host. A host that took
   the task makes the placement ``claimed``; a task that reached review or
   beyond makes it ``delivered``; a task that came back to READY makes it
   ``failed``. A host that went offline, ran out of quota, was revoked or did
   not claim in time loses the task (``moved``, with the reason): its lease
   is released and the task is free for another host.
2. Free READY tasks are matched to hosts: the host's token role, namespaces
   and the directory capabilities must fit the task, the host must reach the
   task's repository, have a free slot and quota left, and must not have
   failed this task twice. The best host has the most free slots and the
   most quota left.
3. Under full automation the task is assigned to the host's agent; the
   assignment and a ``task.placed`` poke (carrying ``placement_id`` as the
   idempotency key) wake the host. Under manual and semi automation the pick
   is only recorded as a suggestion.
4. A task with no host is left READY and the placement says why, in words
   the owner can act on; the Loop page shows it under "Needs you".
5. After three failed rounds a task stays in HANDOFF instead of being placed
   again; nobody is pinged (full-automation spec A-04).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError, Forbidden, NotFound
from app.models import CollabTask, TaskStatus
from app.services import automation
from app.services.common import append_event, record_audit, task_snapshot
from app.services.runners import ONLINE_WINDOW

logger = logging.getLogger("agentgraph.scheduler")
settings = get_settings()

OPEN = ("suggested", "offered", "claimed", "unplaced")
# Where a task is once a host is done with it.
PAST_WORK = {TaskStatus.REVIEWING, TaskStatus.HANDOFF, TaskStatus.DONE,
             TaskStatus.AWAITING_HUMAN, TaskStatus.WAITING_HUMAN,
             TaskStatus.ESCALATED_TO_HUMAN}
MAX_ROUNDS = 3
FAILS_PER_HOST = 2
# Why a host lost a work placement, when it is the host's own doing.
COUNTED_AGAINST_HOST = {"not_claimed", "reported_failed"}
# One scheduling pass at a time, whatever the number of server processes.
_LOCK_SQL = "SELECT pg_try_advisory_lock(hashtext('veynod_scheduler')::bigint)"
_UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtext('veynod_scheduler')::bigint)"
# Reasons a task has no host, most specific last, in words for the owner.
UNPLACED = {
    "no_host_online": "No runner is online.",
    "no_host_for_role": "No online runner can take {role} tasks.",
    "no_host_for_namespace": "No online runner may work in the {namespace} namespace.",
    "no_host_with_capabilities": "No online runner has {capabilities}.",
    "no_host_for_repo": "No online runner can reach {repo}.",
    "no_host_with_runtime": "No online runner has Claude Code or Codex ready.",
    "every_host_failed": "Every runner that could take it already failed it twice.",
    "hosts_out_of_quota": "Every runner that could take it is out of quota.",
    "hosts_busy": "Every runner that could take it is busy; it waits for a free slot.",
    "no_reviewer_host": "No reviewer computer is online that did not build it.",
    "no_supervisor_host": "No supervisor computer is online to look after the Loop.",
}
# Reasons that wait on their own; the rest need the owner.
SELF_RESOLVING = {"hosts_busy"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def quota_used(host: dict[str, Any]) -> float | None:
    """The fullest quota window of the host's best ready runtime (0-100), or None."""
    best: float | None = None
    for runtime in host["runtimes"] or []:
        if runtime.get("available") is False:
            continue
        windows = (runtime.get("usage") or {}).get("windows") or []
        used = max((float(window.get("used_percent") or 0) for window in windows), default=0.0)
        best = used if best is None else min(best, used)
    return best


def _host_problem(host: dict[str, Any], now: datetime) -> str | None:
    """Why a host must give its tasks back, if it must."""
    if host["status"] == "revoked":
        return "host_revoked"
    if host["status"] == "paused":
        return "host_paused"
    if now - host["last_seen_at"] > timedelta(seconds=settings.runner_offline_reassign_seconds):
        return "host_offline"
    used = quota_used(host)
    if used is not None and used >= settings.runner_quota_exhausted_percent:
        return "host_out_of_quota"
    return None


async def _hosts(session: AsyncSession, workspace_id: Any) -> list[dict[str, Any]]:
    rows = (await session.execute(text(
        "SELECT h.*, t.role_scope, t.allowed_namespaces, t.agent_signature, "
        "t.is_active AS token_active, t.revoked_at AS token_revoked_at, "
        "COALESCE(d.capabilities, '{}') AS capabilities "
        "FROM runner_hosts h JOIN agent_tokens t ON t.canonical_id = h.token_id "
        "AND t.workspace_id = h.workspace_id "
        "LEFT JOIN agent_directory d ON d.workspace_id = h.workspace_id "
        "AND d.agent_id = h.agent_id WHERE h.workspace_id = :ws"),
        {"ws": workspace_id})).mappings().all()
    hosts = []
    for row in rows:
        host = dict(row)
        if not host["token_active"] or host["token_revoked_at"] is not None:
            host["status"] = "revoked"
        hosts.append(host)
    return hosts


async def _end(session: AsyncSession, placement: dict[str, Any], status: str,
               reason: str | None = None, note: str | None = None) -> None:
    await session.execute(text(
        "UPDATE runner_placements SET status = CAST(:status AS text), "
        "reason = COALESCE(CAST(:reason AS text), reason), "
        "note = COALESCE(CAST(:note AS text), note), updated_at = NOW(), "
        "ended_at = CASE WHEN CAST(:status AS text) IN "
        "('suggested', 'offered', 'claimed', 'unplaced') "
        "THEN NULL ELSE NOW() END WHERE placement_id = :id"),
        {"status": status, "reason": reason, "note": note and note[:500],
         "id": placement["placement_id"]})
    placement["status"] = status


async def _unassign(session: AsyncSession, task: CollabTask, actor: str) -> None:
    before = task_snapshot(task)
    task.assignee = None
    task.version += 1
    task.updated_at = _now()
    await session.flush()
    await record_audit(session, workspace_id=task.workspace_id, session_batch_id=uuid.uuid4(),
                       agent_signature=actor, target_table="collab_tasks",
                       target_record_id=task.id, operation="UPDATE", before=before,
                       after=task_snapshot(task))


async def _take_back(session: AsyncSession, task: CollabTask, host: dict[str, Any],
                     reason: str) -> None:
    """The host cannot finish: release its lease (if it has one) and unassign."""
    if task.status == TaskStatus.IN_PROGRESS and task.lease_owner:
        from app.services.leases import orphan_task

        await orphan_task(session, task, state="revoked", reason=reason, actor="scheduler")
    if task.assignee == f"agent:{host['agent_id']}":
        await _unassign(session, task, "scheduler")


def _holds(task: CollabTask, host: dict[str, Any]) -> bool:
    return bool(host["agent_signature"]) and host["agent_signature"] in (
        task.claimed_by, task.lease_owner)


async def _reconcile(session: AsyncSession, workspace_id: Any,
                     hosts: dict[str, dict[str, Any]], now: datetime,
                     mode: str) -> dict[str, int]:
    counts = {"claimed": 0, "delivered": 0, "failed": 0, "moved": 0, "withdrawn": 0}
    rows = (await session.execute(text(
        "SELECT * FROM runner_placements WHERE workspace_id = :ws AND status IN "
        "('suggested', 'offered', 'claimed', 'unplaced')"),
        {"ws": workspace_id})).mappings().all()
    chunks = await _chunk_states(session, workspace_id)
    for row in rows:
        placement = dict(row)
        host = hosts.get(placement["host_id"]) if placement["host_id"] else None
        if placement["step"] != "work":
            outcome = await _reconcile_step(session, workspace_id, placement, host, chunks, now)
            if outcome:
                counts[outcome] = counts.get(outcome, 0) + 1
            continue
        task = await session.get(CollabTask, placement["task_id"], with_for_update=True)
        status = placement["status"]
        if task is None:
            continue
        if status in ("suggested", "unplaced"):
            # Kept while the task still waits; the pass below refreshes or replaces it.
            if not (task.status == TaskStatus.READY and task.assignee is None
                    and task.claimed_by is None):
                await _end(session, placement, "withdrawn")
            continue
        assigned_here = host is not None and task.assignee == f"agent:{host['agent_id']}"
        if host is None:
            await _end(session, placement, "moved", "host_removed")
            counts["moved"] += 1
            continue
        # A host that claimed (the lease epoch moved) counts as having had the
        # task, even if it gave it back before this pass saw the claim.
        taken = (task.lease_epoch or 0) > (placement["lease_epoch"] or 0)
        if status == "offered" and (_holds(task, host) or taken):
            await _end(session, placement, "claimed")
            status = "claimed"
            counts["claimed"] += 1
        if task.status in PAST_WORK:
            await _end(session, placement, "delivered", task.status.value.lower())
            counts["delivered"] += 1
            continue
        if task.status in (TaskStatus.DONE, TaskStatus.CANCELLED, TaskStatus.FAILED,
                           TaskStatus.TODO):
            await _end(session, placement, "withdrawn", task.status.value.lower())
            counts["withdrawn"] += 1
            continue
        if not assigned_here and not _holds(task, host):
            # Someone else took or reassigned it.
            await _end(session, placement, "superseded",
                       "unassigned" if task.assignee is None else "reassigned")
            continue
        if status == "claimed" and task.status == TaskStatus.READY:
            # The host had it and it came back: rejected, released or lease lost.
            reason = "lease_lost" if task.orphaned_from == host["agent_signature"] else "returned"
            await _end(session, placement, "failed", reason)
            if assigned_here:
                await _unassign(session, task, "scheduler")
            counts["failed"] += 1
            continue
        problem = _host_problem(host, now)
        if problem is None and status == "offered" and placement.get("reported") == "failed":
            # The runner said it will not take it: a full slot is the host being busy
            # (not its fault), anything else is a failed start.
            problem = ("host_busy" if (placement.get("report_note") or "") == "no_free_slot"
                       else "reported_failed")
        if problem is None and status == "offered" and now - placement["created_at"] > timedelta(
                seconds=settings.runner_claim_timeout_seconds):
            problem = "not_claimed"
        if problem:
            await _take_back(session, task, host, problem)
            # Not taking it, or failing to start it, counts against the host; an
            # outage or a busy host does not.
            counted = problem in COUNTED_AGAINST_HOST
            await _end(session, placement, "failed" if counted else "moved", problem)
            counts["failed" if counted else "moved"] += 1
            await append_event(
                session, workspace_id=workspace_id, agent="scheduler",
                event_type="TASK_UNPLACED",
                payload={"task_key": task.task_key, "host_id": host["host_id"],
                         "placement_id": placement["placement_id"], "reason": problem,
                         "targets": [f"agent:{host['agent_id']}"]},
                namespace=task.namespace)
    return counts


def _fits(task: CollabTask, host: dict[str, Any]) -> list[str]:
    """The filters a host fails for a task, in UNPLACED order."""
    misses = []
    role = task.required_role or task.assigned_role
    if role and host["role_scope"] != role:
        misses.append("no_host_for_role")
    namespaces = list(host["allowed_namespaces"] or [])
    if "*" not in namespaces and task.namespace not in namespaces:
        misses.append("no_host_for_namespace")
    needed = {item.lower() for item in (task.required_capabilities or [])}
    have = {item.lower() for item in host["capabilities"] or []} | {
        runtime["id"] for runtime in host["runtimes"] or []}
    if needed - have:
        misses.append("no_host_with_capabilities")
    if task.repo and task.repo.lower() not in {repo.lower() for repo in host["repos"] or []}:
        misses.append("no_host_for_repo")
    if quota_used(host) is None:
        misses.append("no_host_with_runtime")
    return misses


def _choose(task: CollabTask, candidates: list[dict[str, Any]],
            failures: dict[str, int], load: dict[str, int]) -> tuple[dict[str, Any] | None, str]:
    """The best host, or the reason there is none."""
    if not candidates:
        return None, "no_host_online"
    pool = candidates
    reason = "no_host_online"
    for check in ("no_host_for_role", "no_host_for_namespace", "no_host_with_capabilities",
                  "no_host_for_repo", "no_host_with_runtime"):
        narrowed = [host for host in pool if check not in _fits(task, host)]
        if not narrowed:
            return None, check
        pool = narrowed
    pool = [host for host in pool if failures.get(host["host_id"], 0) < FAILS_PER_HOST]
    if not pool:
        return None, "every_host_failed"
    pool = [host for host in pool
            if (quota_used(host) or 0) < settings.runner_quota_exhausted_percent]
    if not pool:
        return None, "hosts_out_of_quota"
    pool = [host for host in pool if host["max_concurrency"] - load[host["host_id"]] > 0]
    if not pool:
        return None, "hosts_busy"
    best = min(pool, key=lambda host: (-(host["max_concurrency"] - load[host["host_id"]]),
                                       quota_used(host) or 0.0, host["host_id"]))
    return best, reason


def _words(reason: str, task: CollabTask) -> str:
    return UNPLACED[reason].format(
        role=task.required_role or task.assigned_role or "EXECUTOR", namespace=task.namespace,
        repo=task.repo or "", capabilities=", ".join(task.required_capabilities or []))


def _online(hosts: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    return [host for host in hosts if host["status"] == "active"
            and now - host["last_seen_at"] <= ONLINE_WINDOW]


async def _load(session: AsyncSession, workspace_id: Any,
                hosts: list[dict[str, Any]]) -> dict[str, int]:
    """Busy slots per host: open offers and claims, or what the host says it runs."""
    load = {host["host_id"]: 0 for host in hosts}
    for host_id, active in (await session.execute(text(
            "SELECT host_id, count(*) FROM runner_placements WHERE workspace_id = :ws "
            "AND status IN ('offered', 'claimed') GROUP BY host_id"),
            {"ws": workspace_id})).all():
        if host_id in load:
            load[host_id] = active
    for host in hosts:
        load[host["host_id"]] = max(load[host["host_id"]], len(host["running"] or []))
    return load


async def _work_failures(session: AsyncSession, task: CollabTask) -> tuple[dict[str, int], int]:
    """Hosts to avoid for this task (failed twice, or moved off by the supervisor)
    and the failed rounds so far. A supervisor's move does not count as a round."""
    rows = (await session.execute(text(
        "SELECT host_id, status, count(*) FROM runner_placements WHERE task_id = :t "
        "AND step = 'work' AND host_id IS NOT NULL AND (status = 'failed' "
        "OR (status = 'moved' AND reason = 'supervisor_moved')) GROUP BY host_id, status"),
        {"t": task.id})).all()
    failures: dict[str, int] = {}
    rounds = 0
    for host_id, status, count in rows:
        if status == "failed":
            failures[host_id] = failures.get(host_id, 0) + count
            rounds += count
        else:
            failures[host_id] = FAILS_PER_HOST
    return failures, rounds


async def _held(session: AsyncSession, task: CollabTask) -> bool:
    """The supervisor withdrew the task from the computers and nobody took it since."""
    latest = (await session.execute(text(
        "SELECT status, reason, lease_epoch FROM runner_placements WHERE task_id = :t "
        "AND step = 'work' ORDER BY created_at DESC LIMIT 1"), {"t": task.id})
    ).mappings().one_or_none()
    return (latest is not None and latest["status"] == "withdrawn"
            and latest["reason"] == "held"
            and (latest["lease_epoch"] or 0) >= (task.lease_epoch or 0))


async def _offer_work(session: AsyncSession, task: CollabTask, host: dict[str, Any],
                      now: datetime, actor: str = "scheduler") -> str:
    """Assign the task to the host's agent and wake it with a task.placed poke."""
    placement_id = (await session.execute(text(
        "INSERT INTO runner_placements (workspace_id, task_id, task_key, host_id, agent_id, "
        "mode, status, lease_epoch) VALUES (:ws, :t, :key, :host, :agent, 'assign', "
        "'offered', :epoch) RETURNING placement_id"),
        {"ws": task.workspace_id, "t": task.id, "key": task.task_key, "host": host["host_id"],
         "agent": host["agent_id"], "epoch": task.lease_epoch or 0})).scalar_one()
    assignee = f"agent:{host['agent_id']}"
    before = task_snapshot(task)
    task.assignee = assignee
    task.version += 1
    task.updated_at = now
    await session.flush()
    await record_audit(session, workspace_id=task.workspace_id, session_batch_id=uuid.uuid4(),
                       agent_signature=actor, target_table="collab_tasks",
                       target_record_id=task.id, operation="UPDATE", before=before,
                       after=task_snapshot(task))
    await append_event(
        session, workspace_id=task.workspace_id, agent=actor, event_type="TASK_PLACED",
        payload={"task_key": task.task_key, "host_id": host["host_id"],
                 "placement_id": placement_id, "idempotency_key": placement_id,
                 "targets": [assignee],
                 "details": {"delivery_kind": "task.placed", "priority": 1,
                             "note": f"placement {placement_id}"}},
        namespace=task.namespace)
    return placement_id


async def run_pass(session: AsyncSession, workspace_id: Any) -> dict[str, Any]:
    """One scheduling pass over a workspace. Commits."""
    hosts = await _hosts(session, workspace_id)
    if not any(host["status"] != "revoked" for host in hosts):
        return {"mode": None, "hosts": 0, "skipped": "no runner hosts"}
    mode = await automation.current_mode(session, workspace_id)
    now = _now()
    by_id = {host["host_id"]: host for host in hosts}
    counts = await _reconcile(session, workspace_id, by_id, now, mode)

    online = _online(hosts, now)
    load = await _load(session, workspace_id, hosts)

    tasks = (await session.execute(
        select(CollabTask).where(
            CollabTask.workspace_id == workspace_id,
            CollabTask.status == TaskStatus.READY,
            CollabTask.assignee.is_(None),
            CollabTask.claimed_by.is_(None),
            (CollabTask.kind.is_(None)) | (CollabTask.kind == "work"),
        ).order_by(CollabTask.priority.desc(), CollabTask.created_at)
        .limit(100).with_for_update(skip_locked=True))).scalars().all()
    placed = suggested = unplaced = gave_up = 0
    for task in tasks:
        if await _held(session, task):
            continue
        failures, rounds = await _work_failures(session, task)
        if rounds >= MAX_ROUNDS and mode == "full":
            before = task_snapshot(task)
            task.status = TaskStatus.HANDOFF
            task.version += 1
            task.updated_at = now
            await session.flush()
            await record_audit(session, workspace_id=workspace_id,
                               session_batch_id=uuid.uuid4(), agent_signature="scheduler",
                               target_table="collab_tasks", target_record_id=task.id,
                               operation="UPDATE", before=before, after=task_snapshot(task))
            await append_event(
                session, workspace_id=workspace_id, agent="scheduler",
                event_type="TASK_SCHEDULER_STOPPED",
                payload={"task_key": task.task_key, "rounds": rounds,
                         "note": "failed on hosts three times; left in HANDOFF"},
                namespace=task.namespace)
            gave_up += 1
            continue
        host, reason = _choose(task, online, failures, load)
        waiting = (await session.execute(text(
            "SELECT * FROM runner_placements WHERE task_id = :t AND step = 'work' "
            "AND status IN ('suggested', 'unplaced')"), {"t": task.id})).mappings().one_or_none()
        if waiting is not None:
            same = ((host is None and waiting["status"] == "unplaced"
                     and waiting["reason"] == reason)
                    or (host is not None and mode != "full" and waiting["status"] == "suggested"
                        and waiting["host_id"] == host["host_id"]))
            if same:
                await session.execute(text(
                    "UPDATE runner_placements SET updated_at = NOW() "
                    "WHERE placement_id = :id"), {"id": waiting["placement_id"]})
                if host is None:
                    unplaced += 1
                else:
                    load[host["host_id"]] += 1
                    suggested += 1
                continue
            await _end(session, dict(waiting), "withdrawn")
        if host is None:
            await session.execute(text(
                "INSERT INTO runner_placements (workspace_id, task_id, task_key, mode, status, "
                "reason, note) VALUES (:ws, :t, :key, :mode, 'unplaced', :reason, :note)"),
                {"ws": workspace_id, "t": task.id, "key": task.task_key,
                 "mode": "assign" if mode == "full" else "suggest", "reason": reason,
                 "note": _words(reason, task)})
            unplaced += 1
            continue
        load[host["host_id"]] += 1
        if mode != "full":
            await session.execute(text(
                "INSERT INTO runner_placements (workspace_id, task_id, task_key, host_id, "
                "agent_id, mode, status) VALUES (:ws, :t, :key, :host, :agent, 'suggest', "
                "'suggested')"),
                {"ws": workspace_id, "t": task.id, "key": task.task_key,
                 "host": host["host_id"], "agent": host["agent_id"]})
            suggested += 1
            continue
        await _offer_work(session, task, host, now)
        placed += 1
    steps = await _place_steps(session, workspace_id, online, load, mode, now) \
        if mode == "full" else {}
    rounds = await _supervise(session, workspace_id, hosts, online, now) \
        if mode == "full" else 0
    await session.commit()
    return {"mode": mode, "hosts": len(online), "placed": placed, "suggested": suggested,
            "unplaced": unplaced, "stopped": gave_up, **counts, **steps, "rounds": rounds}


async def run_all_locked() -> int | None:
    """One pass for every workspace, by one process at a time; None when another
    process is already scheduling."""
    async with async_session_factory() as lock:
        if not await lock.scalar(text(_LOCK_SQL)):
            return None
        try:
            return await run_all()
        finally:
            await lock.execute(text(_UNLOCK_SQL))
            await lock.commit()


async def scheduler_loop() -> None:
    """Scheduling has its own loop: a failing step elsewhere (the lease reaper
    runs a dozen) must never stop tasks from being placed."""
    import asyncio

    while True:
        try:
            await run_all_locked()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - background resilience
            logger.exception("scheduling pass failed")
        await asyncio.sleep(settings.lease_reaper_interval_seconds)


async def run_all() -> int:
    """One pass for every workspace with runner hosts."""
    async with async_session_factory() as session:
        workspaces = (await session.execute(text(
            "SELECT DISTINCT workspace_id FROM runner_hosts WHERE status <> 'revoked'"
        ))).scalars().all()
    placed = 0
    for workspace_id in workspaces:
        try:
            async with async_session_factory() as session:
                placed += (await run_pass(session, workspace_id)).get("placed", 0)
        except Exception:  # noqa: BLE001 - one workspace must not stop the others
            logger.warning("scheduling pass failed for %s", workspace_id, exc_info=True)
    return placed


# ------------------------------------------------------------------ steps
#
# After the work comes its review and, for diagram chunks, the merge. Each is
# placed like a task: on a reviewer computer that did not build it, moved when
# it fails or stalls. A step is done when its task or chunk moves past it.

REVIEW_STATES = ("awaiting_ci", "awaiting_review", "awaiting_gate")
MERGE_STATES = ("ready_to_merge", "verified")
REVIEWER_ROLES = {"AUDITOR"}
SUPERVISOR_ROLES = {"SUPER_ADMIN"}
REPORT_GRACE = timedelta(minutes=2)


async def _chunk_states(session: AsyncSession, workspace_id: Any) -> dict[str, dict[str, Any]]:
    """Chunk tasks of frozen diagrams: task key -> diagram, chunk, status, queue head."""
    from app.services.diagrams import _queue, status_of

    rows = (await session.execute(text(
        "SELECT c.*, d.diagram_key, d.id AS d_id FROM diagram_chunks c "
        "JOIN diagrams d ON d.id = c.diagram_id WHERE d.workspace_id = :ws "
        "AND d.status = 'frozen' AND c.task_key IS NOT NULL "
        "AND c.state NOT IN ('merged', 'retired', 'pending', 'blocked')"),
        {"ws": workspace_id})).mappings().all()
    if not rows:
        return {}
    tasks = {row["task_key"]: dict(row) for row in (await session.execute(text(
        "SELECT task_key, status, updated_at FROM collab_tasks WHERE workspace_id = :ws "
        "AND task_key = ANY(CAST(:keys AS text[]))"),
        {"ws": workspace_id, "keys": [row["task_key"] for row in rows]})).mappings().all()}
    heads: dict[int, str | None] = {}
    out = {}
    for row in rows:
        chunk = dict(row)
        task = tasks.get(chunk["task_key"])
        if task is not None:
            task = {**task, "status": getattr(task["status"], "value", task["status"])}
        if chunk["d_id"] not in heads:
            entries = await _queue(session, chunk["d_id"])
            heads[chunk["d_id"]] = entries[0]["chunk_key"] if entries else None
        out[chunk["task_key"]] = {
            "diagram_key": chunk["diagram_key"], "chunk_key": chunk["chunk_key"],
            "status": status_of(chunk, task),
            "head": heads[chunk["d_id"]] == chunk["chunk_key"],
        }
    return out


def _step_pending(step: str, task: CollabTask | None, chunk: dict[str, Any] | None,
                  chunk_task: bool = False) -> bool:
    if step == "review":
        if chunk is not None or chunk_task:
            return chunk is not None and chunk["status"] in REVIEW_STATES
        return task is not None and task.status == TaskStatus.REVIEWING
    if step == "merge":
        return chunk is not None and (chunk["status"] in MERGE_STATES
                                      or (chunk["status"] == "queued" and chunk["head"]))
    return False


async def _reconcile_step(session: AsyncSession, workspace_id: Any, placement: dict[str, Any],
                          host: dict[str, Any] | None, chunks: dict[str, dict[str, Any]],
                          now: datetime) -> str | None:
    step, status = placement["step"], placement["status"]
    if step == "supervise":
        if status == "unplaced":
            return None  # the round planner refreshes or places it
        timed_out = now - placement["created_at"] > timedelta(
            seconds=settings.runner_supervise_timeout_seconds)
        if placement["reported"] == "finished":
            await _end(session, placement, "delivered", "finished", placement["report_note"])
            return "delivered"
        if placement["reported"] == "failed":
            await _end(session, placement, "failed", "reported_failed", placement["report_note"])
            return "failed"
        problem = "host_removed" if host is None else _host_problem(host, now)
        if problem or timed_out:
            await _end(session, placement, "moved", problem or "timed_out")
            return "moved"
        return None
    task = await session.get(CollabTask, placement["task_id"]) if placement["task_id"] else None
    chunk = chunks.get(placement["task_key"]) if placement["task_key"] else None
    if not _step_pending(step, task, chunk, chunk_task=placement["chunk_key"] is not None):
        now_state = chunk["status"] if chunk else (task.status.value.lower() if task else "gone")
        if status == "unplaced":
            await _end(session, placement, "withdrawn", now_state)
            return "withdrawn"
        await _end(session, placement, "delivered", now_state)
        return "delivered"
    if status == "unplaced":
        return None
    problem = None
    if host is None:
        problem = "host_removed"
    elif placement["reported"] == "failed":
        problem = "reported_failed"
    elif placement["reported"] == "finished" and placement["reported_at"] \
            and now - placement["reported_at"] > REPORT_GRACE:
        problem = "no_progress"
    else:
        problem = _host_problem(host, now)
        if problem is None and now - placement["created_at"] > timedelta(
                seconds=settings.runner_step_timeout_seconds):
            problem = "timed_out"
    if problem is None:
        return None
    # A host that ran it and failed counts against itself; an outage does not.
    outcome = "failed" if problem in ("reported_failed", "no_progress", "timed_out") else "moved"
    await _end(session, placement, outcome, problem, placement["report_note"])
    await append_event(
        session, workspace_id=workspace_id, agent="scheduler", event_type="TASK_UNPLACED",
        payload={"task_key": placement["task_key"], "step": step,
                 "host_id": placement["host_id"], "placement_id": placement["placement_id"],
                 "reason": problem,
                 "targets": [f"agent:{host['agent_id']}"] if host and host["agent_id"] else []})
    return outcome


def _reviewer(host: dict[str, Any]) -> bool:
    caps = {item.lower() for item in host["capabilities"] or []}
    return host["role_scope"] in REVIEWER_ROLES or "review" in caps


def _supervisor(host: dict[str, Any]) -> bool:
    caps = {item.lower() for item in host["capabilities"] or []}
    return host["role_scope"] in SUPERVISOR_ROLES or "supervisor" in caps


async def _builders(session: AsyncSession, task: CollabTask) -> tuple[set[str], set[str]]:
    """Names and hosts that worked on the task: they never review or merge it."""
    names = {name for name in (task.claimed_by, task.lease_owner, task.handed_off_by,
                               task.orphaned_from) if name}
    rows = (await session.execute(text(
        "SELECT DISTINCT l.holder FROM leases l WHERE l.workspace_id = :ws "
        "AND l.resource_kind = 'task' AND l.resource_ref = :ref"),
        {"ws": task.workspace_id, "ref": task.canonical_id})).scalars().all()
    names.update(name for name in rows if name)
    hosts = set((await session.execute(text(
        "SELECT DISTINCT host_id FROM runner_placements WHERE task_id = :t AND step = 'work' "
        "AND host_id IS NOT NULL AND status IN ('claimed', 'delivered', 'failed')"),
        {"t": task.id})).scalars().all())
    return names, hosts


def _choose_reviewer(task: CollabTask, online: list[dict[str, Any]], names: set[str],
                     built: set[str], failures: dict[str, int],
                     load: dict[str, int]) -> tuple[dict[str, Any] | None, str]:
    pool = [host for host in online if _reviewer(host)
            and host["host_id"] not in built
            and host["agent_signature"] not in names and host["agent_id"] not in names]
    if not pool:
        return None, "no_reviewer_host"
    for check in ("no_host_for_namespace", "no_host_for_repo", "no_host_with_runtime"):
        narrowed = [host for host in pool if check not in _fits(task, host)]
        if not narrowed:
            return None, check
        pool = narrowed
    # One failed review or merge is enough to try another computer.
    pool = [host for host in pool if not failures.get(host["host_id"])]
    if not pool:
        return None, "every_host_failed"
    pool = [host for host in pool
            if (quota_used(host) or 0) < settings.runner_quota_exhausted_percent]
    if not pool:
        return None, "hosts_out_of_quota"
    pool = [host for host in pool if host["max_concurrency"] - load[host["host_id"]] > 0]
    if not pool:
        return None, "hosts_busy"
    return min(pool, key=lambda host: (-(host["max_concurrency"] - load[host["host_id"]]),
                                       quota_used(host) or 0.0, host["host_id"])), ""


def _step_note(placement_id: str, step: str, chunk: dict[str, Any] | None) -> str:
    note = f"placement {placement_id} step {step}"
    if chunk:
        note += f" diagram {chunk['diagram_key']} chunk {chunk['chunk_key']}"
    return note


async def _place_steps(session: AsyncSession, workspace_id: Any, online: list[dict[str, Any]],
                       load: dict[str, int], mode: str, now: datetime) -> dict[str, int]:
    """Place reviews of delivered work and merges of approved chunks."""
    chunks = await _chunk_states(session, workspace_id)
    wanted: dict[str, str] = {}
    for task_key, chunk in chunks.items():
        if chunk["status"] in REVIEW_STATES:
            wanted[task_key] = "review"
        elif chunk["status"] in MERGE_STATES or (chunk["status"] == "queued" and chunk["head"]):
            wanted[task_key] = "merge"
    # Plain tasks in review; a diagram chunk's review follows the chunk, not its task.
    reviewing = (await session.execute(text(
        "SELECT t.task_key FROM collab_tasks t WHERE t.workspace_id = :ws "
        "AND t.status = 'REVIEWING' AND (t.kind IS NULL OR t.kind = 'work') "
        "AND NOT EXISTS (SELECT 1 FROM diagram_chunks c JOIN diagrams d ON d.id = c.diagram_id "
        "WHERE d.workspace_id = t.workspace_id AND c.task_key = t.task_key) "
        "ORDER BY t.priority DESC, t.updated_at LIMIT 100"),
        {"ws": workspace_id})).scalars().all()
    for task_key in reviewing:
        wanted.setdefault(task_key, "review")
    placed = waiting = 0
    for task_key, step in wanted.items():
        task = (await session.execute(select(CollabTask).where(
            CollabTask.workspace_id == workspace_id,
            CollabTask.task_key == task_key))).scalar_one_or_none()
        if task is None:
            continue
        open_row = (await session.execute(text(
            "SELECT * FROM runner_placements WHERE task_id = :t AND status IN "
            "('suggested', 'offered', 'claimed', 'unplaced')"), {"t": task.id})
        ).mappings().one_or_none()
        if open_row is not None and open_row["status"] != "unplaced":
            continue
        names, built = await _builders(session, task)
        history = (await session.execute(text(
            "SELECT host_id, count(*) FROM runner_placements WHERE task_id = :t AND step = :s "
            "AND (status = 'failed' OR reason = 'supervisor_moved') GROUP BY host_id"),
            {"t": task.id, "s": step})).all()
        failures = {host_id: count for host_id, count in history if host_id}
        host, reason = _choose_reviewer(task, online, names, built, failures, load)
        chunk = chunks.get(task_key)
        if host is None:
            if open_row is not None and open_row["step"] == step and open_row["reason"] == reason:
                await session.execute(text(
                    "UPDATE runner_placements SET updated_at = NOW() WHERE placement_id = :id"),
                    {"id": open_row["placement_id"]})
            else:
                if open_row is not None:
                    await _end(session, dict(open_row), "withdrawn")
                await session.execute(text(
                    "INSERT INTO runner_placements (workspace_id, task_id, task_key, mode, status, "
                    "step, diagram_key, chunk_key, reason, note) VALUES (:ws, :t, :key, 'assign', "
                    "'unplaced', :step, :dk, :ck, :reason, :note)"),
                    {"ws": workspace_id, "t": task.id, "key": task.task_key, "step": step,
                     "dk": chunk and chunk["diagram_key"], "ck": chunk and chunk["chunk_key"],
                     "reason": reason, "note": _words(reason, task)})
            waiting += 1
            continue
        if open_row is not None:
            await _end(session, dict(open_row), "withdrawn")
        load[host["host_id"]] += 1
        await _offer_step(session, task, host, step, chunk)
        placed += 1
    return {"steps_placed": placed, "steps_waiting": waiting}


async def _offer_step(session: AsyncSession, task: CollabTask, host: dict[str, Any], step: str,
                      chunk: dict[str, Any] | None, actor: str = "scheduler") -> str:
    """Put a review or merge on a host and wake it; the task stays as it is."""
    placement_id = (await session.execute(text(
        "INSERT INTO runner_placements (workspace_id, task_id, task_key, host_id, agent_id, "
        "mode, status, step, diagram_key, chunk_key, lease_epoch) VALUES (:ws, :t, :key, "
        ":host, :agent, 'assign', 'offered', :step, :dk, :ck, :epoch) "
        "RETURNING placement_id"),
        {"ws": task.workspace_id, "t": task.id, "key": task.task_key, "host": host["host_id"],
         "agent": host["agent_id"], "step": step, "dk": chunk and chunk["diagram_key"],
         "ck": chunk and chunk["chunk_key"], "epoch": task.lease_epoch or 0})).scalar_one()
    await append_event(
        session, workspace_id=task.workspace_id, agent=actor, event_type="TASK_PLACED",
        payload={"task_key": task.task_key, "step": step, "host_id": host["host_id"],
                 "placement_id": placement_id, "idempotency_key": placement_id,
                 "targets": [f"agent:{host['agent_id']}"],
                 "details": {"delivery_kind": "task.placed", "priority": 1,
                             "note": _step_note(placement_id, step, chunk)}},
        namespace=task.namespace)
    return placement_id


# ------------------------------------------------------------------ supervisor


async def agenda(session: AsyncSession, workspace_id: Any,
                 since: datetime | None = None) -> list[dict[str, Any]]:
    """What the supervisor should look at: things that wait or went wrong."""
    items: list[dict[str, Any]] = []
    for row in (await session.execute(text(
            "SELECT rule, severity, subject_kind, subject_ref FROM situation_alerts "
            "WHERE workspace_id = :ws AND state = 'open' AND severity IN ('high', 'medium') "
            "ORDER BY severity, last_seen DESC LIMIT 15"), {"ws": workspace_id})).mappings():
        items.append({"kind": "alert", "rule": row["rule"], "severity": row["severity"],
                      "subject": f"{row['subject_kind']}:{row['subject_ref']}"})
    for row in (await session.execute(text(
            "SELECT task_key, step, reason, note FROM runner_placements WHERE workspace_id = :ws "
            "AND status = 'unplaced' AND step <> 'supervise' "
            "AND reason <> ALL(CAST(:quiet AS text[])) ORDER BY updated_at DESC LIMIT 15"),
            {"ws": workspace_id, "quiet": sorted(SELF_RESOLVING)})).mappings():
        items.append({"kind": "no_host", "task_key": row["task_key"], "step": row["step"],
                      "reason": row["reason"], "note": row["note"]})
    for row in (await session.execute(text(
            "SELECT t.task_key FROM collab_tasks t WHERE t.workspace_id = :ws "
            "AND t.status = 'HANDOFF' AND (SELECT count(*) FROM runner_placements p "
            "WHERE p.task_id = t.id AND p.step = 'work' AND p.status = 'failed') >= :rounds "
            "LIMIT 10"), {"ws": workspace_id, "rounds": MAX_ROUNDS})).mappings():
        items.append({"kind": "stopped", "task_key": row["task_key"],
                      "note": "failed on computers three times; needs a different approach"})
    for row in (await session.execute(text(
            "SELECT task_key FROM collab_tasks WHERE workspace_id = :ws AND status = 'HANDOFF' "
            "AND reviewed_by IS NOT NULL AND repo IS NOT NULL ORDER BY updated_at LIMIT 10"),
            {"ws": workspace_id})).mappings():
        items.append({"kind": "awaiting_deploy", "task_key": row["task_key"],
                      "note": "approved; waits for a verified deploy"})
    if since is not None:
        failed = (await session.execute(text(
            "SELECT task_key, step, reason FROM runner_placements WHERE workspace_id = :ws "
            "AND step <> 'supervise' AND status IN ('failed', 'moved') AND ended_at >= :since "
            "ORDER BY ended_at DESC LIMIT 15"), {"ws": workspace_id, "since": since})
        ).mappings().all()
        for row in failed:
            items.append({"kind": "step_failed", "task_key": row["task_key"],
                          "step": row["step"], "reason": row["reason"]})
    return items[:40]


async def _supervise(session: AsyncSession, workspace_id: Any, hosts: list[dict[str, Any]],
                     online: list[dict[str, Any]], now: datetime) -> int:
    """Wake a supervisor computer for a round when something waits, at most every so often."""
    open_round = (await session.execute(text(
        "SELECT * FROM runner_placements WHERE workspace_id = :ws AND step = 'supervise' "
        "AND status IN ('suggested', 'offered', 'claimed', 'unplaced')"),
        {"ws": workspace_id})).mappings().one_or_none()
    if open_round is not None and open_round["status"] != "unplaced":
        return 0
    last = (await session.execute(text(
        "SELECT created_at FROM runner_placements WHERE workspace_id = :ws AND step = 'supervise' "
        "AND status <> 'unplaced' ORDER BY created_at DESC LIMIT 1"),
        {"ws": workspace_id})).scalar_one_or_none()
    if last is not None and now - last < timedelta(seconds=settings.runner_supervise_every_seconds):
        return 0
    items = await agenda(session, workspace_id, since=last)
    if not items:
        if open_round is not None:
            await _end(session, dict(open_round), "withdrawn", "nothing_waits")
        return 0
    detail = json.dumps({"agenda": items})
    pool = [host for host in online if _supervisor(host)
            and (quota_used(host) or 0) < settings.runner_quota_exhausted_percent]
    if not pool:
        if open_round is not None:
            await session.execute(text(
                "UPDATE runner_placements SET detail = CAST(:d AS jsonb), updated_at = NOW() "
                "WHERE placement_id = :id"), {"d": detail, "id": open_round["placement_id"]})
        else:
            await session.execute(text(
                "INSERT INTO runner_placements (workspace_id, mode, status, step, reason, note, "
                "detail) VALUES (:ws, 'assign', 'unplaced', 'supervise', 'no_supervisor_host', "
                ":note, CAST(:d AS jsonb))"),
                {"ws": workspace_id, "note": UNPLACED["no_supervisor_host"], "d": detail})
        return 0
    if open_round is not None:
        await _end(session, dict(open_round), "withdrawn", "supervisor_online")
    host = min(pool, key=lambda item: (quota_used(item) or 0.0, item["host_id"]))
    placement_id = (await session.execute(text(
        "INSERT INTO runner_placements (workspace_id, host_id, agent_id, mode, status, step, "
        "detail) VALUES (:ws, :host, :agent, 'assign', 'offered', 'supervise', "
        "CAST(:d AS jsonb)) RETURNING placement_id"),
        {"ws": workspace_id, "host": host["host_id"], "agent": host["agent_id"],
         "d": detail})).scalar_one()
    await append_event(
        session, workspace_id=workspace_id, agent="scheduler", event_type="LOOP_ROUND",
        payload={"placement_id": placement_id, "host_id": host["host_id"], "items": len(items),
                 "idempotency_key": placement_id,
                 "targets": [f"agent:{host['agent_id']}"],
                 "details": {"delivery_kind": "task.placed", "priority": 1,
                             "note": _step_note(placement_id, "supervise", None)}})
    return 1


# ------------------------------------------------------------------ doctor


async def doctor(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    """What the Loop needs and what is missing, one check per line in words the
    owner can act on (WAKE_AND_DISPATCH S-9). It answers with no hosts at all,
    which is exactly when it is needed."""
    workspace_id = principal.workspace.id
    now = _now()
    mode = await automation.current_mode(session, workspace_id)
    hosts = [host for host in await _hosts(session, workspace_id) if host["status"] != "revoked"]
    online = _online(hosts, now)
    ready = [host for host in online
             if (quota_used(host) or 0) < settings.runner_quota_exhausted_percent]
    builders = [host for host in ready if host["role_scope"] == "EXECUTOR"]
    reviewers = [host for host in ready if _reviewer(host)]
    supervisors = [host for host in ready if _supervisor(host)]
    checks: list[dict[str, Any]] = []

    def check(check_id: str, ok: bool, required: bool, text: str, fix: str | None) -> None:
        checks.append({"id": check_id, "ok": ok, "required": required, "text": text,
                       "fix": None if ok else fix})

    check("automation", mode == "full", True,
          f"Automation is {mode}." + (" The Loop assigns work." if mode == "full"
                                     else " The Loop only suggests computers."),
          "Set automation to full in Governance so the Loop assigns work.")
    check("hosts", bool(online), True,
          f"{len(online)} of {len(hosts)} computer(s) online." if hosts
          else "No computer is connected.",
          "Run `npx veynod up --run-tasks` on a computer.")
    check("builder", bool(builders), True,
          f"{len(builders)} computer(s) can build (EXECUTOR) with quota left." if builders
          else "No online computer can build tasks.",
          "Connect a computer with `npx veynod up --run-tasks`.")
    check("reviewer", bool(reviewers), True,
          f"{len(reviewers)} computer(s) can review." if reviewers
          else "No online computer can review, so built work waits in review.",
          "Approve the `-review` identity when `veynod up --run-tasks` asks, or connect an "
          "AUDITOR computer.")
    check("supervisor", bool(supervisors), False,
          "A supervisor computer looks after the Loop." if supervisors
          else "No supervisor computer: stuck work is listed for you instead.",
          "Optional: `npx veynod up --supervisor` on one computer.")
    waiting = (await session.execute(select(CollabTask).where(
        CollabTask.workspace_id == workspace_id, CollabTask.status == TaskStatus.READY,
        CollabTask.assignee.is_(None), CollabTask.claimed_by.is_(None),
        (CollabTask.kind.is_(None)) | (CollabTask.kind == "work")).limit(500))).scalars().all()
    reach = {repo.lower() for host in builders for repo in host["repos"] or []}
    uncovered = sorted({task.repo for task in waiting
                        if task.repo and task.repo.lower() not in reach})
    check("repos", not uncovered, False,
          "Every waiting task's repository is on an online computer." if not uncovered
          else f"No online computer works on {', '.join(uncovered[:5])}.",
          "Add the repository with `npx veynod up --run-tasks --repo owner/name`.")
    needs_you = (await session.execute(text(
        "SELECT count(*) FROM runner_placements WHERE workspace_id = :ws AND status = 'unplaced' "
        "AND reason <> ALL(CAST(:quiet AS text[]))"),
        {"ws": workspace_id, "quiet": sorted(SELF_RESOLVING)})).scalar_one()
    check("needs_you", not needs_you, False,
          "Nothing waits for you on the Loop." if not needs_you
          else f"{needs_you} task(s) or step(s) have no computer; the Loop page says why.",
          "Open the Loop page (Needs you).")
    return {"ok": all(item["ok"] or not item["required"] for item in checks), "mode": mode,
            "ready_tasks": len(waiting), "checks": checks}


# ------------------------------------------------------------------ reading


def _placement_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "placement_id": row["placement_id"],
        "task_key": row["task_key"],
        "host_id": row["host_id"],
        "agent_id": row["agent_id"],
        "mode": row["mode"],
        "step": row.get("step", "work"),
        "diagram_key": row.get("diagram_key"),
        "chunk_key": row.get("chunk_key"),
        "status": row["status"],
        "reported": row.get("reported"),
        "reason": row["reason"],
        "note": row["note"],
        "needs_you": row["status"] == "unplaced" and row["reason"] not in SELF_RESOLVING,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
    }


async def placements(session: AsyncSession, principal: Principal, *,
                     task_key: str | None = None, open_only: bool = False,
                     step: str | None = None, limit: int = 100) -> dict[str, Any]:
    clauses = ["workspace_id = :ws"]
    params: dict[str, Any] = {"ws": principal.workspace.id, "limit": limit}
    if step:
        clauses.append("step = :step")
        params["step"] = step
    if task_key:
        clauses.append("task_key = :key")
        params["key"] = task_key
    if open_only:
        clauses.append("status IN ('suggested', 'offered', 'claimed', 'unplaced')")
    rows = (await session.execute(text(
        f"SELECT * FROM runner_placements WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at DESC LIMIT :limit"), params)).mappings().all()
    items = [_placement_out(dict(row)) for row in rows]
    return {"items": items, "needs_you": [item for item in items if item["needs_you"]]}


async def schedule_now(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    """Run a pass for this workspace now (people and commanders)."""
    if not (principal.is_human or principal.is_commander):
        raise Forbidden("people and commanders run the scheduler by hand")
    async with async_session_factory() as platform:
        return await run_pass(platform, principal.workspace.id)


async def _own_placement(session: AsyncSession, principal: Principal,
                         placement_id: str) -> dict[str, Any]:
    row = (await session.execute(text(
        "SELECT p.*, h.token_id FROM runner_placements p LEFT JOIN runner_hosts h "
        "ON h.host_id = p.host_id WHERE p.workspace_id = :ws AND p.placement_id = :id"),
        {"ws": principal.workspace.id, "id": placement_id})).mappings().one_or_none()
    if row is None:
        raise NotFound("placement")
    row = dict(row)
    owner = principal.is_human or principal.is_commander
    if not owner and row["token_id"] != principal.token_cid:
        raise Forbidden("a placement is read and reported by the computer it was placed on")
    return row


async def placement_detail(session: AsyncSession, principal: Principal,
                           placement_id: str) -> dict[str, Any]:
    """One placement with its detail (a supervise round carries its agenda)."""
    row = await _own_placement(session, principal, placement_id)
    return {**_placement_out(row), "detail": row.get("detail") or {},
            "report_note": row.get("report_note")}


async def report(session: AsyncSession, principal: Principal, placement_id: str,
                 outcome: str, note: str | None = None) -> dict[str, Any]:
    """The runner says how a step ended. A failure moves it to another computer at
    the next pass; a finish that changed nothing counts as a failure after 2 min."""
    if outcome not in ("finished", "failed"):
        raise ApiError(422, "validation_error", "outcome is finished or failed",
                       details={"field": "outcome"})
    row = await _own_placement(session, principal, placement_id)
    if row["status"] not in OPEN:
        return _placement_out(row)
    await session.execute(text(
        "UPDATE runner_placements SET reported = :o, reported_at = NOW(), report_note = :n, "
        "updated_at = NOW() WHERE placement_id = :id"),
        {"o": outcome, "n": (note or None) and note[:500], "id": placement_id})
    await session.commit()
    return _placement_out(dict((await session.execute(text(
        "SELECT * FROM runner_placements WHERE placement_id = :id"),
        {"id": placement_id})).mappings().one()))


# ------------------------------------------------------------------ steering


async def _may_steer(session: AsyncSession, principal: Principal) -> None:
    """People, commanders, super admins and supervisor computers steer the Loop."""
    if principal.is_human or principal.is_commander or principal.is_super_admin:
        return
    if principal.token_cid:
        hosts = await _hosts(session, principal.workspace.id)
        if any(host["token_id"] == principal.token_cid and host["status"] != "revoked"
               and _supervisor(host) for host in hosts):
            return
    raise Forbidden("people, super admins and supervisor computers steer the Loop")


async def loop_agenda(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    """What waits or went wrong right now, the list a supervise round is given."""
    await _may_steer(session, principal)
    async with async_session_factory() as platform:
        last = (await platform.execute(text(
            "SELECT created_at FROM runner_placements WHERE workspace_id = :ws "
            "AND step = 'supervise' AND status <> 'unplaced' ORDER BY created_at DESC LIMIT 1"),
            {"ws": principal.workspace.id})).scalar_one_or_none()
        items = await agenda(platform, principal.workspace.id, since=last)
    return {"items": items, "since": last.isoformat() if last else None}


async def _steerable(platform: AsyncSession, principal: Principal,
                     placement_id: str) -> dict[str, Any]:
    row = (await platform.execute(text(
        "SELECT * FROM runner_placements WHERE workspace_id = :ws AND placement_id = :id "
        "FOR UPDATE"), {"ws": principal.workspace.id, "id": placement_id})
    ).mappings().one_or_none()
    if row is None:
        raise NotFound("placement")
    return dict(row)


def _not_movable(placement: dict[str, Any], why: str) -> ApiError:
    return ApiError(409, "placement_not_movable", why,
                    details={"placement_id": placement["placement_id"],
                             "status": placement["status"], "step": placement["step"]})


async def move(session: AsyncSession, principal: Principal, placement_id: str,
               host_ref: str | None = None, note: str | None = None) -> dict[str, Any]:
    """Take a task or step off its computer and put it on another one: the named
    host, or the best other host. The old host is not picked again for it. A task
    the supervisor held comes back onto the computers the same way."""
    await _may_steer(session, principal)
    actor = principal.agent_signature or "supervisor"
    async with async_session_factory() as platform:
        placement = await _steerable(platform, principal, placement_id)
        held = placement["status"] == "withdrawn" and placement["reason"] == "held"
        if placement["step"] == "supervise":
            raise _not_movable(placement, "a supervise round is not moved; withdraw it")
        if placement["status"] not in OPEN and not held:
            raise _not_movable(placement, "this placement already ended")
        hosts = await _hosts(platform, principal.workspace.id)
        now = _now()
        target = None
        if host_ref:
            target = next((host for host in hosts if host_ref in (
                host["host_id"], host["agent_id"], f"host:{host['host_id']}")), None)
            if target is None:
                raise NotFound("host")
            problem = "host_offline" if target not in _online(hosts, now) \
                else _host_problem(target, now)
            if problem or (target["host_id"] == placement["host_id"] and not held):
                raise ApiError(409, "host_does_not_fit",
                               "that computer cannot take it now" if problem
                               else "it is already on that computer",
                               details={"host_id": target["host_id"],
                                        "reason": problem or "same_host"})
        task = await platform.get(CollabTask, placement["task_id"], with_for_update=True)
        if task is None:
            raise _not_movable(placement, "the task is gone")
        old = next((host for host in hosts if host["host_id"] == placement["host_id"]), None)
        result: dict[str, Any] = {"moved": placement_id, "from_host": placement["host_id"]}
        if placement["step"] == "work":
            if task.status not in (TaskStatus.READY, TaskStatus.IN_PROGRESS):
                raise _not_movable(placement, f"the task is {task.status.value}; nothing to move")
            if target is not None:
                misses = _fits(task, target)
                if misses:
                    raise ApiError(409, "host_does_not_fit", _words(misses[0], task),
                                   details={"host_id": target["host_id"], "reason": misses[0]})
            if old is not None:
                await _take_back(platform, task, old, "supervisor_moved")
            if not held:
                await _end(platform, placement, "moved", "supervisor_moved", note)
            if target is None:
                failures, _ = await _work_failures(platform, task)
                target, reason = _choose(task, _online(hosts, now), failures,
                                         await _load(platform, principal.workspace.id, hosts))
                result["reason"] = None if target else reason
            if target is not None:
                result["placement_id"] = await _offer_work(platform, task, target, now, actor)
        else:
            chunk = (await _chunk_states(platform, principal.workspace.id)).get(task.task_key)
            if target is not None:
                names, built = await _builders(platform, task)
                if not _reviewer(target) or target["host_id"] in built or {
                        target["agent_id"], target["agent_signature"]} & names:
                    raise ApiError(409, "host_does_not_fit",
                                   "a review or merge goes to a reviewer computer that did "
                                   "not build the work",
                                   details={"host_id": target["host_id"],
                                            "reason": "not_a_reviewer"})
            await _end(platform, placement, "moved", "supervisor_moved", note)
            if target is not None:
                result["placement_id"] = await _offer_step(platform, task, target,
                                                           placement["step"], chunk, actor)
            else:
                result["reason"] = "placed at the next pass"
        result["to_host"] = target["host_id"] if target else None
        await append_event(
            platform, workspace_id=principal.workspace.id, agent=actor,
            event_type="PLACEMENT_MOVED",
            payload={"task_key": task.task_key, "step": placement["step"],
                     "placement_id": placement_id, "from_host": placement["host_id"],
                     "to_host": result["to_host"], "note": note and note[:500]},
            namespace=task.namespace)
        await platform.commit()
    return result


async def withdraw(session: AsyncSession, principal: Principal, placement_id: str,
                   note: str | None = None) -> dict[str, Any]:
    """Take a task off the computers: its host lets go, the task is READY and
    unassigned, and the scheduler leaves it alone until someone claims it or the
    placement is moved. A supervise round just ends."""
    await _may_steer(session, principal)
    actor = principal.agent_signature or "supervisor"
    async with async_session_factory() as platform:
        placement = await _steerable(platform, principal, placement_id)
        if placement["status"] not in OPEN:
            raise _not_movable(placement, "this placement already ended")
        if placement["step"] in ("review", "merge"):
            raise _not_movable(placement, "a review or merge is not held; move it instead")
        task = None
        if placement["step"] == "work":
            task = await platform.get(CollabTask, placement["task_id"], with_for_update=True)
            if task is None or task.status not in (TaskStatus.READY, TaskStatus.IN_PROGRESS):
                raise _not_movable(placement, "the task is past the work step")
            host = next((item for item in await _hosts(platform, principal.workspace.id)
                         if item["host_id"] == placement["host_id"]), None)
            if host is not None:
                await _take_back(platform, task, host, "supervisor_withdrew")
            elif task.assignee:
                await _unassign(platform, task, actor)
            await _end(platform, placement, "withdrawn", "held", note)
            await platform.execute(text(
                "UPDATE runner_placements SET lease_epoch = :e WHERE placement_id = :id"),
                {"e": task.lease_epoch or 0, "id": placement_id})
        else:
            await _end(platform, placement, "withdrawn", "supervisor_withdrew", note)
        await append_event(
            platform, workspace_id=principal.workspace.id, agent=actor,
            event_type="PLACEMENT_WITHDRAWN",
            payload={"task_key": placement["task_key"], "step": placement["step"],
                     "placement_id": placement_id, "host_id": placement["host_id"],
                     "note": note and note[:500]},
            namespace=task.namespace if task else "default")
        await platform.commit()
    return {"withdrawn": placement_id, "step": placement["step"],
            "task_key": placement["task_key"], "held": placement["step"] == "work"}
