"""MCP connections: one client installation, its credential, and their upkeep.

A connection is an installation of a client on a machine (docs/
MCP_CONNECTION_LIFECYCLE.md): one per workspace and ``client_instance_id``, the
id the CLI makes once and keeps in ``~/.veynod/connection.json``. Its
credentials are ``agent_tokens`` rows that point at it.

* Approving a CLI sign-in that carries the id reuses the connection's
  credential: same token row, same ``agent_signature``, a new secret (the old
  one keeps the usual grace window), the grant the owner just approved and a
  fresh expiry. Running ``npx veynod`` twice no longer makes ``cursor-2``.
* A token from before connections, with the signature asked for and no
  connection of its own, is adopted the same way.
* A credential issued through the CLI expires. The expiry slides while the
  credential is in use, so a computer that works every day never loses its key;
  one whose client configuration was deleted stops being used, expires and is
  revoked by the reaper. Nothing is deleted: revoked rows are the audit trail.
* A connection nobody used for a while is ``dormant`` and later revoked.

A CLI that sends no ``client_instance_id`` keeps the old behaviour.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import generate_agent_key, hash_api_key
from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError, Forbidden, NotFound
from app.models import AgentRole, AgentToken, Workspace

logger = logging.getLogger("agentgraph.connections")
settings = get_settings()

#: What a credential minted by the CLI device flow says about itself.
CLI_PURPOSE_PREFIX = "npx veynod init"
EXPIRED = "expired"
DORMANT = "connection_dormant"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def credential_expiry(now: datetime | None = None) -> datetime | None:
    """When a credential issued through the CLI expires (None: it does not)."""
    days = settings.cli_credential_ttl_days
    return (now or _now()) + timedelta(days=days) if days else None


def clean_instance_id(value: str | None) -> str | None:
    """A client instance id as stored, or None when the client sent none."""
    value = (value or "").strip()
    return value[:64] or None


async def upsert(scoped: AsyncSession, workspace: Workspace, instance_id: str, *,
                 label: str = "", kinds: list[str] | None = None) -> dict[str, Any]:
    """The connection of this installation in this workspace, created if new.

    A reconnect brings a dormant connection back; a revoked one stays revoked
    and the caller issues nothing under it (the installation gets a new one by
    running with ``--fresh``).
    """
    kind = (kinds or ["other"])[0] if kinds else "other"
    row = (await scoped.execute(text(
        "INSERT INTO mcp_connections (workspace_id, client_instance_id, client_kind, label, "
        "last_seen_at) VALUES (:ws, :cid, :kind, :label, NOW()) "
        "ON CONFLICT (workspace_id, client_instance_id) DO UPDATE SET "
        "last_seen_at = NOW(), "
        "label = CASE WHEN EXCLUDED.label <> '' THEN EXCLUDED.label "
        "ELSE mcp_connections.label END, "
        "client_kind = EXCLUDED.client_kind, "
        "status = CASE WHEN mcp_connections.status = 'dormant' THEN 'active' "
        "ELSE mcp_connections.status END "
        "RETURNING *, (xmax = 0) AS created"),
        {"ws": workspace.id, "cid": instance_id, "kind": kind[:32], "label": label[:64]})
    ).mappings().one()
    return dict(row)


async def credential_for(scoped: AsyncSession, workspace: Workspace, connection: dict[str, Any],
                         signature: str) -> tuple[AgentToken | None, str]:
    """The token to reuse for ``signature`` on this connection, and how.

    ``rotated``: the connection's own live credential with that name.
    ``adopted``: a live token from before connections with that name and no
    connection. ``("", None)`` otherwise, which includes a name that belongs to
    another connection: that one is never taken over.
    """
    rows = (await scoped.execute(
        select(AgentToken).where(
            AgentToken.workspace_id == workspace.id,
            AgentToken.agent_signature == signature,
            AgentToken.depth == 1,
        ).with_for_update())).scalars().all()
    for token in rows:
        if token.revoked_at is not None or not token.is_active:
            continue
        if token.connection_id == connection["id"]:
            return token, "rotated"
        if token.connection_id is None:
            return token, "adopted"
    return None, ""


async def reuse(scoped: AsyncSession, workspace: Workspace, connection: dict[str, Any],
                token: AgentToken, *, role: str, namespaces: list[str],
                purpose: str) -> str:
    """Rotate a credential in place for a reconnect; the raw new secret.

    The row, its ``agent_signature``, its agent id and everything attached to
    them (onboarding, directory entry, assignments) stay. The previous secret
    works for the usual grace window, so a session that is running switches
    over without a 401.
    """
    from app.services.lineage import snapshot_token
    from app.services.token_tree import ROTATION_GRACE

    raw = generate_agent_key()
    now = _now()
    token.previous_key_hash = token.api_key_hash
    token.previous_key_valid_until = now + ROTATION_GRACE
    token.api_key_hash = hash_api_key(raw)
    token.connection_id = connection["id"]
    token.expires_at = credential_expiry(now)
    token.expiring_notified_at = None
    changed = (token.role_scope.value != role
               or list(token.allowed_namespaces or ["*"]) != list(namespaces))
    token.role_scope = AgentRole(role)
    token.allowed_namespaces = namespaces
    token.purpose = purpose[:200]
    await scoped.flush()
    if changed:
        # The owner approved a different grant: later writes carry the new one.
        await snapshot_token(scoped, workspace, token)
    return raw


async def link(scoped: AsyncSession, token_id: Any, connection: dict[str, Any] | None) -> None:
    """Give a freshly minted CLI credential its expiry and, if any, its connection."""
    await scoped.execute(text(
        "UPDATE agent_tokens SET connection_id = :c, expires_at = COALESCE(:x, expires_at) "
        "WHERE id = :id"),
        {"c": connection["id"] if connection else None, "x": credential_expiry(),
         "id": uuid.UUID(str(token_id))})


# ------------------------------------------------------------------ reading


def _out(row: dict[str, Any]) -> dict[str, Any]:
    stamp = lambda value: value.isoformat() if value else None  # noqa: E731
    return {
        "id": str(row["id"]),
        "client_instance_id": row["client_instance_id"],
        "client_kind": row["client_kind"],
        "label": row["label"],
        "status": row["status"],
        "created_at": stamp(row["created_at"]),
        "last_seen_at": stamp(row["last_seen_at"]),
        "revoked_at": stamp(row["revoked_at"]),
        "revoke_reason": row.get("revoke_reason"),
        "active_credentials": int(row.get("active_credentials") or 0),
        "credentials": row.get("credentials") or [],
    }


def _owner(principal: Any) -> None:
    if principal.human_role not in ("OWNER", "ADMIN"):
        raise Forbidden("an owner or admin of this workspace manages connections")


async def list_connections(scoped: AsyncSession, principal: Any, *,
                           include_inactive: bool = False) -> dict[str, Any]:
    """The workspace's client installations; active (and dormant) by default.

    Each carries its credentials, the live one first: a connection is what the
    console lists, credentials are its history.
    """
    _owner(principal)
    rows = (await scoped.execute(text(
        "SELECT * FROM mcp_connections WHERE workspace_id = :ws "
        + ("" if include_inactive else "AND status <> 'revoked' ")
        + "ORDER BY status = 'revoked', COALESCE(last_seen_at, created_at) DESC LIMIT 500"),
        {"ws": principal.workspace.id})).mappings().all()
    items = []
    for row in rows:
        tokens = (await scoped.execute(text(
            "SELECT canonical_id, agent_signature, role_scope::text AS role_scope, "
            "allowed_namespaces, is_active, expires_at, revoked_at, revoke_reason, created_at "
            "FROM agent_tokens WHERE connection_id = :c "
            "ORDER BY revoked_at IS NOT NULL, created_at DESC"), {"c": row["id"]})
        ).mappings().all()
        credentials = [{
            "token_id": token["canonical_id"], "agent_signature": token["agent_signature"],
            "role_scope": token["role_scope"],
            "allowed_namespaces": list(token["allowed_namespaces"] or ["*"]),
            "active": bool(token["is_active"]) and token["revoked_at"] is None,
            "expires_at": token["expires_at"].isoformat() if token["expires_at"] else None,
            "revoked_at": token["revoked_at"].isoformat() if token["revoked_at"] else None,
            "revoke_reason": token["revoke_reason"],
        } for token in tokens if include_inactive or token["revoked_at"] is None]
        items.append(_out({**dict(row), "credentials": credentials,
                           "active_credentials": sum(1 for item in credentials
                                                     if item["active"])}))
    counts = await counts_for(scoped, principal.workspace.id)
    return {"items": items, "counts": counts}


async def counts_for(session: AsyncSession, workspace_id: Any) -> dict[str, int]:
    """How many connections a workspace has by status (all an operator sees)."""
    rows = (await session.execute(text(
        "SELECT status, count(*) FROM mcp_connections WHERE workspace_id = :ws GROUP BY status"),
        {"ws": workspace_id})).all()
    counts = {"active": 0, "dormant": 0, "revoked": 0}
    counts.update({status: int(count) for status, count in rows})
    return counts


async def revoke_connection(scoped: AsyncSession, principal: Any, connection_id: str, *,
                            reason: str = "revoked in the console") -> dict[str, Any]:
    """Revoke a connection and every live credential of it, at once.

    Current and previous secrets stop authenticating (a revoked token is
    refused whatever secret is presented). Rows, audit and grant snapshots stay.
    """
    _owner(principal)
    try:
        cid = uuid.UUID(str(connection_id))
    except ValueError:
        raise NotFound("connection") from None
    row = (await scoped.execute(text(
        "SELECT * FROM mcp_connections WHERE workspace_id = :ws AND id = :id FOR UPDATE"),
        {"ws": principal.workspace.id, "id": cid})).mappings().one_or_none()
    if row is None:
        raise NotFound("connection")
    revoked = await _revoke_credentials(scoped, principal.workspace.id, cid, reason)
    await scoped.execute(text(
        "UPDATE mcp_connections SET status = 'revoked', revoked_at = COALESCE(revoked_at, NOW()), "
        "revoke_reason = COALESCE(revoke_reason, :r) WHERE id = :id"),
        {"r": reason[:200], "id": cid})
    from app.services.common import append_event

    await append_event(
        scoped, workspace_id=principal.workspace.id,
        agent=getattr(principal, "agent_signature", None) or "console",
        event_type="CONNECTION_REVOKED",
        payload={"connection_id": str(cid), "label": row["label"], "reason": reason[:200],
                 "credentials": revoked})
    await scoped.commit()
    return {"connection_id": str(cid), "status": "revoked", "credentials_revoked": revoked}


async def _revoke_credentials(session: AsyncSession, workspace_id: Any, connection_id: Any,
                              reason: str) -> list[str]:
    """Revoke the live credentials of a connection (and what they delegated)."""
    tokens = (await session.execute(
        select(AgentToken).where(AgentToken.workspace_id == workspace_id,
                                 AgentToken.connection_id == connection_id,
                                 AgentToken.revoked_at.is_(None)))).scalars().all()
    return await _revoke_tokens(session, workspace_id, list(tokens), reason)


async def _revoke_tokens(session: AsyncSession, workspace_id: Any, tokens: list[AgentToken],
                         reason: str) -> list[str]:
    """Mark tokens and their subtrees revoked and release what they hold."""
    from app.services.leases import revoke_holder

    now = _now()
    done: list[str] = []
    for target in tokens:
        subtree = (await session.execute(
            select(AgentToken).where(AgentToken.workspace_id == workspace_id,
                                     AgentToken.lineage.any(target.canonical_id),
                                     AgentToken.revoked_at.is_(None))
            .order_by(AgentToken.depth))).scalars().all()
        if target not in subtree:
            subtree = [target, *subtree]
        for token in subtree:
            token.revoked_at = now
            token.is_active = False
            token.revoke_reason = reason
            token.revoked_ancestor = target.canonical_id
            await session.flush()
            await revoke_holder(session, workspace_id, token, reason="holder_revoked")
        done.append(target.canonical_id)
    return done


# ------------------------------------------------------------------ upkeep

_last_upkeep: datetime | None = None
# When a CLI credential or its agent was last heard from: the agent's directory
# entry, the computer's runner heartbeat, or its connection's last reconnect.
_LAST_USED = (
    "GREATEST(COALESCE(t.last_seen_at, t.created_at), "
    "COALESCE((SELECT max(d.last_seen_at) FROM agent_directory d "
    "  WHERE d.workspace_id = t.workspace_id AND t.canonical_id = ANY(d.token_ids)), "
    "  t.created_at), "
    "COALESCE((SELECT max(h.last_seen_at) FROM runner_hosts h "
    "  WHERE h.workspace_id = t.workspace_id AND h.token_id = t.canonical_id), t.created_at))"
)
_CLI_CREDENTIAL = (f"t.depth = 1 AND (t.connection_id IS NOT NULL "
                   f"OR t.purpose LIKE '{CLI_PURPOSE_PREFIX}%')")


async def upkeep(*, force: bool = False) -> dict[str, int]:
    """One cleanup pass (the reaper calls it; at most every ten minutes).

    1. A CLI credential still in use has its expiry moved forward.
    2. One that expired is marked revoked (``revoke_reason = 'expired'``). It
       already stopped authenticating at its expiry; this makes the list say so.
    3. A connection none of whose credentials was used for
       ``connection_dormant_days`` goes dormant, and one unused for
       ``connection_revoke_days`` is revoked with its credentials.

    Nothing is deleted.
    """
    global _last_upkeep
    now = _now()
    if not force and _last_upkeep is not None and now - _last_upkeep < timedelta(minutes=10):
        return {}
    _last_upkeep = now
    ttl = settings.cli_credential_ttl_days
    counts = {"extended": 0, "expired": 0, "dormant": 0, "revoked": 0, "woken": 0}
    async with async_session_factory() as session:
        if ttl:
            # In use within the last week and inside the second half of its life.
            counts["extended"] = (await session.execute(text(
                f"UPDATE agent_tokens t SET expires_at = NOW() + make_interval(days => :ttl), "
                f"expiring_notified_at = NULL WHERE {_CLI_CREDENTIAL} "
                "AND t.revoked_at IS NULL AND t.is_active AND t.expires_at IS NOT NULL "
                "AND t.expires_at > NOW() "
                "AND t.expires_at < NOW() + make_interval(days => :half) "
                f"AND {_LAST_USED} > NOW() - INTERVAL '7 days'"),
                {"ttl": ttl, "half": max(1, ttl // 2)})).rowcount or 0
        expired = (await session.execute(
            select(AgentToken).where(
                AgentToken.depth == 1, AgentToken.revoked_at.is_(None),
                AgentToken.expires_at.is_not(None), AgentToken.expires_at < now,
                (AgentToken.connection_id.is_not(None))
                | (AgentToken.purpose.like(f"{CLI_PURPOSE_PREFIX}%")),
            ).limit(500))).scalars().all()
        by_workspace: dict[Any, list[AgentToken]] = {}
        for token in expired:
            by_workspace.setdefault(token.workspace_id, []).append(token)
        for workspace_id, tokens in by_workspace.items():
            counts["expired"] += len(await _revoke_tokens(session, workspace_id, tokens, EXPIRED))
        # A connection is as recently used as its most recently used credential.
        used = (
            "GREATEST(COALESCE(c.last_seen_at, c.created_at), COALESCE((SELECT max("
            + _LAST_USED + ") FROM agent_tokens t WHERE t.connection_id = c.id), c.created_at))"
        )
        counts["woken"] = (await session.execute(text(
            f"UPDATE mcp_connections c SET status = 'active' WHERE c.status = 'dormant' "
            f"AND {used} > NOW() - make_interval(days => :d)"),
            {"d": settings.connection_dormant_days})).rowcount or 0
        counts["dormant"] = (await session.execute(text(
            f"UPDATE mcp_connections c SET status = 'dormant' WHERE c.status = 'active' "
            f"AND {used} < NOW() - make_interval(days => :d)"),
            {"d": settings.connection_dormant_days})).rowcount or 0
        gone = (await session.execute(text(
            f"SELECT c.id, c.workspace_id FROM mcp_connections c WHERE c.status = 'dormant' "
            f"AND {used} < NOW() - make_interval(days => :d) LIMIT 500"),
            {"d": settings.connection_revoke_days})).all()
        for connection_id, workspace_id in gone:
            await _revoke_credentials(session, workspace_id, connection_id, DORMANT)
            await session.execute(text(
                "UPDATE mcp_connections SET status = 'revoked', revoked_at = NOW(), "
                "revoke_reason = :r WHERE id = :id"), {"r": DORMANT, "id": connection_id})
            counts["revoked"] += 1
        await session.commit()
    if any(counts.values()):
        logger.info("connection upkeep: %s", counts)
    return counts


__all__ = [
    "ApiError",
    "clean_instance_id",
    "counts_for",
    "credential_expiry",
    "credential_for",
    "link",
    "list_connections",
    "reuse",
    "revoke_connection",
    "upkeep",
    "upsert",
]
