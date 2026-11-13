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

A placement has three stages, each with its own record, and none stands in for
the next: the offer was *delivered* (the computer read the poke, its worker
process came up), the work *started* (the worker asked ``start`` and the
placement was still open) and it *finished* (the runner's report). A supervise
round counts as done only when its report says finished; a worker that arrives
after its placement moved on is ``skipped``, with the reason, never a success.
"""

from __future__ import annotations

import json
import logging
import math
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
from app.services.deploys import is_deploy_host
from app.services.runners import MIN_RUNNER_VERSION, ONLINE_WINDOW, runner_outdated

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
# One pass per workspace at a time, across processes; released at commit.
_WORKSPACE_LOCK_SQL = (
    "SELECT pg_advisory_xact_lock(hashtext('veynod_scheduler:' || CAST(:ws AS text))::bigint)"
)
# Reasons a task has no host, most specific last, in words for the owner.
UNPLACED = {
    "no_host_online": "No runner is online.",
    "no_host_for_role": "No online runner can take {role} tasks.",
    "no_host_for_namespace": "No online runner may work in the {namespace} namespace.",
    "no_host_with_capabilities": "No online runner has {capabilities}.",
    "no_host_for_repo": "No online runner can reach {repo}.",
    "no_kernel_binding": ("{repo} is not connected to a Kernel, so no computer can build it: "
                          "bind it under the project's repositories."),
    "no_host_with_runtime": "No online runner has Claude Code or Codex ready.",
    "every_host_failed": "Every runner that could take it already failed it twice.",
    "hosts_out_of_quota": "Every runner that could take it is out of quota.",
    "hosts_busy": "Every runner that could take it is busy; it waits for a free slot.",
    "no_reviewer_host": ("No other computer can review it (the one that built it may not), "
                         "so it waits for your review."),
    "no_supervisor_host": "No supervisor computer is online to look after the Loop.",
}
# Reasons that wait on their own; the rest need the owner.
SELF_RESOLVING = {"hosts_busy"}
# Placements a computer may still start working on.
STARTABLE = ("offered", "claimed")
# How a supervise round ended, as a timeline fact. LOOP_ROUND itself only says a
# round was offered to a computer; these say what became of it.
ROUND_OUTCOMES = {"delivered": "LOOP_ROUND_FINISHED", "failed": "LOOP_ROUND_FAILED",
                  "moved": "LOOP_ROUND_MOVED"}


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
    was_open = placement.get("status") in OPEN
    placement["status"] = status
    outcome = ROUND_OUTCOMES.get(status) if placement.get("step") == "supervise" else None
    if outcome and was_open and placement.get("host_id") and placement.get("workspace_id"):
        # A round that was offered to a computer says how it ended: finished only
        # on the runner's own report, otherwise failed or moved with the reason.
        await append_event(
            session, workspace_id=placement["workspace_id"], agent="scheduler",
            event_type=outcome,
            payload={"placement_id": placement["placement_id"], "host_id": placement["host_id"],
                     "reason": reason, "note": note and note[:500],
                     "started": placement.get("started_at") is not None,
                     "idempotency_key": f"{placement['placement_id']}:{status}"})


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
        # Free the Kernel worktree too: an orphaned host must not keep a write-set
        # lock that blocks the replacement placement.
        try:
            from app.services import kernel_delivery

            await kernel_delivery.release_task_lease(
                session, task, reason=f"scheduler: {reason}"[:2000]
            )
        except Exception:  # noqa: BLE001 - placement still proceeds
            logger.exception("could not release the Kernel lease for %s", task.task_key)
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
        if status == "offered":
            # The nerve dispatch moves the lease epoch before the computer has
            # answered, so the epoch alone is not a claim. Its delivery record
            # says what happened, whichever process is delivering it.
            delivery = (await session.execute(text(
                "SELECT t.dispatch_state, d.qos_status, d.detail FROM collab_tasks t "
                "LEFT JOIN poke_deliveries d ON d.workspace_id = t.workspace_id "
                "AND d.frame_id = :f WHERE t.id = :t"),
                {"t": task.id, "f": placement["placement_id"]})).mappings().one()
            if delivery["dispatch_state"] in ("AWAITING_NET_ACK", "AWAITING_EXEC_ACK"):
                continue  # receipts pending; the dispatcher (or reaper) settles it
            if delivery["qos_status"] == "TIMEOUT_REVOKED":
                if assigned_here:
                    await _unassign(session, task, "scheduler")
                await _end(session, placement, "moved",
                           f"nerve:{delivery['detail'] or 'not_delivered'}"[:120])
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
        if problem is None and status == "claimed" and placement.get("reported") == "failed":
            # The computer took it and then said the run failed (the agent would not
            # start, the worker died). The task is still IN_PROGRESS under that
            # computer's lease: waiting for the lease to run out left it stuck for a
            # quarter of an hour with nobody working on it. Release it now.
            problem = "reported_failed"
        if problem is None and status == "offered" and now - placement["created_at"] > timedelta(
                seconds=settings.runner_claim_timeout_seconds):
            problem = "not_claimed"
        if problem:
            await _take_back(session, task, host, problem)
            # Not taking it, or failing to start it, counts against the host; an
            # outage or a busy host does not.
            counted = problem in COUNTED_AGAINST_HOST
            await _end(session, placement, "failed" if counted else "moved", problem,
                       placement.get("report_note") if problem == "reported_failed" else None)
            counts["failed" if counted else "moved"] += 1
            await append_event(
                session, workspace_id=workspace_id, agent="scheduler",
                event_type="TASK_UNPLACED",
                payload={"task_key": task.task_key, "host_id": host["host_id"],
                         "placement_id": placement["placement_id"], "reason": problem,
                         "targets": [f"agent:{host['agent_id']}"]},
                namespace=task.namespace)
    return counts


# What a computer with a coding agent can do without having said so. Tasks
# name capabilities such as "frontend"; computers almost never declare any, so
# such tasks had no computer although any coding agent could take them.
CODING_AGENT_CAPABILITIES = frozenset({
    "software-engineering", "coding", "code", "backend", "frontend", "fullstack", "tests",
    "testing", "docs", "documentation", "research", "writing", "analysis"})


def _capabilities(host: dict[str, Any]) -> set[str]:
    """Declared capabilities, runtime ids and, on a computer that runs tasks at
    all (any ready runtime: a coding agent or the command its owner chose), the
    general ones. A special capability (``gpu``, ``ios-signing``) must still be
    declared."""
    have = {item.lower() for item in host["capabilities"] or []} | {
        runtime["id"] for runtime in host["runtimes"] or []}
    if any(runtime.get("available") is not False for runtime in host["runtimes"] or []):
        have |= CODING_AGENT_CAPABILITIES
    return have


def same_repo(wanted: str, offered: str) -> bool:
    """Whether a task's repository is the one a computer serves.

    Exact, ignoring case, a trailing ``.git`` and a host prefix. A bare name
    (``smoke``) matches ``owner/smoke``: tasks written by hand often leave the
    owner out, and such a task waited forever as "no computer reaches it".
    """
    def clean(value: str) -> str:
        value = value.strip().lower().removesuffix(".git").rstrip("/")
        for prefix in ("https://github.com/", "http://github.com/", "git@github.com:",
                       "github.com/"):
            value = value.removeprefix(prefix)
        return value

    left, right = clean(wanted), clean(offered)
    if not left or not right:
        return False
    if left == right:
        return True
    return "/" not in left and right.rsplit("/", 1)[-1] == left


def _stands_in(host_role: str | None, wanted: str, task: CollabTask) -> bool:
    """One worker role: an EXECUTOR and an ANALYST computer take each other's tasks.

    Only when the task names its role as ``assigned_role``: ``required_role`` is
    a hard requirement its author set on purpose and is kept.
    """
    from app.auth import WORKER_ROLES

    return (settings.worker_roles_unified and wanted in WORKER_ROLES
            and host_role in WORKER_ROLES and not task.required_role)


def _fits(task: CollabTask, host: dict[str, Any]) -> list[str]:
    """The filters a host fails for a task, in UNPLACED order."""
    misses = []
    role = task.required_role or task.assigned_role
    if role and host["role_scope"] != role and not _stands_in(host["role_scope"], role, task):
        misses.append("no_host_for_role")
    namespaces = list(host["allowed_namespaces"] or [])
    if "*" not in namespaces and task.namespace not in namespaces:
        misses.append("no_host_for_namespace")
    needed = {item.lower() for item in (task.required_capabilities or [])}
    if needed - _capabilities(host):
        misses.append("no_host_with_capabilities")
    if task.repo and not any(same_repo(task.repo, repo) for repo in host["repos"] or []):
        misses.append("no_host_for_repo")
    if quota_used(host) is None:
        misses.append("no_host_with_runtime")
    return misses


async def _repo_bound(session: AsyncSession, task: CollabTask,
                      cache: dict[tuple[str | None, str], bool]) -> bool:
    """Whether the task's repository is bound to a Kernel (once per pass)."""
    key = (task.project, task.repo)
    if key not in cache:
        from app.services.task_lifecycle import kernel_binding_for

        cache[key] = await kernel_binding_for(
            session, task.workspace_id, task.project, task.repo) is not None
    return cache[key]


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
    """Hosts that can take work now: active, heard from, and able to receive it
    (a CLI older than nerve-only dispatch would be offered work it never gets)."""
    return [host for host in hosts if host["status"] == "active"
            and now - host["last_seen_at"] <= ONLINE_WINDOW
            and not runner_outdated(host.get("runner_version"))
            and not is_deploy_host(host)]


async def _load(session: AsyncSession, workspace_id: Any,
                hosts: list[dict[str, Any]]) -> dict[str, int]:
    """Busy slots per host: open offers and claims, or what the host says it runs.

    A supervise round is not counted: it runs in a lane of its own, so looking
    after the Loop never takes the slot a task needs (and a long task never
    stops the rounds). A computer whose CLI has no such lane still lists the
    round among what it runs, which the second loop below counts.
    """
    load = {host["host_id"]: 0 for host in hosts}
    for host_id, active in (await session.execute(text(
            "SELECT host_id, count(*) FROM runner_placements WHERE workspace_id = :ws "
            "AND status IN ('offered', 'claimed') AND step <> 'supervise' GROUP BY host_id"),
            {"ws": workspace_id})).all():
        if host_id in load:
            load[host_id] = active
    for host in hosts:
        load[host["host_id"]] = max(load[host["host_id"]], len(host["running"] or []))
    return load


async def _work_failures(session: AsyncSession, task: CollabTask) -> tuple[dict[str, int], int]:
    """Hosts to avoid for this task (failed twice, or moved off by the supervisor)
    and the failed rounds so far. A supervisor's move does not count as a round."""
    # Only what failed lately counts (runner_failure_window_hours): an old
    # failure may have had a cause that is gone, and counting it forever kept a
    # task stopped for good.
    rows = (await session.execute(text(
        "SELECT host_id, status, count(*) FROM runner_placements WHERE task_id = :t "
        "AND step = 'work' AND host_id IS NOT NULL AND (status = 'failed' "
        "OR (status = 'moved' AND reason = 'supervisor_moved')) "
        "AND COALESCE(ended_at, created_at) > NOW() - make_interval(hours => :w) "
        "GROUP BY host_id, status"),
        {"t": task.id, "w": settings.runner_failure_window_hours})).all()
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
        # A timeline fact. The computer hears about it over its nerve socket
        # (dispatch_offered), never through an agent inbox.
        payload={"task_key": task.task_key, "host_id": host["host_id"],
                 "placement_id": placement_id, "idempotency_key": placement_id},
        namespace=task.namespace)
    return placement_id


