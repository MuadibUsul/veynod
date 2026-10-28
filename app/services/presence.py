"""Presence states (blueprint L2; spec-realtime-messaging M-05a / M-01a).

Every registered agent is in exactly one of four states:

``online``      an open inbox stream or a loop heartbeat within the last 85 s
                (heartbeats every 30 s; it is gone before a third one is
                missed, and announced within 90 s of its last beat);
``offline``     neither, but a working wake channel: a poke will wake it;
``degraded``    neither, and its wake channel is failing (retrying or DEAD);
``unwakeable``  neither, and no wake channel at all: nothing reaches it until
                it comes back by itself. The board shows it as such.

The state an agent was last announced in is kept on its directory entry, and
every change is published once as an ``AGENT_PRESENCE_CHANGED`` timeline event
(stream, webhooks, the live blackboard). Reconciliation runs when a stream
opens or closes, on each heartbeat, when a wake URL is set, and every 5 s in
``presence_loop``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.errors import ApiError
from app.services.common import append_event
from app.services.pokes import INSTANCE, ONLINE_WINDOW, TOUCH_EVERY_SECONDS, _now, _unscoped

logger = logging.getLogger("agentgraph.presence")

STATES = ("online", "offline", "degraded", "unwakeable")
EVENT = "AGENT_PRESENCE_CHANGED"
# Three missed 30 s heartbeats is 90 s. The state is judged against 85 s and
# re-checked every 5 s, so a silent agent is announced within 90 s of its
# last sign of life, never later.
LIVE_WINDOW = timedelta(seconds=85)
CHECK_EVERY_SECONDS = 5

_STATE_SQL = """
SELECT d.workspace_id, d.agent_id, d.principal_id, d.display_name, d.presence_state,
       p.seen, p.live, p.transports,
       w.id AS wake_id, w.active AS wake_active, w.consecutive_failures,
       w.failing_since, w.disabled_at
FROM agent_directory d
LEFT JOIN LATERAL (
    SELECT max(seen_at) AS seen,
           count(*) FILTER (WHERE seen_at >= :cutoff) AS live,
           array_agg(DISTINCT transport) FILTER (WHERE seen_at >= :cutoff) AS transports
    FROM agent_presence
    WHERE workspace_id = d.workspace_id AND token_id = ANY(d.token_ids)
) p ON TRUE
LEFT JOIN webhook_rules w
    ON w.workspace_id = d.workspace_id AND w.name = left('wake:' || d.agent_id, 128)
