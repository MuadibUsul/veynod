"""Pokes ride along (2026-10-24): every agent tool result says what is new.

An agent that is working calls Veynod anyway, so the cheapest wake-up is to
put new inbox items on the next result it gets: a small ``inbox`` block with
the unacked count and the newest few envelopes (keys and statuses, never
content). An item is shown once per process; the count stays until the agent
acks with ``ack_inbox``. Agents opt out with ``reach.inbox_hints=false`` in
register_agent; operators with ``INBOX_HINTS_ENABLED=false``.

Hints never fail or slow down the call they follow: a short per-token
throttle bounds the extra query, and every error is swallowed.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import text

from app.auth import Principal
from app.config import get_settings

logger = logging.getLogger("agentgraph.inbox_hints")
settings = get_settings()

# Tools that already show the inbox, or are how an agent answers it.
SKIP_TOOLS = frozenset({"get_orientation", "list_inbox", "ack_inbox", "poll_events",
                        "get_digest"})
NEWEST = 3
THROTTLE_SECONDS = 5.0
_CACHE_MAX = 10_000

_surfaced: dict[str, int] = {}   # token -> highest seq already shown
_checked: dict[str, float] = {}  # token -> monotonic time of the last check


def _remember(cache: dict, key: str, value: Any) -> None:
    if len(cache) >= _CACHE_MAX:
        cache.clear()
    cache[key] = value


def reset() -> None:
    """Forget what was shown (tests)."""
    _surfaced.clear()
    _checked.clear()


async def hint_for(principal: Principal | None, tool: str | None) -> dict[str, Any] | None:
    if (not settings.inbox_hints_enabled or principal is None or principal.is_human
            or not principal.token_cid or tool in SKIP_TOOLS):
        return None
    key = principal.token_cid
    now = time.monotonic()
    if now - _checked.get(key, 0.0) < THROTTLE_SECONDS:
        return None
    _remember(_checked, key, now)
    try:
        return await _hint(principal, key)
    except Exception:  # noqa: BLE001 - a hint must never fail the call it rides on
        logger.warning("inbox hint skipped", exc_info=True)
        return None


async def _hint(principal: Principal, key: str) -> dict[str, Any] | None:
    from app.database import async_session_factory
    from app.services import inbox
    from app.tenant import activate_tenant

    async with async_session_factory() as session:
        await activate_tenant(session, principal.workspace.id, principal.tenant_scope)
        opted_out = await session.scalar(text(
            "SELECT reach->>'inbox_hints' = 'false' FROM agent_directory "
            "WHERE :t = ANY(token_ids) LIMIT 1"), {"t": key})
        if opted_out:
            return None
        owner, names = await inbox._who(session, principal, False)
        acked = await inbox._acked(session, principal, owner)
        shown = max(acked, _surfaced.get(key, 0))
        # What the agent just did itself is not news (its own claim, its own write).
        mine = ("workspace_id = :ws AND targets ?| CAST(:me AS text[]) "
                "AND seq IS NOT NULL AND actor IS DISTINCT FROM :self")
        params = {"ws": principal.workspace.id, "me": names, "acked": acked, "shown": shown,
                  "self": principal.agent_signature}
        counts = (await session.execute(text(
            f"SELECT count(*) FILTER (WHERE seq > :acked) AS unacked, "
            f"count(*) FILTER (WHERE seq > :shown) AS fresh, max(seq) AS newest "
            f"FROM workspace_events WHERE {mine} AND seq > :acked"), params)).mappings().one()
        if not counts["fresh"]:
            return None
        rows = (await session.execute(text(
            "SELECT seq, event_id, type, source_table, task_key, doc_key, actor, version, "
            f"refs, payload, created_at FROM workspace_events WHERE {mine} AND seq > :shown "
            "ORDER BY seq DESC LIMIT :n"), {**params, "n": NEWEST})).mappings().all()
    _remember(_surfaced, key, int(counts["newest"]))
    return {
        "unacked_count": int(counts["unacked"]),
        "new": [_brief(inbox.envelope(row)) for row in rows],
        "more": max(0, int(counts["fresh"]) - len(rows)),
        "next_call": {"tool": "list_inbox", "then": "ack_inbox(up_to_event_seq)"},
    }


def _brief(item: dict[str, Any]) -> dict[str, Any]:
    keep = ("event_seq", "kind", "task_key", "doc_key", "actor", "to_status", "note",
            "created_at")
    return {name: item[name] for name in keep if item.get(name) is not None}
