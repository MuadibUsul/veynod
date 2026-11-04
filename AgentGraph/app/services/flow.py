"""The Loop page: the engine's view across every diagram (v3.24).

A diagram is the plan; the Loop only moves its pieces along. This read model
answers the page in one call, so the console never stitches five lists:

* ``lanes``: every open piece of work, grouped the way it was planned. A
  diagram is a lane of its chunks; a task with subtasks is a lane of itself
  and its children; three or more tasks whose keys share a family
  (``fedv2-…``) are a lane; the rest is the inbox. Every piece has one state
  from ``STATES``, whatever kind it is, and a time in that state.
* ``stuck``: per lane, the piece that has waited longest in a state that
  should move on its own.
* ``needs_you``: approved work waiting for a deploy, and tasks waiting for a
  person. (Placements without a computer and alerts have their own lists.)
* ``today``: approvals, returns and completions in the last 24 hours, with an
  hourly series.
* ``agents``: the directory without the computers' seats, each with what it
  holds and its last action. ``timeline``: the latest events, as data the
  console puts in words.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.services import diagrams, presence, runners
from app.services.kernel_delivery import kernel_owns_steps

# One state for every piece of work, in the order a piece moves through them.
STATES = ("not_ready", "ready", "doing", "review", "you", "deploy", "done")
# States that should move without anyone being asked; the longest wait is "stuck".
MOVING = ("ready", "doing", "review", "you", "deploy")
OPEN = ("TODO", "READY", "IN_PROGRESS", "REVIEWING", "HANDOFF", "WAITING_HUMAN",
        "AWAITING_HUMAN", "ESCALATED_TO_HUMAN")
PERSON = ("WAITING_HUMAN", "AWAITING_HUMAN", "ESCALATED_TO_HUMAN")
REVIEW_CHUNK = ("awaiting_gate", "awaiting_ci", "awaiting_review", "ready_to_merge",
                "queued", "verified")
TIMELINE_TYPES = (
    "TASK_CREATED", "TASK_CLAIMED", "TASK_HANDOFF", "TASK_APPROVED", "HUMAN_APPROVED",
    "TASK_REJECTED", "HUMAN_REJECTED", "TASK_RETURNED_FOR_REWORK", "TASK_ESCALATED",
    "TASK_PLACED", "TASK_UNPLACED", "TASK_CANCELLED", "HUMAN_REVIEW_REQUESTED",
    "HOST_CONNECTED", "HOST_PAUSED", "HOST_REVOKED", "LOOP_ROUND", "DEPLOY_VERIFIED",
    "CHUNK_DISPATCHED", "CHUNK_SUBMITTED", "CHUNK_MERGED", "CHUNK_CI_FAILED",
    "CHUNK_CIRCUIT_BROKEN", "DIAGRAM_CONFIRMED", "DIAGRAM_CHANGE_PROPOSED",
)
APPROVED = ("TASK_APPROVED", "HUMAN_APPROVED")
RETURNED = ("TASK_REJECTED", "HUMAN_REJECTED", "TASK_RETURNED_FOR_REWORK")
# A review placement left unplaced for these reasons will never find a computer
# on its own: on a one-computer Loop the builder may not review its own work.
PERSON_REVIEW_REASONS = ("no_reviewer_host",)
# Too generic to name a family of tasks.
NO_FAMILY = {"task", "dg", "ops", "tmp", "test"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return value or None


def _approved(row: dict[str, Any]) -> bool:
    brief = row.get("handoff_brief") or {}
    approval = brief.get("approval") if isinstance(brief, dict) else None
    if isinstance(approval, dict):
        return bool(approval.get("awaiting_deploy"))
    # Approved before approvals were recorded on the brief: reviewed, nothing to address.
    return bool(row.get("reviewed_by")) and not row.get("review_feedback")


def task_state(row: dict[str, Any]) -> str:
    status = row["status"]
    if status == "TODO":
        return "not_ready"
    if status == "READY":
        return "ready"
    if status == "IN_PROGRESS":
        return "doing"
    if status == "REVIEWING":
        # The person is the reviewer when the Kernel already gated and merged the
        # work, or when no other computer can review it ("review" moves alone).
        return "you" if row.get("review_by") == "person" else "review"
    if status in PERSON:
        return "you"
    if status == "HANDOFF":
        # Approved and held for a deploy, or handed over to whoever is next.
        return "deploy" if _approved(row) else "ready"
    return "done"


def chunk_state(chunk: dict[str, Any]) -> str:
    status = chunk.get("status") or chunk.get("state")
    if status in ("merged", "retired"):
        return "done"
    if status in ("pending", "blocked"):
        return "not_ready"
    if status in REVIEW_CHUNK:
        return "review"
    if status == "in_progress" or chunk.get("task_status") == "IN_PROGRESS":
        return "doing"
    return "ready"


def _family(key: str) -> str | None:
    head = re.split(r"[-_/]", key.lower(), maxsplit=1)[0]
    return head if len(head) >= 3 and head not in NO_FAMILY else None


def _family_title(titles: list[str], family: str) -> str:
    """The words the tasks' titles share ("联邦 02/24 …" → "联邦"), else the key family."""
    if not titles:
        return family
    first, last = min(titles), max(titles)
    common = ""
    for a, b in zip(first, last, strict=False):
        if a != b:
            break
        common += a
    common = re.sub(r"[\s\d/:：·.,，-]+$", "", common).strip()
    return common if len(common) >= 2 else family


