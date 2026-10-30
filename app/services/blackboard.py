"""The live blackboard (agentgraph/spec-live-blackboard v1): an observation surface.

One snapshot answers the first screen in full (M-01, A-01): the three columns,
every agent with its presence, who works on what, and the event cursor the
snapshot was read at. The page then follows the workspace event stream from
that cursor (Last-Event-ID), so every later change arrives as an event and
nothing is polled; ``task_keys=`` refreshes just the cards an event touched.

Columns follow the task state machine (P-B: no local guessing):

* to do       TODO, READY, HANDOFF (waiting for its next holder)
* in progress IN_PROGRESS, REVIEWING, AWAITING_HUMAN, ESCALATED_TO_HUMAN,
              WAITING_HUMAN
* resolved    DONE, the newest ``resolved_limit`` only (M-03, default 10)

Older DONE tasks, and FAILED / CANCELLED ones, live in the archive (M-10):
searchable by time, agent and keyword, out of the board's way. The board has
no actions (the operations scope is the owner's to decide).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.errors import ApiError
from app.services import presence

TODO = ("TODO", "READY", "HANDOFF")
IN_PROGRESS = ("IN_PROGRESS", "REVIEWING", "AWAITING_HUMAN", "ESCALATED_TO_HUMAN",
               "WAITING_HUMAN")
RESOLVED = ("DONE",)
ARCHIVED_ONLY = ("FAILED", "CANCELLED")
# Suggested values (spec: confirmed by the owner, then final).
DEFAULT_RESOLVED_LIMIT = 10
MAX_OPEN_CARDS = 500

_CARD_COLUMNS = (
    "task_key, title, status, priority, namespace, kind, claimed_by, lease_owner, "
    "assignee, handed_off_by, created_by, version, parent_task_key, updated_at, created_at, "
    # A task waiting for a person has no holder; who asked is the last actor.
    "CASE WHEN status IN ('AWAITING_HUMAN', 'ESCALATED_TO_HUMAN', 'WAITING_HUMAN') THEN "
    "(SELECT a.agent_signature FROM state_audit_diffs a WHERE a.workspace_id = "
    "collab_tasks.workspace_id AND a.target_table = 'collab_tasks' AND a.target_record_id = "
    "collab_tasks.id ORDER BY a.created_at DESC LIMIT 1) END AS asked_by"
)


def column_of(status: str) -> str | None:
    if status in TODO:
        return "todo"
    if status in IN_PROGRESS:
        return "in_progress"
    if status in RESOLVED:
        return "resolved"
    return None


def _namespaces(principal: Principal) -> list[str] | None:
    if principal.is_commander or "*" in principal.allowed_namespaces:
        return None
    return list(principal.allowed_namespaces)


def _scope(principal: Principal, params: dict[str, Any]) -> str:
    params["ws"] = principal.workspace.id
    allowed = _namespaces(principal)
    if allowed is None:
        return "workspace_id = :ws"
    params["nss"] = allowed
    return "workspace_id = :ws AND namespace = ANY(CAST(:nss AS text[]))"


class _Agents:
    """Directory entries keyed by every name a task field may carry."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = entries
        self.by_name: dict[str, str] = {}
        # Same precedence as the directory: an id beats a current signature,
        # which beats an old name another entry used to carry.
        for field in ("agent_id", "principal_id", "signatures", "legacy_names"):
            for entry in entries:
                value = entry.get(field)
                for name in (value if isinstance(value, list) else [value]):
                    if name:
                        self.by_name.setdefault(name, entry["agent_id"])
                        self.by_name.setdefault(f"agent:{name}", entry["agent_id"])

    def resolve(self, name: str | None) -> str | None:
        if not name:
            return None
        return self.by_name.get(name) or self.by_name.get(name.removeprefix("agent:"))


async def _agents(session: AsyncSession, principal: Principal) -> _Agents:
    rows = (await session.execute(text(
        "SELECT agent_id, principal_id, display_name, role, signatures, legacy_names, "
        "capabilities FROM agent_directory WHERE workspace_id = :ws AND status <> 'retired' "
        "ORDER BY agent_id"), {"ws": principal.workspace.id})).mappings().all()
    return _Agents([dict(row) for row in rows])


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _card(row: dict[str, Any], agents: _Agents) -> dict[str, Any]:
    status = row["status"]
    holder = row["claimed_by"] or row["lease_owner"]
    # The face on the card: whoever holds it, else whoever it waits for, else
    # whoever finished it, else its publisher.
    owner = holder or row["assignee"] or row["handed_off_by"] or row["created_by"]
    return {
        "task_key": row["task_key"],
        "title": row["title"],
        "status": status,
        "column": column_of(status) or "archive",
        "priority": row["priority"],
        "namespace": row["namespace"],
        "kind": row["kind"],
        "version": row["version"],
        "parent_task_key": row["parent_task_key"],
        "holder": holder,
        "holder_agent": agents.resolve(holder),
        "owner": owner,
        "owner_agent": agents.resolve(owner),
        "assignee": row["assignee"],
        "created_by": row["created_by"],
        "created_by_agent": agents.resolve(row["created_by"]),
        "asked_by_agent": agents.resolve(row.get("asked_by")),
        "updated_at": _iso(row["updated_at"]),
        "created_at": _iso(row["created_at"]),
    }


