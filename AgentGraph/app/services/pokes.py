"""Realtime pokes (TASK-72A59BD9): presence, push, wake-up and receipts.

An agent that keeps ``GET /v1/inbox/stream`` open is online: addressed events
(assignments, mentions, reviews, handoffs) are pushed to it as they commit,
and each push is recorded. An agent that is offline is woken through its own
wake webhook (``register_agent(reach={"wake_url": ...})``), retried and marked
DEAD like every webhook, and the outcome is recorded too. Every poke then has
three receipts: sent (the event committed), delivered (inbox at once; stream
or webhook when they happened) and read (the ack that covered it, or the claim
of its task). Timestamps travel with each stage so a delay can be placed on
the platform or on the host.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.errors import ApiError, NotFound

logger = logging.getLogger("agentgraph.pokes")

INSTANCE = uuid.uuid4().hex[:16]  # this server process
ONLINE_WINDOW = timedelta(seconds=90)
# An agent that works through REST or MCP calls (no stream, no heartbeat) is
# online for this long after its last call: think time between calls is long.
API_WINDOW = timedelta(minutes=10)
TOUCH_EVERY_SECONDS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _unscoped(fn, *args: Any) -> Any:
    """Presence and receipts are written beside the request, never inside it."""
    from app.database import async_session_factory

    async with async_session_factory() as session:
        result = await fn(session, *args)
        await session.commit()
        return result


# ------------------------------------------------------------------ presence


async def presence_open(principal: Principal, agent_ref: str, transport: str = "sse") -> None:
    async def run(session: AsyncSession) -> None:
        await session.execute(text(
            "INSERT INTO agent_presence (workspace_id, token_id, agent_ref, instance, transport) "
            "VALUES (:ws, :t, :a, :i, :tr) ON CONFLICT (token_id, instance) DO UPDATE SET "
            "seen_at = NOW(), agent_ref = EXCLUDED.agent_ref"),
            {"ws": principal.workspace.id, "t": principal.token_cid, "a": agent_ref,
             "i": INSTANCE, "tr": transport})

    await _unscoped(run)
    await _announce(principal.workspace.id)


async def presence_touch(principal: Principal) -> None:
    async def run(session: AsyncSession) -> None:
        await session.execute(text(
            "UPDATE agent_presence SET seen_at = NOW() WHERE token_id = :t AND instance = :i"),
            {"t": principal.token_cid, "i": INSTANCE})

    await _unscoped(run)


async def presence_close(principal: Principal) -> None:
    """One stream closed. Another stream of the same token may still be open."""
    async def run(session: AsyncSession) -> None:
        await session.execute(text(
            "DELETE FROM agent_presence WHERE token_id = :t AND instance = :i"),
            {"t": principal.token_cid, "i": INSTANCE})

    try:
        await _unscoped(run)
    except Exception:  # noqa: BLE001 - the reaper sweeps what a crash leaves
        logger.warning("presence close failed", exc_info=True)
    await _announce(principal.workspace.id)


async def _announce(workspace_id: Any) -> None:
    from app.services import presence

    await presence.reconcile(workspace_id)


async def sweep_presence() -> int:
    """Delete stale rows; nerve rows survive at 90 s because their socket is newer.

    A nerve socket keeps its own row touched every few seconds and removes it
    on close, so a row that is old at 90 s is an orphan from a killed process:
    it is deleted like any other, and the agent's directory state follows.
    """
    async def run(session: AsyncSession) -> int:
        result = await session.execute(text(
            "DELETE FROM agent_presence WHERE (transport <> 'api' AND seen_at < :cutoff) "
            "OR seen_at < :api_cutoff"),
            {"cutoff": _now() - ONLINE_WINDOW, "api_cutoff": _now() - API_WINDOW})
        return result.rowcount or 0

    return await _unscoped(run)


async def presence_for_tokens(session: AsyncSession, workspace_id: Any,
                              token_ids: list[str]) -> dict[str, Any]:
    """Live connection state for a directory entry's tokens."""
    if not token_ids:
        return {"online": False, "connections": 0, "last_seen_at": None}
    row = (await session.execute(text(
        "SELECT count(*) FILTER (WHERE transport <> 'api' AND seen_at >= :cutoff) AS live, "
        "count(*) FILTER (WHERE transport = 'api' AND seen_at >= :api_cutoff) AS api, "
        "max(seen_at) AS seen "
        "FROM agent_presence WHERE workspace_id = :ws AND token_id = ANY(CAST(:ids AS text[]))"),
        {"ws": workspace_id, "ids": list(token_ids), "cutoff": _now() - ONLINE_WINDOW,
         "api_cutoff": _now() - API_WINDOW})
    ).mappings().one()
    return {"online": bool(row["live"] or row["api"]), "connections": int(row["live"] or 0),
            "last_seen_at": row["seen"].isoformat() if row["seen"] else None,
            "transport": "sse" if row["live"] else ("api" if row["api"] else None)}