#: Placements a dispatch in this process is delivering right now. The sockets
#: live in this process, so this is the whole set: the scheduling pass and the
#: dispatch loop both call dispatch_offered, and without the claim the loser
#: read "already dispatching" as a failed delivery and moved a placement the
#: winner was still delivering.
_DISPATCHING: set[str] = set()
#: Results that mean another dispatch owns the delivery, not that it failed.
_NOT_MINE = {"TASK_ALREADY_DISPATCHING"}


async def dispatch_offered(workspace_id: Any | None = None, limit: int = 50) -> dict[str, int]:
    """Deliver every offered placement over its computer's nerve socket.

    An offer is a row (status ``offered``), whoever made it: a scheduling pass,
    a person accepting a suggestion, a supervisor's move, a supervise round.
    This is the one path that tells a computer. Each placement is delivered
    once (its ``poke_deliveries`` row is the record); a delivery that fails is
    taken back and the placement moved, so the next pass places it again. A
    computer whose socket is not connected right now is retried on the next
    call; the claim timeout still covers one that never comes back.
    """
    from app.services import nerve_hub

    counts = {"ok": 0, "failed": 0, "skipped": 0}
    async with async_session_factory() as session:
        rows = (await session.execute(text(
            "SELECT p.*, t.title AS task_title, k.agent_signature AS holder "
            "FROM runner_placements p "
            "LEFT JOIN collab_tasks t ON t.id = p.task_id "
            "LEFT JOIN runner_hosts h ON h.host_id = p.host_id "
            "LEFT JOIN agent_tokens k ON k.canonical_id = h.token_id "
            "AND k.workspace_id = h.workspace_id "
            "WHERE p.status = 'offered' AND p.agent_id IS NOT NULL "
            "AND (CAST(:ws AS uuid) IS NULL OR p.workspace_id = CAST(:ws AS uuid)) "
            "AND NOT EXISTS (SELECT 1 FROM poke_deliveries d WHERE d.workspace_id = p.workspace_id "
            "AND d.frame_id = p.placement_id) ORDER BY p.created_at LIMIT :limit"),
            {"ws": str(workspace_id) if workspace_id else None, "limit": limit})).mappings().all()
        for row in rows:
            placement = dict(row)
            sock = nerve_hub.nerve_hub.get(placement["workspace_id"], placement["agent_id"])
            # Not connected right now, or finishing its work before it restarts on
            # a new version: the offer waits for the next call.
            if placement["placement_id"] in _DISPATCHING or sock is None or sock.draining:
                counts["skipped"] += 1
                continue
            _DISPATCHING.add(placement["placement_id"])
            try:
                await _deliver_one(session, placement, nerve_hub, counts)
            finally:
                _DISPATCHING.discard(placement["placement_id"])
    return counts