async def _cursor(session: AsyncSession, principal: Principal) -> int:
    return int(await session.scalar(text(
        "SELECT COALESCE(max(id), 0) FROM workspace_events WHERE workspace_id = :ws"),
        {"ws": principal.workspace.id}) or 0)


async def snapshot(session: AsyncSession, principal: Principal, *,
                   resolved_limit: int = DEFAULT_RESOLVED_LIMIT,
                   task_keys: list[str] | None = None) -> dict[str, Any]:
    if not 1 <= resolved_limit <= 100:
        raise ApiError(422, "validation_error", "resolved_limit is 1-100",
                       details={"field": "resolved_limit"})
    # The cursor is read first: an event committed while the rest is read is
    # replayed on top of the snapshot, never lost between the two (A-07).
    cursor = await _cursor(session, principal)
    agents = await _agents(session, principal)
    params: dict[str, Any] = {}
    scope = _scope(principal, params)

    if task_keys is not None:
        params["keys"] = task_keys[:200]
        found = (await session.execute(text(
            f"SELECT {_CARD_COLUMNS} FROM collab_tasks WHERE {scope} "
            "AND task_key = ANY(CAST(:keys AS text[]))"), params)).mappings().all()
        cards = [_card(dict(row), agents) for row in found]
        return {"cursor": cursor, "cards": cards,
                "missing": sorted(set(params["keys"]) - {card["task_key"] for card in cards})}

    params["open"] = list(TODO + IN_PROGRESS)
    params["max_open"] = MAX_OPEN_CARDS + 1
    open_rows = (await session.execute(text(
        f"SELECT {_CARD_COLUMNS} FROM collab_tasks WHERE {scope} "
        "AND status::text = ANY(CAST(:open AS text[])) "
        "ORDER BY priority DESC, updated_at DESC LIMIT :max_open"), params)).mappings().all()
    params["limit"] = resolved_limit
    resolved_rows = (await session.execute(text(
        f"SELECT {_CARD_COLUMNS} FROM collab_tasks WHERE {scope} AND status = 'DONE' "
        "ORDER BY updated_at DESC, task_key LIMIT :limit"), params)).mappings().all()
    archived = int(await session.scalar(text(
        f"SELECT count(*) FROM collab_tasks WHERE {scope} AND status IN "
        "('DONE', 'FAILED', 'CANCELLED')"), params) or 0) - len(resolved_rows)

    columns: dict[str, list[dict[str, Any]]] = {"todo": [], "in_progress": [], "resolved": []}
    for row in list(open_rows[:MAX_OPEN_CARDS]) + list(resolved_rows):
        card = _card(dict(row), agents)
        columns[card["column"]].append(card)

    states = await presence.for_principals(
        session, principal.workspace.id, [entry["principal_id"] for entry in agents.entries])
    working: dict[str, list[str]] = {}
    for card in columns["in_progress"]:
        if card["status"] == "IN_PROGRESS" and card["holder_agent"]:
            working.setdefault(card["holder_agent"], []).append(card["task_key"])
    avatars = []
    for entry in agents.entries:
        info = states.get(entry["principal_id"]) or {"state": "unwakeable", "online": False}
        tasks = working.get(entry["agent_id"], [])
        avatars.append({
            "agent_id": entry["agent_id"],
            "display_name": entry["display_name"],
            "role": entry["role"],
            "presence": info,
            # M-04: online idle, working, or (any state but online) offline.
            "activity": ("working" if tasks else "idle") if info.get("online") else "offline",
            "working_on": tasks,
            # Every name an event may carry for this agent (actor, sender, targets).
            "names": sorted({name for name in (entry["agent_id"], entry["principal_id"],
                                               *(entry["signatures"] or []),
                                               *(entry["legacy_names"] or [])) if name}),
        })
    return {
        "cursor": cursor,
        "generated_at": datetime.now().astimezone().isoformat(),
        "columns": columns,
        "counts": {name: len(cards) for name, cards in columns.items()},
        "truncated": {"todo_in_progress": len(open_rows) > MAX_OPEN_CARDS},
        "archived": max(0, archived),
        "resolved_limit": resolved_limit,
        "agents": avatars,
        "presence_rules": {"heartbeat_every_s": presence.TOUCH_EVERY_SECONDS,
                           "offline_after_s": int(presence.ONLINE_WINDOW.total_seconds())},
        "stream": {"tables": ["collab_tasks", "timeline_streams"],
                   "resume_with": "Last-Event-ID = cursor"},
    }