async def online_refs(session: AsyncSession, workspace_id: Any) -> set[str]:
    """agent:<id> / agent:<prn> / labels of every agent with a live stream.

    Only a stream counts: it is what a poke is pushed over. A loop heartbeat
    without a stream must not hold back the agent's wake webhook."""
    rows = (await session.execute(text(
        "SELECT p.agent_ref, d.agent_id, d.principal_id, t.agent_signature "
        "FROM agent_presence p LEFT JOIN agent_directory d ON p.token_id = ANY(d.token_ids) "
        "LEFT JOIN agent_tokens t ON t.canonical_id = p.token_id "
        "WHERE p.workspace_id = :ws AND p.seen_at >= :cutoff "
        "AND p.transport NOT IN ('api', 'heartbeat')"),
        {"ws": workspace_id, "cutoff": _now() - ONLINE_WINDOW})).mappings().all()
    refs: set[str] = set()
    for row in rows:
        for name in (row["agent_ref"], row["agent_id"], row["principal_id"],
                     row["agent_signature"]):
            if name:
                refs.update({name if ":" in name else f"agent:{name}", name})
    return refs


# ------------------------------------------------------------------ receipts


async def record_delivery(workspace_id: Any, event_seq: int, recipient: str, channel: str,
                          status: str = "delivered", detail: str | None = None) -> None:
    async def run(session: AsyncSession) -> None:
        await session.execute(text(
            "INSERT INTO poke_deliveries (workspace_id, event_seq, recipient, channel, status, "
            "detail) VALUES (:ws, :seq, :r, :c, :s, :d) ON CONFLICT "
            "(workspace_id, event_seq, recipient, channel) DO UPDATE SET status = "
            "EXCLUDED.status, detail = EXCLUDED.detail, at = NOW()"),
            {"ws": workspace_id, "seq": int(event_seq), "r": recipient[:256], "c": channel,
             "s": status, "d": (detail or None) and detail[:500]})

    try:
        await _unscoped(run)
    except Exception:  # noqa: BLE001 - a receipt never fails the delivery it records
        logger.warning("poke receipt not recorded", exc_info=True)


async def record_ack(session: AsyncSession, workspace_id: Any, owner: str, from_seq: int,
                     to_seq: int) -> None:
    if to_seq <= from_seq:
        return
    await session.execute(text(
        "INSERT INTO inbox_acks (workspace_id, owner, from_seq, to_seq) "
        "VALUES (:ws, :o, :f, :t)"),
        {"ws": workspace_id, "o": owner, "f": int(from_seq), "t": int(to_seq)})


async def _owner_of(session: AsyncSession, workspace_id: Any, target: str) -> str:
    """The watermark owner that answers for a target ref (see inbox._who)."""
    kind, _, ident = target.partition(":")
    if kind == "human" or target == "human":
        return "human"
    from app.services.collab import _directory_entry

    entry = await _directory_entry(session, workspace_id, ident or target)
    if entry is not None:
        return f"agent:{entry['principal_id']}"
    return f"agent:{ident or target}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def receipts(session: AsyncSession, principal: Principal, *,
                   event_seq: int | None = None, task_key: str | None = None) -> dict[str, Any]:
    """Sent / delivered / read for each recipient of one poke, with timestamps.

    Pass event_seq (from the inbox or the stream) or task_key (its newest
    addressed event, e.g. the assignment).
    """
    params: dict[str, Any] = {"ws": principal.workspace.id}
    if event_seq is not None:
        where, params["seq"] = "seq = :seq", int(event_seq)
    elif task_key:
        where, params["task"] = ("task_key = :task AND jsonb_array_length(targets) > 0 "
                                 "ORDER BY seq DESC"), task_key
    else:
        raise ApiError(422, "validation_error", "pass event_seq or task_key",
                       details={"fields": ["event_seq", "task_key"]})
    event = (await session.execute(text(
        "SELECT seq, event_id, type, task_key, doc_key, actor, namespace, targets, payload, "
        f"created_at FROM workspace_events WHERE workspace_id = :ws AND {where} LIMIT 1"),
        params)).mappings().one_or_none()
    if event is None:
        raise NotFound("poke", "no addressed event matches")
    if not principal.is_commander and "*" not in principal.allowed_namespaces and (
            event["namespace"] not in principal.allowed_namespaces):
        raise NotFound("poke", "no addressed event matches")
    pushes = (await session.execute(text(
        "SELECT recipient, channel, status, detail, at FROM poke_deliveries "
        "WHERE workspace_id = :ws AND event_seq = :seq"),
        {"ws": principal.workspace.id, "seq": event["seq"]})).mappings().all()
    claimed = None
    if event["task_key"]:
        claimed = (await session.execute(text(
            "SELECT holder, acquired_at FROM leases WHERE workspace_id = :ws "
            "AND resource_kind = 'task' AND acquired_at >= :since AND resource_ref = "
            "(SELECT canonical_id FROM collab_tasks WHERE workspace_id = :ws AND task_key = :k) "
            "ORDER BY acquired_at LIMIT 1"),
            {"ws": principal.workspace.id, "since": event["created_at"],
             "k": event["task_key"]})).mappings().one_or_none()
    sent_at = (event["payload"] or {}).get("timestamp")
    recipients = []
    for target in sorted({str(item) for item in (event["targets"] or [])}):
        owner = await _owner_of(session, principal.workspace.id, target)
        read = (await session.execute(text(
            "SELECT acked_at FROM inbox_acks WHERE workspace_id = :ws AND owner = :o "
            "AND to_seq >= :seq ORDER BY acked_at LIMIT 1"),
            {"ws": principal.workspace.id, "o": owner, "seq": event["seq"]})).scalar()
        channels = [{"channel": row["channel"], "status": row["status"],
                     "detail": row["detail"], "at": _iso(row["at"])}
                    for row in pushes if row["recipient"] in (target, owner)]
        live = [item for item in channels if item["status"] == "delivered"]
        recipients.append({
            "recipient": target,
            "sent": {"at": sent_at or _iso(event["created_at"])},
            # In the inbox from the moment the event committed: surfaced (2026-10-24 R-4).
            "delivered": {"at": min([item["at"] for item in live] or [_iso(event["created_at"])]),
                          "via": [item["channel"] for item in live] or ["inbox"],
                          "inbox_at": _iso(event["created_at"])},
            "channels": channels,
            "read": {"at": _iso(read), "via": "ack"} if read else None,
            "claimed": ({"at": _iso(claimed["acquired_at"]), "by": claimed["holder"]}
                        if claimed else None),
            "surfaced": True,
        })
    return {"event_seq": event["seq"], "event_id": event["event_id"], "type": event["type"],
            "task_key": event["task_key"], "doc_key": event["doc_key"], "actor": event["actor"],
            "stored_at": _iso(event["created_at"]), "recipients": recipients}