async def _deliver_one(session: AsyncSession, placement: dict[str, Any], nerve_hub: Any,
                       counts: dict[str, int]) -> None:
    """One offered placement over its computer's socket; a real failure moves it."""
    chunk = ({"diagram_key": placement["diagram_key"], "chunk_key": placement["chunk_key"]}
             if placement["diagram_key"] else {})
    if placement["step"] == "work":
        result = await nerve_hub.deliver_task_poke(
            session, workspace_id=placement["workspace_id"],
            task_key=placement["task_key"], placement_id=placement["placement_id"],
            epoch=int(placement["lease_epoch"] or 0) + 1,
            title=placement["task_title"] or placement["task_key"],
            payload={"agent_id": placement["agent_id"],
                     "holder": placement["holder"] or placement["agent_id"], **chunk})
    else:
        result = await nerve_hub.deliver_step_poke(
            session, workspace_id=placement["workspace_id"],
            placement_id=placement["placement_id"], agent_id=placement["agent_id"],
            step=placement["step"], task_key=placement["task_key"],
            title=placement["task_title"] or placement["step"], extra=chunk)
    if result.get("dispatched"):
        counts["ok"] += 1
        return
    reason = str(result.get("reason") or "nerve")
    if reason in _NOT_MINE:
        counts["skipped"] += 1
        return
    counts["failed"] += 1
    logger.info("nerve delivery of %s (%s) did not land: %s",
                placement["placement_id"], placement["step"], reason)
    await _undeliverable(session, placement, f"nerve:{reason}"[:120])


async def _undeliverable(session: AsyncSession, placement: dict[str, Any], reason: str) -> None:
    """The computer did not take the poke: free the task and move the placement."""
    current = (await session.execute(text(
        "SELECT status FROM runner_placements WHERE placement_id = :id FOR UPDATE"),
        {"id": placement["placement_id"]})).scalar_one_or_none()
    if current != "offered":
        await session.rollback()
        return
    if placement["step"] == "work" and placement["task_id"]:
        task = await session.get(CollabTask, placement["task_id"], with_for_update=True)
        if task is not None and task.assignee == f"agent:{placement['agent_id']}":
            await _unassign(session, task, "scheduler")
    await _end(session, placement, "moved", reason)
    await session.commit()