WHERE d.status <> 'retired' {where}
"""


def state_of(row: dict[str, Any]) -> str:
    if int(row.get("live") or 0) > 0:
        return "online"
    if row.get("wake_id") is None or not row.get("wake_active"):
        return "unwakeable"
    if row.get("disabled_at") is not None or int(row.get("consecutive_failures") or 0) > 0:
        return "degraded"
    return "offline"


def describe(row: dict[str, Any]) -> dict[str, Any]:
    """The presence block of a directory entry or a blackboard avatar."""
    state = state_of(row)
    seen = row.get("seen")
    wake = None
    if row.get("wake_id") is not None:
        wake = {
            "channel": "webhook",
            "active": bool(row.get("wake_active")),
            "failing": int(row.get("consecutive_failures") or 0) > 0,
            "dead": row.get("disabled_at") is not None,
        }
    return {
        "state": state,
        "online": state == "online",
        "connections": int(row.get("live") or 0),
        "transport": (sorted(row.get("transports") or []) or [None])[0],
        "last_seen_at": seen.isoformat() if seen else None,
        "wakeable": state in ("online", "offline"),
        "wake": wake,
        "heartbeat_every_s": TOUCH_EVERY_SECONDS,
        "offline_after_s": int(ONLINE_WINDOW.total_seconds()),
    }


async def rows(session: AsyncSession, workspace_id: Any = None,
               principal_ids: list[str] | None = None) -> list[dict[str, Any]]:
    where, params = "", {"cutoff": _now() - LIVE_WINDOW}
    if workspace_id is not None:
        where += " AND d.workspace_id = :ws"
        params["ws"] = workspace_id
    if principal_ids is not None:
        where += " AND d.principal_id = ANY(CAST(:ids AS text[]))"
        params["ids"] = list(principal_ids)
    result = await session.execute(text(_STATE_SQL.format(where=where)), params)
    return [dict(row) for row in result.mappings().all()]


async def for_principals(session: AsyncSession, workspace_id: Any,
                         principal_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not principal_ids:
        return {}
    return {row["principal_id"]: describe(row)
            for row in await rows(session, workspace_id, principal_ids)}


async def _reconcile(session: AsyncSession, workspace_id: Any = None) -> list[dict[str, Any]]:
    changed = []
    for row in await rows(session, workspace_id):
        state = state_of(row)
        previous = row.get("presence_state")
        if state == previous:
            continue
        # Compare-and-set: of two replicas (or a stream close racing the
        # loop) that see the same change, only one announces it.
        claimed = await session.execute(text(
            "UPDATE agent_directory SET presence_state = :s, presence_changed_at = NOW() "
            "WHERE principal_id = :p AND presence_state IS NOT DISTINCT FROM :prev"),
            {"s": state, "p": row["principal_id"], "prev": previous})
        if not claimed.rowcount:
            continue
        if previous is None and state != "online":
            # First sighting (a new entry, or the upgrade): record it quietly.
            # Coming online is announced either way.
            continue
        info = describe(row)
        await append_event(
            session, workspace_id=row["workspace_id"], agent="presence", event_type=EVENT,
            payload={
                "agent_id": row["agent_id"],
                "details": {"agent_id": row["agent_id"], "state": state,
                            "previous": previous or "unknown", "last_seen_at": info["last_seen_at"],
                            "wakeable": info["wakeable"]},
            })
        changed.append({"agent_id": row["agent_id"], "state": state,
                        "previous": previous or "unknown"})
    return changed


async def reconcile(workspace_id: Any = None) -> list[dict[str, Any]]:
    """Announce every presence change since the last pass. Never raises."""
    try:
        return await _unscoped(_reconcile, workspace_id)
    except Exception:  # noqa: BLE001 - presence bookkeeping never fails a request
        logger.warning("presence reconciliation failed", exc_info=True)
        return []


async def presence_loop() -> None:
    """Announce silent agents promptly (blueprint L2: within 90 s)."""
    while True:
        await asyncio.sleep(CHECK_EVERY_SECONDS)
        try:
            await _unscoped(_reconcile, None)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - background resilience
            logger.warning("presence check failed", exc_info=True)


async def heartbeat(principal: Principal, agent_ref: str) -> dict[str, Any]:
    """A loop heartbeat: online for the next 90 s, whatever the transport."""
    if principal.auth_kind.value != "agent" or not principal.token_cid:
        raise ApiError(403, "forbidden", "presence heartbeats come from an agent's own token")

    async def run(session: AsyncSession) -> None:
        await session.execute(text(
            "INSERT INTO agent_presence (workspace_id, token_id, agent_ref, instance, transport) "
            "VALUES (:ws, :t, :a, :i, 'heartbeat') ON CONFLICT (token_id, instance) DO UPDATE "
            "SET seen_at = NOW(), agent_ref = EXCLUDED.agent_ref"),
            {"ws": principal.workspace.id, "t": principal.token_cid, "a": agent_ref,
             "i": f"hb:{INSTANCE}"[:64]})

    await _unscoped(run)
    await reconcile(principal.workspace.id)
    return {
        "state": "online",
        "next_heartbeat_in_s": TOUCH_EVERY_SECONDS,
        "offline_after_s": int(ONLINE_WINDOW.total_seconds()),
        "note": "send one every 30 s from the loop that does the work; three missed "
                "heartbeats (90 s) and the board shows you offline, degraded or unwakeable",
    }


async def list_states(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    items = []
    for row in await rows(session, principal.workspace.id):
        items.append({"agent_id": row["agent_id"], "display_name": row["display_name"],
                      **describe(row)})
    counts = {state: sum(1 for item in items if item["state"] == state) for state in STATES}
    # Blueprint L6: every agent re-handshakes with a wake channel (target 100 %).
    with_wake = sum(1 for item in items if item["wake"] is not None)
    return {"items": sorted(items, key=lambda item: item["agent_id"]), "counts": counts,
            "wake_coverage": {"agents": len(items), "with_wake_channel": with_wake,
                              "rate": _rate(with_wake, len(items))}}


# ------------------------------------------------------------- observability

WINDOWS = {"1h": timedelta(hours=1), "24h": timedelta(days=1), "7d": timedelta(days=7)}
MAX_ADDRESSED = 20_000


def _since(window: str) -> datetime:
    if window not in WINDOWS:
        raise ApiError(422, "validation_error", "window is 1h, 24h or 7d",
                       details={"field": "window", "allowed": list(WINDOWS)})
    return _now() - WINDOWS[window]


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


async def delivery_report(session: AsyncSession, principal: Principal,
                          window: str = "24h") -> dict[str, Any]:
    """Delivery rates per agent (blueprint L2 / L6): of the events addressed to
    it, how many were pushed live (stream or wake webhook), how many wake
    attempts failed, and how many it has read (acked). Everything addressed is
    also in its inbox from the moment it committed."""
    from app.services.blackboard import _agents

    since = _since(window)
    agents = await _agents(session, principal)
    ws = principal.workspace.id
    events = (await session.execute(text(
        "SELECT seq, targets FROM workspace_events WHERE workspace_id = :ws "
        "AND created_at >= :since AND seq IS NOT NULL AND jsonb_array_length(targets) > 0 "
        "ORDER BY seq DESC LIMIT :cap"),
        {"ws": ws, "since": since, "cap": MAX_ADDRESSED})).mappings().all()
    pushes = (await session.execute(text(
        "SELECT event_seq, recipient, channel, status FROM poke_deliveries "
        "WHERE workspace_id = :ws AND at >= :since"), {"ws": ws, "since": since})
    ).mappings().all()
    acks = (await session.execute(text(
        "SELECT owner, max(to_seq) AS upto FROM inbox_acks WHERE workspace_id = :ws "
        "GROUP BY owner"), {"ws": ws})).mappings().all()

    live: set[tuple[int, str]] = set()
    failed: set[tuple[int, str]] = set()
    for row in pushes:
        agent = agents.resolve(row["recipient"])
        if agent is None:
            continue
        key = (int(row["event_seq"]), agent)
        (live if row["status"] == "delivered" else failed).add(key)
    read_upto: dict[str, int] = {}
    for row in acks:
        agent = agents.resolve(row["owner"])
        if agent is not None:
            read_upto[agent] = max(read_upto.get(agent, 0), int(row["upto"] or 0))

    stats: dict[str, dict[str, int]] = {
        entry["agent_id"]: {"addressed": 0, "delivered_live": 0, "wake_failed": 0, "read": 0}
        for entry in agents.entries}
    for row in events:
        seq = int(row["seq"])
        for agent in {agents.resolve(str(target)) for target in row["targets"] or []}:
            if agent is None:
                continue
            item = stats[agent]
            item["addressed"] += 1
            if (seq, agent) in live:
                item["delivered_live"] += 1
            elif (seq, agent) in failed:
                item["wake_failed"] += 1
            if read_upto.get(agent, 0) >= seq:
                item["read"] += 1

    states = await for_principals(session, ws, [entry["principal_id"] for entry in agents.entries])
    items = []
    for entry in agents.entries:
        item = stats[entry["agent_id"]]
        items.append({
            "agent_id": entry["agent_id"],
            "state": (states.get(entry["principal_id"]) or {}).get("state"),
            **item,
            "live_rate": _rate(item["delivered_live"], item["addressed"]),
            "read_rate": _rate(item["read"], item["addressed"]),
            "unread": item["addressed"] - item["read"],
        })
    total = {key: sum(item[key] for item in items)
             for key in ("addressed", "delivered_live", "wake_failed", "read")}
    return {
        "window": window,
        "since": since.isoformat(),
        "items": sorted(items, key=lambda item: (-item["addressed"], item["agent_id"])),
        "total": {**total, "live_rate": _rate(total["delivered_live"], total["addressed"]),
                  "read_rate": _rate(total["read"], total["addressed"])},
        "truncated": len(events) >= MAX_ADDRESSED,
        "note": "every addressed event is in the recipient's inbox from commit; live means "
                "pushed over an open stream or a successful wake webhook",
    }


async def history(session: AsyncSession, principal: Principal, *, agent: str | None = None,
                  window: str = "24h", limit: int = 200) -> dict[str, Any]:
    """Presence changes over a window (blueprint L6), newest first."""
    since = _since(window)
    params: dict[str, Any] = {"ws": principal.workspace.id, "since": since,
                              "lim": max(1, min(limit, 1000)), "type": "agent.presence_changed"}
    where = ""
    if agent:
        params["agent"] = agent.removeprefix("agent:")
        where = " AND payload->'details'->>'agent_id' = :agent"
    rows = (await session.execute(text(
        "SELECT id, payload->'details' AS details, created_at FROM workspace_events "
        f"WHERE workspace_id = :ws AND type = :type AND created_at >= :since{where} "
        "ORDER BY id DESC LIMIT :lim"), params)).mappings().all()
    return {"window": window, "items": [
        {"seq": row["id"], "at": row["created_at"].isoformat(), **(row["details"] or {})}
        for row in rows]}