async def _directory(session: AsyncSession, workspace_id: Any) -> list[dict[str, Any]]:
    rows = (await session.execute(text(
        "SELECT agent_id, principal_id, display_name, role, signatures, legacy_names "
        "FROM agent_directory WHERE workspace_id = :ws AND status <> 'retired' "
        "ORDER BY agent_id"), {"ws": workspace_id})).mappings().all()
    return [dict(row) for row in rows]


def _resolver(entries: list[dict[str, Any]]):
    names: dict[str, str] = {}
    for field in ("agent_id", "principal_id", "signatures", "legacy_names"):
        for entry in entries:
            value = entry.get(field)
            for name in (value if isinstance(value, list) else [value]):
                if name:
                    names.setdefault(name, entry["agent_id"])

    def resolve(name: str | None) -> str | None:
        if not name:
            return None
        name = name.removeprefix("agent:")
        return names.get(name)
    return resolve


async def overview(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    ws = principal.workspace.id
    now = _now()
    entries = await _directory(session, ws)
    resolve = _resolver(entries)
    machines = await runners.computers(session, ws)
    seat_of = {seat["agent_id"]: machine for machine in machines
               for seat in machine["seats"] if seat["agent_id"]}

    rows = [dict(row) for row in (await session.execute(text(
        "SELECT task_key, title, status::text AS status, priority, namespace, parent_task_key, "
        "root_task_key, claimed_by, lease_owner, assignee, reviewed_by, review_feedback, "
        "handoff_brief, handed_off_by, correlation, updated_at, created_at FROM collab_tasks "
        "WHERE workspace_id = :ws AND (status::text = ANY(CAST(:open AS text[])) "
        "OR (status = 'DONE' AND updated_at > NOW() - INTERVAL '7 days')) "
        "ORDER BY priority DESC, updated_at DESC LIMIT 1500"),
        {"ws": ws, "open": list(OPEN)})).mappings().all()]
    by_key = {row["task_key"]: row for row in rows}
    stranded = set((await session.execute(text(
        "SELECT task_key FROM runner_placements WHERE workspace_id = :ws AND step = 'review' "
        "AND status = 'unplaced' AND reason = ANY(CAST(:reasons AS text[]))"),
        {"ws": ws, "reasons": list(PERSON_REVIEW_REASONS)})).scalars().all())
    for row in rows:
        if row["status"] == "REVIEWING" and (
                kernel_owns_steps(row) or row["task_key"] in stranded):
            row["review_by"] = "person"

    def holder_of(row: dict[str, Any]) -> tuple[str | None, str | None]:
        agent = resolve(row.get("claimed_by") or row.get("lease_owner"))
        machine = seat_of.get(agent) if agent else None
        return agent, machine["name"] if machine else None

    def item(key: str, title: str, state: str, since: Any, row: dict[str, Any] | None,
             **extra: Any) -> dict[str, Any]:
        agent, where = holder_of(row) if row else (None, None)
        return {"key": key, "title": title, "state": state, "since": _iso(since),
                "status": row["status"] if row else None, "holder": agent, "where": where,
                "reviewed_by": resolve(row.get("reviewed_by")) if row else None, **extra}

    lanes: list[dict[str, Any]] = []
    taken: set[str] = set()

    # Diagrams first: their chunks are the pieces, their tasks are those chunks.
    listed = (await diagrams.list_diagrams(session, principal))["items"]
    for entry in listed:
        detail = await diagrams.view(session, principal, entry["diagram_key"])
        items = []
        for chunk in detail["chunks"]:
            if chunk.get("status") == "retired":
                continue
            row = by_key.get(chunk.get("task_key") or "")
            if chunk.get("task_key"):
                taken.add(chunk["task_key"])
            state = chunk_state(chunk)
            since = ((chunk.get("submission") or {}).get("at") if state == "review"
                     else (chunk.get("merge") or {}).get("at") if state == "done"
                     else row["updated_at"] if row else entry["updated_at"])
            waiting = [dep for dep in chunk.get("depends_on") or []
                       if next((c for c in detail["chunks"] if c["chunk_key"] == dep), {})
                       .get("state") != "merged"] if state == "not_ready" else []
            items.append(item(chunk["chunk_key"], chunk["title"], state, since, row,
                              task_key=chunk.get("task_key"), waiting_on=waiting,
                              layer=chunk.get("layer")))
        lanes.append({"lane_id": f"diagram:{entry['diagram_key']}", "kind": "diagram",
                      "key": entry["diagram_key"], "title": entry["title"],
                      "status": entry["status"], "items": items})
    # Chunk tasks are also named DG-<diagram>-<chunk>[-n]: never count them twice.
    taken |= {key for key in by_key if key.startswith("DG-")}

    open_rows = [row for row in rows if row["task_key"] not in taken]
    # A task with subtasks is a lane of itself and its children.
    roots: dict[str, list[dict[str, Any]]] = {}
    for row in open_rows:
        root = row.get("root_task_key") or row.get("parent_task_key")
        if root and root != row["task_key"]:
            roots.setdefault(root, []).append(row)
    for root, children in roots.items():
        head = by_key.get(root)
        members = ([head] if head else []) + children
        if not any(member["status"] != "DONE" for member in members):
            continue
        taken |= {member["task_key"] for member in members}
        lanes.append({"lane_id": f"task:{root}", "kind": "umbrella", "key": root,
                      "title": head["title"] if head else root,
                      "items": [item(member["task_key"], member["title"], task_state(member),
                                     member["updated_at"], member) for member in members]})

    loose = [row for row in open_rows if row["task_key"] not in taken and row["status"] != "DONE"]
    families: dict[str, list[dict[str, Any]]] = {}
    for row in loose:
        family = _family(row["task_key"])
        if family:
            families.setdefault(family, []).append(row)
    for family, members in families.items():
        if len(members) < 3:
            continue
        taken |= {member["task_key"] for member in members}
        lanes.append({"lane_id": f"family:{family}", "kind": "family", "key": family,
                      "title": _family_title([member["title"] for member in members], family),
                      "items": [item(member["task_key"], member["title"], task_state(member),
                                     member["updated_at"], member) for member in members]})
    inbox = [row for row in loose if row["task_key"] not in taken]
    if inbox:
        lanes.append({"lane_id": "inbox", "kind": "inbox", "key": "inbox", "title": None,
                      "items": [item(row["task_key"], row["title"], task_state(row),
                                     row["updated_at"], row) for row in inbox]})

    order = {state: index for index, state in enumerate(STATES)}
    for lane in lanes:
        lane["items"].sort(key=lambda entry: (order[entry["state"]], entry["since"] or ""))
        lane["counts"] = {state: sum(1 for entry in lane["items"] if entry["state"] == state)
                          for state in STATES}
        lane["total"] = len(lane["items"])
        moving = [entry for entry in lane["items"] if entry["state"] in MOVING and entry["since"]]
        lane["stuck"] = min(moving, key=lambda entry: entry["since"]) if moving else None
    # What needs a person first, then what waits for a deploy, then the longest wait.
    lanes.sort(key=lambda lane: (
        0 if lane["counts"]["you"] else 1 if lane["counts"]["deploy"] else 2,
        lane["kind"] == "inbox",
        (lane["stuck"] or {}).get("since") or "9999"))
    lanes = [lane for lane in lanes if any(entry["state"] != "done" for entry in lane["items"])
             or lane["kind"] == "diagram"]

    pieces = [entry for lane in lanes for entry in lane["items"]]
    needs_you = {
        "deploy": [{"key": entry["key"], "title": entry["title"], "since": entry["since"]}
                   for entry in pieces if entry["state"] == "deploy"],
        # ``why``: "review" when the person is the task's reviewer, "answer" when
        # an agent asked the person something.
        "waiting": [{"key": entry["key"], "title": entry["title"], "since": entry["since"],
                     "asked_by": entry["holder"],
                     "why": "review" if entry["status"] == "REVIEWING" else "answer"}
                    for entry in pieces if entry["state"] == "you"],
    }

    # The last day, from the event log: approvals, returns, completions.
    since = now - timedelta(hours=24)
    events = [dict(row) for row in (await session.execute(text(
        "SELECT event_type, created_at FROM timeline_streams WHERE workspace_id = :ws "
        "AND created_at > :since AND event_type = ANY(CAST(:types AS text[]))"),
        {"ws": ws, "since": since, "types": list(APPROVED + RETURNED)})).mappings().all()]
    done_rows = [row for row in rows if row["status"] == "DONE" and row["updated_at"]
                 and row["updated_at"] > since]
    hours = [{"approved": 0, "done": 0} for _ in range(24)]

    def bucket(at: datetime) -> int:
        return max(0, min(23, 23 - int((now - at).total_seconds() // 3600)))
    for event in events:
        if event["event_type"] in APPROVED:
            hours[bucket(event["created_at"])]["approved"] += 1
    for row in done_rows:
        hours[bucket(row["updated_at"])]["done"] += 1
    today = {"approved": sum(1 for event in events if event["event_type"] in APPROVED),
             "returned": sum(1 for event in events if event["event_type"] in RETURNED),
             "done": len(done_rows), "hours": hours}

    # Agents: people's and cloud agents, not the computers' seats.
    people = [entry for entry in entries if entry["agent_id"] not in seat_of]
    states = await presence.for_principals(session, ws, [entry["principal_id"] for entry in people])
    names = sorted({name for entry in people for name in
                    (entry["agent_id"], *(entry["signatures"] or []))})
    last = {}
    if names:
        for row in (await session.execute(text(
                "SELECT DISTINCT ON (agent_signature) agent_signature, event_type, "
                "payload->>'task_key' AS task_key, created_at FROM timeline_streams "
                "WHERE workspace_id = :ws AND agent_signature = ANY(CAST(:names AS text[])) "
                "AND created_at > NOW() - INTERVAL '14 days' "
                "ORDER BY agent_signature, created_at DESC"),
                {"ws": ws, "names": names})).mappings():
            agent = resolve(row["agent_signature"])
            if agent and (agent not in last or row["created_at"] > last[agent]["at"]):
                last[agent] = {"at": row["created_at"], "event_type": row["event_type"],
                               "task_key": row["task_key"]}
    holding: dict[str, list[str]] = {}
    for row in rows:
        if row["status"] in ("IN_PROGRESS", "REVIEWING", *PERSON):
            agent = resolve(row.get("claimed_by") or row.get("lease_owner"))
            if agent:
                holding.setdefault(agent, []).append(row["task_key"])
    agents = [{
        "agent_id": entry["agent_id"], "display_name": entry["display_name"], "role": entry["role"],
        "presence": states.get(entry["principal_id"]) or {"state": "unwakeable", "online": False},
        "holding": holding.get(entry["agent_id"], []),
        "last": {**last[entry["agent_id"]], "at": _iso(last[entry["agent_id"]]["at"])}
        if entry["agent_id"] in last else None,
    } for entry in people]
    agents.sort(key=lambda agent: (not agent["presence"].get("online"),
                                   -(datetime.fromisoformat(agent["last"]["at"]).timestamp()
                                     if agent["last"] else 0)))

    timeline = [{
        "at": _iso(row["created_at"]), "type": row["event_type"],
        "actor": resolve(row["agent_signature"]) or row["agent_signature"],
        "task_key": row["task_key"], "diagram_key": row["diagram_key"],
        "chunk_key": row["chunk_key"], "awaiting_deploy": row["awaiting_deploy"] == "true",
        "reason": row["reason"],
    } for row in (await session.execute(text(
        "SELECT event_type, agent_signature, created_at, payload->>'task_key' AS task_key, "
        "COALESCE(payload->>'diagram_key', payload->'details'->>'diagram_key') AS diagram_key, "
        "payload->'details'->>'chunk_key' AS chunk_key, "
        "COALESCE(payload->>'awaiting_deploy', payload->'details'->>'awaiting_deploy') "
        "AS awaiting_deploy, payload->>'reason' AS reason FROM timeline_streams "
        "WHERE workspace_id = :ws AND event_type = ANY(CAST(:types AS text[])) "
        "ORDER BY created_at DESC LIMIT 40"),
        {"ws": ws, "types": list(TIMELINE_TYPES)})).mappings()]

    return {"generated_at": now.isoformat(), "states": list(STATES), "lanes": lanes,
            "open": sum(1 for entry in pieces if entry["state"] != "done"),
            "needs_you": needs_you, "today": today, "agents": agents, "timeline": timeline}