async def run_pass(session: AsyncSession, workspace_id: Any) -> dict[str, Any]:
    """One scheduling pass over a workspace. Commits.

    Passes on one workspace take turns: a pass by hand (``schedule_now``) does
    not hold the background lock, and two passes acting on the same open
    placements would end them from stale reads. The lock lasts until the
    pass commits, before delivery.
    """
    await session.execute(text(_WORKSPACE_LOCK_SQL), {"ws": str(workspace_id)})
    hosts = await _hosts(session, workspace_id)
    if not any(host["status"] != "revoked" for host in hosts):
        return {"mode": None, "hosts": 0, "skipped": "no runner hosts"}
    mode = await automation.current_mode(session, workspace_id)
    now = _now()
    by_id = {host["host_id"]: host for host in hosts}
    counts = await _reconcile(session, workspace_id, by_id, now, mode)

    online = _online(hosts, now)
    load = await _load(session, workspace_id, hosts)
    resumed = await _resume_stopped(session, workspace_id, now) if mode == "full" else 0

    # ADR-017: a task with an active Agent Run is executed by the Runtime
    # through the Kernel. It must never also be offered to a runner host.
    tasks = (await session.execute(
        select(CollabTask).where(
            CollabTask.workspace_id == workspace_id,
            CollabTask.status == TaskStatus.READY,
            CollabTask.assignee.is_(None),
            CollabTask.claimed_by.is_(None),
            CollabTask.active_run_id.is_(None),
            (CollabTask.kind.is_(None)) | (CollabTask.kind == "work"),
        ).order_by(CollabTask.priority.desc(), CollabTask.created_at)
        .limit(100).with_for_update(skip_locked=True))).scalars().all()
    placed = suggested = unplaced = gave_up = 0
    bound: dict[tuple[str | None, str], bool] = {}
    workspace_mode = mode
    for task in tasks:
        if await _held(session, task):
            continue
        # A chunk task follows its diagram's own mode when the diagram sets one.
        mode = await automation.mode_for_task(session, workspace_id, task.task_key)
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
        if task.repo and not await _repo_bound(session, task, bound):
            # Code runs only through the Kernel: no computer can take it until
            # the project's repository is bound, so do not place it to fail.
            host, reason = None, "no_kernel_binding"
        else:
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
                "reason, note) VALUES (:ws, :t, :key, :mode, 'unplaced', :reason, :note) "
                "ON CONFLICT (task_id, step) WHERE status IN "
                "('suggested', 'offered', 'claimed', 'unplaced') DO UPDATE SET "
                "status = 'unplaced', host_id = NULL, agent_id = NULL, "
                "reason = EXCLUDED.reason, note = EXCLUDED.note, updated_at = NOW()"),
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
    steps = await _place_steps(session, workspace_id, online, load, workspace_mode, now) \
        if workspace_mode == "full" else {}
    rounds = await _supervise(session, workspace_id, hosts, online, now) \
        if workspace_mode == "full" else 0
    await session.commit()
    # Code tasks the Kernel merged are finished on trust (their own transaction:
    # reading the Kernel's evidence commits).
    merged = 0
    if workspace_mode == "full":
        from app.services.tasks import finish_merged_on_trust

        merged = await finish_merged_on_trust(workspace_id)
    # Delivery is a separate step: the pass must not hold its transaction open
    # across a receipt wait per placement.
    delivered = await dispatch_offered(workspace_id)
    return {"mode": workspace_mode, "hosts": len(online), "placed": placed,
            "suggested": suggested, "nerve": delivered, "resumed": resumed, "merged": merged,
            "unplaced": unplaced, "stopped": gave_up, **counts, **steps, "rounds": rounds}


async def _resume_stopped(session: AsyncSession, workspace_id: Any, now: datetime) -> int:
    """Put tasks the scheduler stopped back on the board once their failures are old.

    A task goes to HANDOFF after ``MAX_ROUNDS`` failed rounds. That used to be
    the end: the scheduler only places READY tasks, and nothing moved a stopped
    one back. A stopped task (HANDOFF with no hand-off by anyone, nobody
    holding it, and at least ``MAX_ROUNDS`` failed work placements in its
    history) returns to READY when fewer than ``MAX_ROUNDS`` of them fall
    inside the failure window, so it is tried again about once per window
    instead of never. A task an agent or a person handed off is not touched.
    """
    rows = (await session.execute(text(
        "SELECT t.id FROM collab_tasks t WHERE t.workspace_id = :ws AND t.status = 'HANDOFF' "
        "AND t.handed_off_by IS NULL AND t.claimed_by IS NULL AND t.assignee IS NULL "
        "AND t.active_run_id IS NULL AND (t.kind IS NULL OR t.kind = 'work') "
        "AND (SELECT count(*) FROM runner_placements p WHERE p.task_id = t.id "
        "     AND p.step = 'work' AND p.status = 'failed') >= :rounds "
        "AND (SELECT count(*) FROM runner_placements p WHERE p.task_id = t.id "
        "     AND p.step = 'work' AND p.status = 'failed' "
        "     AND COALESCE(p.ended_at, p.created_at) > NOW() - make_interval(hours => :w)) "
        "    < :rounds LIMIT 50"),
        {"ws": workspace_id, "rounds": MAX_ROUNDS,
         "w": settings.runner_failure_window_hours})).scalars().all()
    resumed = 0
    for task_id in rows:
        task = await session.get(CollabTask, task_id, with_for_update=True)
        if task is None or task.status != TaskStatus.HANDOFF:
            continue
        before = task_snapshot(task)
        task.status = TaskStatus.READY
        task.version += 1
        task.updated_at = now
        await session.flush()
        await record_audit(session, workspace_id=workspace_id, session_batch_id=uuid.uuid4(),
                           agent_signature="scheduler", target_table="collab_tasks",
                           target_record_id=task.id, operation="UPDATE", before=before,
                           after=task_snapshot(task))
        await append_event(
            session, workspace_id=workspace_id, agent="scheduler",
            event_type="TASK_SCHEDULER_RESUMED",
            payload={"task_key": task.task_key,
                     "note": "its failed rounds are older than the failure window; trying again"},
            namespace=task.namespace)
        resumed += 1
    return resumed


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


