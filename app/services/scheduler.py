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

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.config import get_settings
from app.database import async_session_factory
from app.errors import Forbidden
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
    for row in rows:
        placement = dict(row)
        task = await session.get(CollabTask, placement["task_id"], with_for_update=True)
        host = hosts.get(placement["host_id"]) if placement["host_id"] else None
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
        if problem is None and status == "offered" and now - placement["created_at"] > timedelta(
                seconds=settings.runner_claim_timeout_seconds):
            problem = "not_claimed"
        if problem:
            await _take_back(session, task, host, problem)
            # Not taking it in time counts against the host; an outage does not.
            await _end(session, placement, "failed" if problem == "not_claimed" else "moved",
                       problem)
            counts["failed" if problem == "not_claimed" else "moved"] += 1
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


async def run_pass(session: AsyncSession, workspace_id: Any) -> dict[str, Any]:
    """One scheduling pass over a workspace. Commits."""
    hosts = await _hosts(session, workspace_id)
    if not any(host["status"] != "revoked" for host in hosts):
        return {"mode": None, "hosts": 0, "skipped": "no runner hosts"}
    mode = await automation.current_mode(session, workspace_id)
    now = _now()
    by_id = {host["host_id"]: host for host in hosts}
    counts = await _reconcile(session, workspace_id, by_id, now, mode)

    online = [host for host in hosts if host["status"] == "active"
              and now - host["last_seen_at"] <= ONLINE_WINDOW]
    load = {host["host_id"]: 0 for host in hosts}
    for host_id, active in (await session.execute(text(
            "SELECT host_id, count(*) FROM runner_placements WHERE workspace_id = :ws "
            "AND status IN ('offered', 'claimed') GROUP BY host_id"),
            {"ws": workspace_id})).all():
        if host_id in load:
            load[host_id] = active
    for host in hosts:
        load[host["host_id"]] = max(load[host["host_id"]], len(host["running"] or []))

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
        history = (await session.execute(text(
            "SELECT host_id, count(*) FROM runner_placements WHERE task_id = :t "
            "AND status = 'failed' GROUP BY host_id"), {"t": task.id})).all()
        failures = {host_id: count for host_id, count in history if host_id}
        if sum(failures.values()) >= MAX_ROUNDS and mode == "full":
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
                payload={"task_key": task.task_key, "rounds": sum(failures.values()),
                         "note": "failed on hosts three times; left in HANDOFF"},
                namespace=task.namespace)
            gave_up += 1
            continue
        host, reason = _choose(task, online, failures, load)
        waiting = (await session.execute(text(
            "SELECT * FROM runner_placements WHERE task_id = :t "
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
        placement_id = (await session.execute(text(
            "INSERT INTO runner_placements (workspace_id, task_id, task_key, host_id, agent_id, "
            "mode, status, lease_epoch) VALUES (:ws, :t, :key, :host, :agent, 'assign', "
            "'offered', :epoch) RETURNING placement_id"),
            {"ws": workspace_id, "t": task.id, "key": task.task_key, "host": host["host_id"],
             "agent": host["agent_id"], "epoch": task.lease_epoch or 0})).scalar_one()
        assignee = f"agent:{host['agent_id']}"
        before = task_snapshot(task)
        task.assignee = assignee
        task.version += 1
        task.updated_at = now
        await session.flush()
        await record_audit(session, workspace_id=workspace_id, session_batch_id=uuid.uuid4(),
                           agent_signature="scheduler", target_table="collab_tasks",
                           target_record_id=task.id, operation="UPDATE", before=before,
                           after=task_snapshot(task))
        await append_event(
            session, workspace_id=workspace_id, agent="scheduler", event_type="TASK_PLACED",
            payload={"task_key": task.task_key, "host_id": host["host_id"],
                     "placement_id": placement_id, "idempotency_key": placement_id,
                     "targets": [assignee],
                     "details": {"delivery_kind": "task.placed", "priority": 1,
                                 "note": f"placement {placement_id}"}},
            namespace=task.namespace)
        placed += 1
    await session.commit()
    return {"mode": mode, "hosts": len(online), "placed": placed, "suggested": suggested,
            "unplaced": unplaced, "stopped": gave_up, **counts}


async def run_all() -> int:
    """Reaper hook: one pass for every workspace with runner hosts."""
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


# ------------------------------------------------------------------ reading


def _placement_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "placement_id": row["placement_id"],
        "task_key": row["task_key"],
        "host_id": row["host_id"],
        "agent_id": row["agent_id"],
        "mode": row["mode"],
        "status": row["status"],
        "reason": row["reason"],
        "note": row["note"],
        "needs_you": row["status"] == "unplaced" and row["reason"] not in SELF_RESOLVING,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
    }


async def placements(session: AsyncSession, principal: Principal, *,
                     task_key: str | None = None, open_only: bool = False,
                     limit: int = 100) -> dict[str, Any]:
    clauses = ["workspace_id = :ws"]
    params: dict[str, Any] = {"ws": principal.workspace.id, "limit": limit}
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