# ------------------------------------------------------------------ wake-up


async def set_wake_url(session: AsyncSession, principal: Principal,
                       url: str | None) -> dict[str, Any]:
    """The URL Veynod POSTs (signed) when a poke reaches this agent while it is
    offline (R-3). Delivery, retries (30 s / 120 s / 600 s) and DEAD are those
    of every webhook; each outcome lands in the poke's receipts. url=None
    removes it. The signing secret is returned once."""
    from app.models import WebhookRule
    from app.services.outbound import new_secret, validate_target

    if principal.auth_kind.value != "agent" or not principal.token_cid:
        raise ApiError(403, "forbidden", "set_wake_url is called by the agent's own token")
    entry = (await session.execute(text(
        "SELECT agent_id, principal_id, signatures FROM agent_directory "
        "WHERE workspace_id = :ws AND :t = ANY(token_ids) LIMIT 1"),
        {"ws": principal.workspace.id, "t": principal.token_cid})).mappings().one_or_none()
    if entry is None:
        raise ApiError(409, "agent_not_registered", "register_agent first: the wake URL belongs "
                       "to your directory entry", details={"register_with": "register_agent"})
    name = f"wake:{entry['agent_id']}"[:128]
    from sqlalchemy import select

    rule = await session.scalar(select(WebhookRule).where(
        WebhookRule.workspace_id == principal.workspace.id, WebhookRule.name == name))
    if url is None:
        if rule is not None:
            await session.delete(rule)
            await session.commit()
            await _announce(principal.workspace.id)
        return {"wake_url": None, "removed": rule is not None}
    await validate_target(url)
    refs = [f"agent:{entry['agent_id']}", f"agent:{entry['principal_id']}",
            principal.agent_signature, f"agent:{principal.agent_signature}"]
    refs += [f"agent:{item}" for item in entry["signatures"] or []]
    secret = new_secret()
    latest = await session.scalar(text(
        "SELECT COALESCE(max(id), 0) FROM workspace_events WHERE workspace_id = :ws"),
        {"ws": principal.workspace.id})
    if rule is None:
        rule = WebhookRule(workspace_id=principal.workspace.id, name=name, url=url,
                           secret=secret, event_types=["*"], tables=[], namespaces=[],
                           last_event_id=int(latest or 0), created_by=principal.token_cid)
        session.add(rule)
    rule.url, rule.secret, rule.active = url, secret, True
    rule.targets, rule.wake_only = list(dict.fromkeys(refs)), True
    rule.consecutive_failures, rule.failing_since, rule.disabled_at = 0, None, None
    await session.commit()
    await _announce(principal.workspace.id)
    return {"wake_url": url, "webhook_id": str(rule.id), "secret": secret,
            "signature": "X-Veynod-Signature = hex HMAC-SHA256(secret, "
                         "f\"{X-Veynod-Timestamp}.\" + body)",
            "fires": "only while you have no open inbox stream",
            "retries": "30 s, 120 s, 600 s, then DEAD (see get_poke_receipts)"}