async def nerve_dispatch_loop(interval_seconds: float = 1.0) -> None:
    """Deliver offers made outside a scheduling pass (a person accepting a
    suggestion, a supervisor's move) and offers whose computer reconnected."""
    import asyncio

    while True:
        try:
            await dispatch_offered()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - background resilience
            logger.exception("nerve dispatch pass failed")
        await asyncio.sleep(interval_seconds)


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
    if step == "deploy":
        return None  # app.services.deploys owns a deploy run from offer to report
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
        from app.services.kernel_delivery import kernel_owns_steps

        # Gate + merge coordinator own review and merge. Do not wake a second
        # computer (the permanent -review seat) for a Kernel delivery.
        if kernel_owns_steps(task):
            continue
        open_row = (await session.execute(text(
            "SELECT * FROM runner_placements WHERE task_id = :t AND status IN "
            "('suggested', 'offered', 'claimed', 'unplaced') "
            "ORDER BY updated_at DESC, placement_id LIMIT 1"), {"t": task.id})
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
                    "'unplaced', :step, :dk, :ck, :reason, :note) "
                    "ON CONFLICT (task_id, step) WHERE status IN "
                    "('suggested', 'offered', 'claimed', 'unplaced') DO UPDATE SET "
                    "status = 'unplaced', host_id = NULL, agent_id = NULL, "
                    "reason = EXCLUDED.reason, note = EXCLUDED.note, updated_at = NOW()"),
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
                 "placement_id": placement_id, "idempotency_key": placement_id},
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


def _agenda_fingerprint(items: list[dict[str, Any]]) -> str:
    """What a round is about, without when: the same open problems give the same value."""
    import hashlib

    keys = sorted(json.dumps({k: v for k, v in item.items() if k not in ("note",)},
                             sort_keys=True, default=str) for item in items)
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()[:16]


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
    seen = _agenda_fingerprint(items)
    finished = (await session.execute(text(
        "SELECT detail->>'fingerprint' AS fingerprint, created_at FROM runner_placements "
        "WHERE workspace_id = :ws AND step = 'supervise' AND reported = 'finished' "
        "ORDER BY created_at DESC LIMIT 1"), {"ws": workspace_id})).mappings().one_or_none()
    if (finished is not None and finished["fingerprint"] == seen
            and now - finished["created_at"] < timedelta(
                seconds=settings.runner_supervise_repeat_seconds)):
        # The last round saw exactly this and finished: nothing new to look at.
        return 0
    detail = json.dumps({"agenda": items, "fingerprint": seen})
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
                 "idempotency_key": placement_id})
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
    minimum = ".".join(str(part) for part in MIN_RUNNER_VERSION)
    outdated_hosts = sorted((host for host in hosts if runner_outdated(host.get("runner_version"))),
                            key=lambda host: host["agent_id"] or host["host_id"])
    outdated = [f"{host['agent_id'] or host['host_id']} ({host['runner_version']})"
                for host in outdated_hosts]
    upgrade = (f"On each of them run `npx veynod@latest` (it keeps the computer's settings), "
               f"then check that `npx veynod --version` prints {minimum} or newer.")
    # Connected but too old: it heartbeats, so "run veynod on a computer" would
    # send the owner in a circle. Say what is wrong and how to upgrade instead.
    stale = [host for host in outdated_hosts if host["status"] == "active"
             and now - host["last_seen_at"] <= ONLINE_WINDOW and not is_deploy_host(host)]
    if online or not hosts:
        hosts_text = (f"{len(online)} of {len(hosts)} computer(s) online." if hosts
                      else "No computer is connected.")
    else:
        hosts_text = (f"0 of {len(hosts)} computer(s) can take work."
                      + (f" {len(stale)} connected, but on a veynod older than {minimum}."
                         if stale else ""))
    check("hosts", bool(online), True, hosts_text,
          upgrade if stale else "Run `npx veynod --run-tasks` on a computer.")
    check("cli_version", not outdated, False,
          f"Every computer runs veynod {minimum} or newer." if not outdated
          else (f"{', '.join(outdated)} run(s) a veynod older than {minimum}: work reaches "
                "a computer only over its nerve socket now, so it gets none."),
          upgrade)
    stale_builders = [host for host in stale if host["role_scope"] == "EXECUTOR"]
    check("builder", bool(builders), True,
          f"{len(builders)} computer(s) can build (EXECUTOR) with quota left." if builders
          else "No online computer can build tasks.",
          upgrade if stale_builders and not builders
          else "Connect a computer with `npx veynod --run-tasks`.")
    can_review = (f"{len(reviewers)} computer(s) can review work without a repository. "
                  if reviewers else "")
    trusting = (mode == "full" and settings.trust_by_default)
    check("reviewer", True, False,
          can_review + ("Work without a repository is finished on trust; the Kernel gate "
                        "reviews code; flagged work waits for a look, not for approval."
                        if trusting else
                        "The Kernel gate reviews code; a person reviews what no other "
                        "computer can."),
          None)
    unpinned = sorted(host["agent_id"] or host["host_id"] for host in online
                      if (host.get("capabilities_report") or {}).get("mcp_pinning") is False)
    check("pinning", not unpinned, False,
          "Every online computer starts its sessions with its own key." if not unpinned
          else f"{', '.join(unpinned[:5])} start(s) sessions without their own key, so "
               "claims there are refused (not_assignee).",
          # Before 0.7.0 Codex pinned only through OAR (Node 24 and a service copy
          # that could load it); from 0.7.0 Claude Code and Codex pin through their
          # own CLIs, on the CLI's own floor (Node 22).
          "On each of them run `npx veynod@latest` (0.7.0 or newer): Claude Code and Codex then "
          "start every session with the computer's own key (Node 22 or newer).")
    check("supervisor", bool(supervisors), False,
          "A supervisor computer is online." if supervisors
          else "No supervisor computer: stuck work is listed for you instead.",
          "Optional: `npx veynod --supervisor` on one computer.")
    # Being online is not looking after the Loop: judge by how rounds ended.
    recent = (await session.execute(text(
        "SELECT status, reported, reason FROM runner_placements WHERE workspace_id = :ws "
        "AND step = 'supervise' AND host_id IS NOT NULL AND ended_at IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 5"), {"ws": workspace_id})).mappings().all()
    finished = sum(1 for row in recent if row["reported"] == "finished")
    rounds_ok = not recent or finished > 0
    why = recent[0]["reason"] if recent else None
    check("rounds", rounds_ok, False,
          "No supervisor round has ended yet." if not recent
          else f"{finished} of the last {len(recent)} supervisor round(s) finished."
          + ("" if rounds_ok else f" None ran to the end (last: {why or 'no report'})."),
          "Rounds are offered but do not finish: update the supervisor computer "
          "(`npx veynod@latest`), then read `loop(action=\"metrics\")` for the reason.")
    # Work that could run but does not move.
    stale = (await session.execute(text(
        "SELECT count(*) FROM collab_tasks t WHERE t.workspace_id = :ws AND t.status = 'READY' "
        "AND t.assignee IS NULL AND t.claimed_by IS NULL AND (t.kind IS NULL OR t.kind = 'work') "
        "AND t.updated_at < NOW() - INTERVAL '6 hours'"), {"ws": workspace_id})).scalar_one()
    stopped = (await session.execute(text(
        "SELECT count(*) FROM collab_tasks t WHERE t.workspace_id = :ws AND t.status = 'HANDOFF' "
        "AND t.handed_off_by IS NULL AND (SELECT count(*) FROM runner_placements p "
        "WHERE p.task_id = t.id AND p.step = 'work' AND p.status = 'failed') >= :rounds"),
        {"ws": workspace_id, "rounds": MAX_ROUNDS})).scalar_one()
    check("progress", not stale and not stopped, False,
          "No task has waited long and none is stopped." if not stale and not stopped
          else f"{stale} READY task(s) have waited over 6 hours; {stopped} task(s) are "
               "stopped after failed rounds (they are tried again once those are a day old).",
          "Open the Loop page (Needs you) for the tasks with no computer; "
          "`loop(action=\"metrics\")` lists the stopped ones with what failed.")
    waiting = (await session.execute(select(CollabTask).where(
        CollabTask.workspace_id == workspace_id, CollabTask.status == TaskStatus.READY,
        CollabTask.assignee.is_(None), CollabTask.claimed_by.is_(None),
        (CollabTask.kind.is_(None)) | (CollabTask.kind == "work")).limit(500))).scalars().all()
    reach = [repo for host in builders for repo in host["repos"] or []]
    uncovered = sorted({task.repo for task in waiting
                        if task.repo and not any(same_repo(task.repo, repo) for repo in reach)})
    check("repos", not uncovered, False,
          "Every waiting task's repository is on an online computer." if not uncovered
          else f"No online computer works on {', '.join(uncovered[:5])}.",
          "On a computer that runs tasks: `npx veynod --repo owner/name` (adds it).")
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