def _decode_cursor(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        return max(0, int(json.loads(base64.urlsafe_b64decode(raw.encode()))["offset"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(422, "invalid_cursor", "cursor is not one this API returned",
                       details={"field": "cursor"}) from exc


async def archive(session: AsyncSession, principal: Principal, *, q: str | None = None,
                  agent: str | None = None, since: datetime | None = None,
                  until: datetime | None = None, resolved_limit: int = DEFAULT_RESOLVED_LIMIT,
                  cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Archived tasks (M-10): DONE beyond the board's newest N, FAILED, CANCELLED."""
    limit = max(1, min(limit, 200))
    offset = _decode_cursor(cursor)
    agents = await _agents(session, principal)
    params: dict[str, Any] = {"lim": limit + 1, "off": offset, "keep": resolved_limit}
    scope = _scope(principal, params)
    where = [scope, "status IN ('DONE', 'FAILED', 'CANCELLED')",
             # The newest N DONE tasks are on the board, not in the archive.
             "NOT (status = 'DONE' AND task_key IN (SELECT task_key FROM collab_tasks "
             f"WHERE {scope} AND status = 'DONE' ORDER BY updated_at DESC, task_key "
             "LIMIT :keep))"]
    if q:
        params["q"] = f"%{q.strip()}%"
        where.append("(title ILIKE :q OR task_key ILIKE :q OR description ILIKE :q)")
    if agent:
        names = [agent, agent.removeprefix("agent:")]
        entry = next((item for item in agents.entries
                      if agents.resolve(agent) == item["agent_id"]), None)
        if entry is not None:
            names += [entry["agent_id"], entry["principal_id"], *(entry["signatures"] or []),
                      *(entry["legacy_names"] or [])]
        names += [f"agent:{name}" for name in list(names)]
        params["who"] = list(dict.fromkeys(names))
        where.append("(claimed_by = ANY(CAST(:who AS text[])) OR lease_owner = "
                     "ANY(CAST(:who AS text[])) OR handed_off_by = ANY(CAST(:who AS text[])) "
                     "OR assignee = ANY(CAST(:who AS text[])) OR created_by = "
                     "ANY(CAST(:who AS text[])) OR reviewed_by = ANY(CAST(:who AS text[])))")
    if since:
        params["since"] = since
        where.append("updated_at >= :since")
    if until:
        params["until"] = until
        where.append("updated_at < :until")
    found = (await session.execute(text(
        f"SELECT {_CARD_COLUMNS} FROM collab_tasks WHERE {' AND '.join(where)} "
        "ORDER BY updated_at DESC, task_key LIMIT :lim OFFSET :off"), params)).mappings().all()
    items = []
    for row in found[:limit]:
        card = _card(dict(row), agents)
        card["column"] = "archive"
        items.append(card)
    next_cursor = None
    if len(found) > limit:
        next_cursor = base64.urlsafe_b64encode(
            json.dumps({"offset": offset + limit}).encode()).decode()
    return {"items": items, "next_cursor": next_cursor}


async def card_events(session: AsyncSession, principal: Principal, task_key: str,
                      limit: int = 20) -> dict[str, Any]:
    """The events behind a card (M-08: every animation traces back to one)."""
    params: dict[str, Any] = {"k": task_key, "lim": max(1, min(limit, 100))}
    scope = _scope(principal, params)
    exists = await session.scalar(text(
        f"SELECT 1 FROM collab_tasks WHERE {scope} AND task_key = :k"), params)
    if not exists:
        raise ApiError(404, "not_found", f"task '{task_key}' not found",
                       details={"task_key": task_key})
    rows = (await session.execute(text(
        "SELECT id, type, actor, status, version, targets, summary, created_at "
        "FROM workspace_events WHERE workspace_id = :ws AND task_key = :k "
        "ORDER BY id DESC LIMIT :lim"), params)).mappings().all()
    return {"task_key": task_key, "events": [
        {"seq": row["id"], "event_id": f"evt_{row['id']}", "type": row["type"],
         "actor": row["actor"], "status": row["status"], "version": row["version"],
         "targets": list(row["targets"] or []), "summary": row["summary"],
         "at": _iso(row["created_at"])} for row in rows]}