def _stage(row: dict[str, Any]) -> str:
    """How far a placement that was offered to a computer got, in one word.

    offered -> delivered (the computer read the poke) -> ready (its worker
    process is up) -> started (the placement was checked and work began) ->
    finished | failed. ``skipped``: a worker arrived after the placement had
    moved on and did nothing. ``not_delivered``: the poke never became a worker.
    """
    if row.get("reported") in ("finished", "failed", "skipped"):
        return row["reported"]
    if row.get("started_at") is not None:
        return "started"
    if row.get("exec_ack_at") is not None:
        return "ready"
    if row.get("qos_status") == "TIMEOUT_REVOKED":
        return "not_delivered"
    if row.get("net_ack_at") is not None:
        return "delivered"
    return "offered" if row.get("host_id") else "waiting"


def _timings(row: dict[str, Any]) -> dict[str, Any] | None:
    """The three times of a delivery, each from the moment the poke was written."""
    if row.get("poke_at") is None:
        return None
    return {
        "sent_at": row["poke_at"].isoformat(),
        # ack.net: the computer read the poke.
        "delivered_ms": row.get("net_rtt_ms") if row.get("net_ack_at") is not None else None,
        # worker.ready: a worker process is up and can take it.
        "worker_ready_ms": (row.get("exec_startup_ms")
                            if row.get("exec_ack_at") is not None else None),
        "warm": row.get("warm"),
        # execution.started: the placement was checked and work on it began.
        "execution_started_ms": row.get("exec_started_ms"),
        "delivery": row.get("qos_status"),
        "delivery_note": row.get("poke_detail"),
    }


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
        "report_note": row.get("report_note"),
        "stage": _stage(row),
        "timings": _timings(row),
        "reason": row["reason"],
        "note": row["note"],
        "needs_you": row["status"] == "unplaced" and row["reason"] not in SELF_RESOLVING,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "started_at": row["started_at"].isoformat() if row.get("started_at") else None,
        "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
    }


# A placement with the receipts of its poke (the frame id is the placement id).
_PLACEMENT_SQL = (
    "SELECT p.*, d.at AS poke_at, d.qos_status, d.detail AS poke_detail, d.net_ack_at, "
    "d.exec_ack_at, d.net_rtt_ms, d.exec_startup_ms, d.exec_started_ms, d.warm "
    "FROM runner_placements p LEFT JOIN poke_deliveries d "
    "ON d.workspace_id = p.workspace_id AND d.frame_id = p.placement_id "
)


async def placements(session: AsyncSession, principal: Principal, *,
                     task_key: str | None = None, open_only: bool = False,
                     step: str | None = None, limit: int = 100) -> dict[str, Any]:
    clauses = ["p.workspace_id = :ws"]
    params: dict[str, Any] = {"ws": principal.workspace.id, "limit": limit}
    if step:
        clauses.append("p.step = :step")
        params["step"] = step
    if task_key:
        clauses.append("p.task_key = :key")
        params["key"] = task_key
    if open_only:
        clauses.append("p.status IN ('suggested', 'offered', 'claimed', 'unplaced')")
    rows = (await session.execute(text(
        f"{_PLACEMENT_SQL} WHERE {' AND '.join(clauses)} "
        "ORDER BY p.created_at DESC LIMIT :limit"), params)).mappings().all()
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
        "SELECT p.*, h.token_id, d.at AS poke_at, d.qos_status, d.detail AS poke_detail, "
        "d.net_ack_at, d.exec_ack_at, d.net_rtt_ms, d.exec_startup_ms, d.exec_started_ms, "
        "d.warm FROM runner_placements p LEFT JOIN runner_hosts h ON h.host_id = p.host_id "
        "LEFT JOIN poke_deliveries d ON d.workspace_id = p.workspace_id "
        "AND d.frame_id = p.placement_id "
        "WHERE p.workspace_id = :ws AND p.placement_id = :id"),
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
    return _placement_out(await _own_placement(session, principal, placement_id))


async def start(session: AsyncSession, principal: Principal, placement_id: str,
                lease_epoch: int | None = None) -> dict[str, Any]:
    """The worker asks to start: is this placement still its to work?

    Both receipts of a poke only say a process is up. This is the check that
    turns "ready" into "started": while the placement is still open it is
    stamped ``started_at`` (the third time of the delivery) and the worker goes
    on; otherwise the answer is ``start: false`` with the reason and the
    placement records ``skipped`` (unless it already carries a report). A worker
    that came up late, after the board moved the round to another computer or a
    newer dispatch took the task, therefore does nothing and is never counted as
    a round that ran.
    """
    row = await _own_placement(session, principal, placement_id)
    why = None
    if row["status"] not in STARTABLE:
        why = f"placement is {row['status']}" + (f" ({row['reason']})" if row["reason"] else "")
    elif row["step"] == "work" and row["task_id"] and lease_epoch is not None:
        # The poke carried the lease epoch its dispatch wrote. A higher one on the
        # task means a later dispatch (or another holder) owns it now.
        current = (await session.execute(text(
            "SELECT lease_epoch FROM collab_tasks WHERE id = :t"),
            {"t": row["task_id"]})).scalar_one_or_none()
        if current is not None and int(current) > int(lease_epoch):
            why = f"stale lease epoch {lease_epoch} (the task is at {current})"
    if why is not None:
        await session.execute(text(
            "UPDATE runner_placements SET reported = 'skipped', reported_at = NOW(), "
            "report_note = :n, updated_at = NOW() "
            "WHERE placement_id = :id AND reported IS NULL"),
            {"n": why[:500], "id": placement_id})
        if row["step"] == "supervise" and row["reported"] is None:
            await append_event(
                session, workspace_id=principal.workspace.id,
                agent=principal.agent_signature or "runner", event_type="LOOP_ROUND_SKIPPED",
                payload={"placement_id": placement_id, "host_id": row["host_id"],
                         "reason": why[:500], "idempotency_key": f"{placement_id}:skipped"})
        await session.commit()
        out = _placement_out(await _own_placement(session, principal, placement_id))
        return {**out, "start": False, "skip_reason": why}
    from app.services import nerve_hub

    await session.execute(text(
        "UPDATE runner_placements SET started_at = COALESCE(started_at, NOW()), "
        "updated_at = NOW() WHERE placement_id = :id"), {"id": placement_id})
    await nerve_hub.record_exec_started(session, workspace_id=principal.workspace.id,
                                        placement_id=placement_id)
    await session.commit()
    row = await _own_placement(session, principal, placement_id)
    return {**_placement_out(row), "start": True, "detail": row.get("detail") or {}}


async def worker_exited(workspace_id: Any, agent_id: str, placement_id: str, *,
                        code: Any = None, signal: Any = None,
                        reason: str | None = None) -> bool:
    """A computer says the worker process of a placement ended.

    A worker reports how its step ended before it exits. One that ends without a
    report (it crashed, was killed, or the process died on its way out) left the
    placement open and, for work, the task under a lease nobody uses. That is a
    failure with a reason, recorded now, so the next pass releases the task
    instead of the lease's expiry doing it a quarter of an hour later. True when
    the placement was marked.
    """
    note = (reason or f"the worker exited without a report (code={code}, signal={signal})")[:500]
    async with async_session_factory() as session:
        marked = (await session.execute(text(
            "UPDATE runner_placements SET reported = 'failed', reported_at = NOW(), "
            "report_note = :n, updated_at = NOW() WHERE workspace_id = :ws "
            "AND placement_id = :id AND agent_id = :agent AND reported IS NULL "
            "AND status IN ('offered', 'claimed') RETURNING placement_id"),
            {"n": note, "ws": workspace_id, "id": placement_id,
             "agent": agent_id})).scalar_one_or_none()
        await session.commit()
    if marked:
        logger.info("placement %s: %s", placement_id, note)
    return marked is not None


def _percentiles(values: list[int]) -> dict[str, Any]:
    """count, p50, p95, p99 and max of a list of milliseconds (nearest rank)."""
    if not values:
        return {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}
    ordered = sorted(values)

    def rank(q: float) -> int:
        return ordered[min(len(ordered), max(1, math.ceil(q * len(ordered)))) - 1]

    return {"count": len(ordered), "p50": rank(0.50), "p95": rank(0.95), "p99": rank(0.99),
            "max": ordered[-1]}


#: What the second-level targets are, so a reader can compare without the spec.
TARGETS_MS = {"delivered_p95": 1000, "execution_started_p95": 2000,
              "execution_started_p99": 5000}


async def loop_metrics(session: AsyncSession, principal: Principal,
                       hours: int = 24) -> dict[str, Any]:
    """How the Loop's three stages did over the last ``hours``.

    Delivery, worker start and execution start are separate numbers, and the
    time the model takes for the whole task is not among them. A supervise round
    succeeds only when its placement reports finished; skipped rounds are their
    own count. ``stopped`` lists the tasks the scheduler gave up on with what
    their rounds failed on, to check one by one before putting any back.
    """
    workspace_id = principal.workspace.id
    hours = max(1, min(int(hours), 24 * 30))
    since = _now() - timedelta(hours=hours)
    rows = [dict(row) for row in (await session.execute(text(
        f"{_PLACEMENT_SQL} WHERE p.workspace_id = :ws AND p.created_at >= :since "
        "AND p.host_id IS NOT NULL ORDER BY p.created_at DESC LIMIT 5000"),
        {"ws": workspace_id, "since": since})).mappings().all()]
    sent = [row for row in rows if row.get("poke_at") is not None]
    delivered = [row["net_rtt_ms"] for row in sent
                 if row.get("net_ack_at") is not None and row.get("net_rtt_ms") is not None]
    ready = [row["exec_startup_ms"] for row in sent
             if row.get("exec_ack_at") is not None and row.get("exec_startup_ms") is not None]
    started = [row for row in sent if row.get("exec_started_ms") is not None]
    warm = [row["exec_started_ms"] for row in started if row.get("warm") is True]
    refused: dict[str, int] = {}
    for row in sent:
        if row.get("qos_status") == "TIMEOUT_REVOKED":
            # Group by what went wrong, not by the free text after it.
            reason = str(row.get("poke_detail") or "unknown")
            key = reason.split(":")[1].strip() if reason.startswith("AGENT_REJECTED:") \
                else reason.split(":")[0]
            refused[key[:60]] = refused.get(key[:60], 0) + 1
    timings = {
        "delivered_ms": _percentiles(delivered),
        "worker_ready_ms": _percentiles(ready),
        "execution_started_ms": _percentiles([row["exec_started_ms"] for row in started]),
        "execution_started_warm_ms": _percentiles(warm),
    }
    meets = {
        "delivered_p95": (timings["delivered_ms"]["p95"] is not None
                          and timings["delivered_ms"]["p95"] <= TARGETS_MS["delivered_p95"]),
        "execution_started_p95": (
            timings["execution_started_warm_ms"]["p95"] is not None
            and timings["execution_started_warm_ms"]["p95"] <= TARGETS_MS["execution_started_p95"]),
        "execution_started_p99": (
            timings["execution_started_warm_ms"]["p99"] is not None
            and timings["execution_started_warm_ms"]["p99"] <= TARGETS_MS["execution_started_p99"]),
    }
    by_step: dict[str, dict[str, int]] = {}
    for row in rows:
        entry = by_step.setdefault(row["step"], {})
        stage = _stage(row) if row["status"] not in OPEN else "open"
        entry[stage] = entry.get(stage, 0) + 1
    rounds = [row for row in rows if row["step"] == "supervise"]
    ended = [row for row in rounds if row["status"] not in OPEN]
    finished = [row for row in ended if row.get("reported") == "finished"]
    # Newest first: how many rounds in a row ended finished.
    streak = 0
    for row in ended:
        if row.get("reported") != "finished":
            break
        streak += 1
    supervise = {
        "offered": len(rounds),
        "open": len(rounds) - len(ended),
        "finished": len(finished),
        "failed": sum(1 for row in ended if row.get("reported") == "failed"),
        "skipped": sum(1 for row in ended if row.get("reported") == "skipped"),
        "not_delivered": sum(1 for row in ended if row.get("reported") is None
                             and row.get("qos_status") == "TIMEOUT_REVOKED"),
        "moved": sum(1 for row in ended if row["status"] == "moved"),
        "finished_rate": round(len(finished) / len(ended), 4) if ended else None,
        "finished_in_a_row": streak,
    }
    stopped = []
    for row in (await session.execute(text(
            "SELECT t.task_key, t.title, t.assigned_role, t.required_role, t.repo, "
            "t.required_capabilities, t.updated_at FROM collab_tasks t "
            "WHERE t.workspace_id = :ws AND t.status = 'HANDOFF' "
            "AND (SELECT count(*) FROM runner_placements p WHERE p.task_id = t.id "
            "AND p.step = 'work' AND p.status = 'failed') >= :rounds "
            "ORDER BY t.updated_at DESC LIMIT 100"),
            {"ws": workspace_id, "rounds": MAX_ROUNDS})).mappings():
        history = (await session.execute(text(
            "SELECT status, reason, note, host_id, ended_at FROM runner_placements "
            "WHERE workspace_id = :ws AND task_key = :k AND step = 'work' "
            "AND status IN ('failed', 'moved') ORDER BY created_at DESC LIMIT 6"),
            {"ws": workspace_id, "k": row["task_key"]})).mappings().all()
        stopped.append({
            "task_key": row["task_key"], "title": row["title"],
            "role": row["required_role"] or row["assigned_role"], "repo": row["repo"],
            "required_capabilities": list(row["required_capabilities"] or []),
            "rounds": [{"status": item["status"], "reason": item["reason"], "note": item["note"],
                        "host_id": item["host_id"],
                        "ended_at": item["ended_at"].isoformat() if item["ended_at"] else None}
                       for item in history],
        })
    return {
        "window_hours": hours, "since": since.isoformat(),
        "pokes": {"sent": len(sent), "delivered": len(delivered), "worker_ready": len(ready),
                  "execution_started": len(started),
                  "not_delivered": sum(refused.values()),
                  "worker_start_failure_rate": (round(sum(refused.values()) / len(sent), 4)
                                                if sent else None)},
        "timings": timings, "targets_ms": TARGETS_MS, "meets_targets": meets,
        "not_delivered_reasons": dict(sorted(refused.items(), key=lambda item: -item[1])),
        "by_step": by_step, "supervise": supervise, "stopped": stopped,
    }


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
