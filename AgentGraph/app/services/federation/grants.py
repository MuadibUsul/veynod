"""Connections between workspaces: the grant state machine (F-03a/b/c).

Contract: docs/federation/ICD_I_GRANT.md. Each side keeps its own copy of a
connection (``federation_grants``) and an append-only history
(``federation_grant_history``); the two sides only talk through signed
envelopes (``grant.request`` / ``grant.decision`` / ``grant.revoke``) carried
by a Transport (ADR-006: same deployment first, behind an interface).

Two-sided consent: the requesting owner consents by sending the request, the
receiving owner by approving it; a connection is ACTIVE on a side only once
that side's own person has agreed (GRANT-INV-2).

P1 simplifications (documented in ROUND_2_STATUS.md):
  * only an owner-level person can request a connection;
  * change_scopes only narrows; widening means a new connection;
  * renew is not offered: an expired connection is re-requested;
  * freeze and block are local: the peer simply gets FED_E_GRANT_INACTIVE.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.services.federation import identity, policy
from app.services.federation.canonical import sha256_hex
from app.services.federation.envelope import Verified, parse_time, verify_envelope
from app.services.federation.errors import FederationError
from app.services.federation.ids import new_connection_id, parse_federation_id, require_type

SCOPES = ("chat", "delegate", "presence")
LIVE_STATES = ("REQUESTED", "ACTIVE", "EXPIRED", "SUSPENDED")
FENCE_ACTIONS = ("freeze", "revoke", "block")
DEFAULT_TTL_DAYS = 90  # P1 placeholder (ICD_I_GRANT §2)
MAX_TTL_DAYS = 365


@dataclass(frozen=True)
class Actor:
    """Who is acting on this side. Built by the route layer from the principal."""

    kind: str  # "user" | "agent"
    id: str
    is_owner: bool = False

    @property
    def ref(self) -> str:
        return f"{self.kind}:{self.id}"


class Transport(Protocol):
    async def deliver(self, envelope: dict[str, Any]) -> dict[str, Any]: ...


class LocalTransport:
    """Same-deployment delivery (ADR-006): hand the signed envelope to the
    hosted workspace named by its audience. The receiver verifies it exactly as
    it would one that arrived over a network."""

    async def deliver(self, envelope: dict[str, Any]) -> dict[str, Any]:
        audience = envelope.get("audience")
        async with async_session_factory() as platform:
            workspace_id = await platform.scalar(
                text(
                    "SELECT workspace_id FROM federation_local_map "
                    "WHERE federation_id = :f AND id_type = 'workspace'"
                ),
                {"f": audience},
            )
        if workspace_id is None:
            raise FederationError("FED_E_IDENTITY_UNKNOWN", "no such workspace on this deployment")
        kind = str(envelope.get("type", ""))
        if kind.startswith("delegate."):
            from app.services.federation import delegations

            return await delegations.receive(str(workspace_id), envelope)
        if kind in ("chat.message", "delivery.receipt"):
            from app.services.federation import messaging

            return await messaging.receive(str(workspace_id), envelope)
        return await receive(str(workspace_id), envelope)


_transport: Transport = LocalTransport()


def set_transport(transport: Transport) -> None:
    """Tests and a future network transport plug in here."""
    global _transport
    _transport = transport


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check_scopes(scopes: list[str] | tuple[str, ...]) -> list[str]:
    unknown = sorted(set(scopes) - set(SCOPES))
    if unknown:
        raise FederationError("FED_E_SCOPE_UNKNOWN", "unknown scope", scopes=unknown)
    return sorted(set(scopes))


def _require_owner(actor: Actor) -> None:
    if actor.kind != "user" or not actor.is_owner:
        raise FederationError(
            "FED_E_APPROVAL_NOT_HUMAN",
            "only an owner-level person of this workspace can do this",
            actor_kind=actor.kind,
        )


def _require_person(actor: Actor) -> None:
    if actor.kind != "user":
        raise FederationError("FED_E_APPROVAL_NOT_HUMAN", "only a person can do this")


async def _local_fid(platform: AsyncSession, workspace_id: str) -> str:
    fid = await platform.scalar(
        text(
            "SELECT federation_id FROM federation_local_map "
            "WHERE id_type = 'workspace' AND workspace_id = CAST(:w AS uuid)"
        ),
        {"w": str(workspace_id)},
    )
    if fid is None:
        raise FederationError(
            "FED_E_IDENTITY_UNKNOWN", "federation is not enabled for this workspace"
        )
    return fid


def _out(row: dict[str, Any]) -> dict[str, Any]:
    """The public view of one side's connection: federation ids only."""
    return {
        "connection_id": row["connection_id"],
        "local_workspace": row["local_workspace_fid"],
        "peer_workspace": row["peer_workspace_fid"],
        "peer_subject": row["peer_subject_fid"],
        "initiator": row["initiator"],
        "state": row["state"],
        "outbound_scopes": list(row["outbound_scopes"]),
        "inbound_scopes": list(row["inbound_scopes"]),
        "grant_version": row["grant_version"],
        "revocation_epoch": row["revocation_epoch"],
        "expires_at": row["expires_at"].isoformat(),
        "encryption_policy": row["encryption_policy"],
        "reason": row["reason"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _policy_hash(row: dict[str, Any]) -> str:
    return sha256_hex(
        {
            "state": row["state"],
            "outbound_scopes": sorted(row["outbound_scopes"]),
            "inbound_scopes": sorted(row["inbound_scopes"]),
            "expires_at": row["expires_at"].astimezone(timezone.utc).isoformat(),
            "encryption_policy": row["encryption_policy"],
            "revocation_epoch": row["revocation_epoch"],
        }
    )


async def _row(
    platform: AsyncSession, workspace_id: str, connection_id: str, *, lock: bool = False
) -> dict[str, Any]:
    row = (
        (
            await platform.execute(
                text(
                    "SELECT * FROM federation_grants WHERE workspace_id = CAST(:w AS uuid) "
                    "AND connection_id = :c" + (" FOR UPDATE" if lock else "")
                ),
                {"w": str(workspace_id), "c": connection_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise FederationError("FED_E_GRANT_UNKNOWN", "no such connection")
    return dict(row)


async def _record(
    platform: AsyncSession,
    row: dict[str, Any],
    action: str,
    *,
    to_state: str,
    actor: str,
    approved_by: str | None = None,
    outbound: list[str] | None = None,
    inbound: list[str] | None = None,
    bump_epoch: bool = False,
) -> dict[str, Any]:
    """Apply one transition: update the row, append its history line."""
    new = dict(row)
    new["state"] = to_state
    if outbound is not None:
        new["outbound_scopes"] = outbound
    if inbound is not None:
        new["inbound_scopes"] = inbound
    new["revocation_epoch"] = row["revocation_epoch"] + (1 if bump_epoch else 0)
    new["grant_version"] = row["grant_version"] + 1
    await platform.execute(
        text(
            "UPDATE federation_grants SET state = :s, outbound_scopes = :o, inbound_scopes = :i, "
            "revocation_epoch = :e, grant_version = :v, updated_at = NOW() "
            "WHERE workspace_id = CAST(:w AS uuid) AND connection_id = :c"
        ),
        {
            "s": to_state,
            "o": new["outbound_scopes"],
            "i": new["inbound_scopes"],
            "e": new["revocation_epoch"],
            "v": new["grant_version"],
            "w": str(row["workspace_id"]),
            "c": row["connection_id"],
        },
    )
    await _history(
        platform, new, action, from_state=row["state"], actor=actor, approved_by=approved_by
    )
    return new


async def _history(
    platform: AsyncSession,
    row: dict[str, Any],
    action: str,
    *,
    from_state: str | None,
    actor: str,
    approved_by: str | None = None,
) -> None:
    await platform.execute(
        text(
            "INSERT INTO federation_grant_history (workspace_id, tenant_id, connection_id, "
            "grant_version, action, from_state, to_state, outbound_scopes, inbound_scopes, "
            "expires_at, revocation_epoch, actor, approved_by, policy_hash) VALUES "
            "(CAST(:w AS uuid), '', :c, :v, :a, :f, :t, :o, :i, :x, :e, :who, :ap, :h)"
        ),
        {
            "w": str(row["workspace_id"]),
            "c": row["connection_id"],
            "v": row["grant_version"],
            "a": action,
            "f": from_state,
            "t": row["state"],
            "o": row["outbound_scopes"],
            "i": row["inbound_scopes"],
            "x": row["expires_at"],
            "e": row["revocation_epoch"],
            "who": actor,
            "ap": approved_by,
            "h": _policy_hash(row),
        },
    )


async def _upsert_peer(
    platform: AsyncSession, workspace_id: str, peer_ws: str, peer_subject: str | None
) -> None:
    await platform.execute(
        text(
            "INSERT INTO federation_peers (workspace_id, tenant_id, peer_workspace_fid, "
            "peer_subject_fid) VALUES (CAST(:w AS uuid), '', :p, :s) "
            "ON CONFLICT (workspace_id, peer_workspace_fid) DO UPDATE SET last_seen_at = NOW()"
        ),
        {"w": str(workspace_id), "p": peer_ws, "s": peer_subject},
    )


async def _expire_if_due(platform: AsyncSession, row: dict[str, Any]) -> dict[str, Any]:
    if row["state"] in ("REQUESTED", "ACTIVE") and row["expires_at"] <= _now():
        return await _record(platform, row, "expire", to_state="EXPIRED", actor="system")
    return row


async def _sign_and_send(local_fid: str, type: str, peer_ws: str, payload: dict[str, Any]):
    envelope = await identity.sign_as(local_fid, type=type, audience=peer_ws, payload=payload)
    return await _transport.deliver(envelope)


# ── local actions ─────────────────────────────────────────────────────────


async def request(
    workspace_id: str,
    *,
    peer_workspace: str,
    outbound_scopes: list[str],
    inbound_scopes: list[str],
    reason: str,
    actor: Actor,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> dict[str, Any]:
    """Ask another workspace for a connection. The requesting owner's consent
    is this call itself; the peer's owner still has to approve."""
    _require_owner(actor)
    require_type(peer_workspace, "workspace")
    outbound, inbound = _check_scopes(outbound_scopes), _check_scopes(inbound_scopes)
    if not outbound and not inbound:
        raise FederationError("FED_E_SCOPE_UNKNOWN", "ask for at least one scope")
    if not (1 <= ttl_days <= MAX_TTL_DAYS):
        raise FederationError("FED_E_SCOPE_UNKNOWN", "ttl_days must be 1-365")
    if not isinstance(reason, str) or len(reason.strip()) < 5:
        raise FederationError(
            "FED_E_SCOPE_UNKNOWN", "a reason of at least 5 characters is required"
        )
    peer = await identity.resolve(peer_workspace)
    if peer["status"] != "active":
        raise FederationError("FED_E_IDENTITY_FROZEN", "peer workspace is not active")
    connection_id = new_connection_id()
    expires_at = _now() + timedelta(days=ttl_days)
    async with async_session_factory() as platform:
        local_fid = await _local_fid(platform, workspace_id)
        if local_fid == peer_workspace:
            raise FederationError("FED_E_TYPE_MISMATCH", "a workspace cannot connect to itself")
        row = {
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "local_workspace_fid": local_fid,
            "peer_workspace_fid": peer_workspace,
            "peer_subject_fid": peer["parent_id"],
            "initiator": "local",
            "state": "REQUESTED",
            "outbound_scopes": outbound,
            "inbound_scopes": inbound,
            "grant_version": 1,
            "revocation_epoch": 0,
            "expires_at": expires_at,
            "encryption_policy": "server_readable",
            "reason": reason.strip(),
        }
        try:
            async with platform.begin_nested():
                await platform.execute(
                    text(
                        "INSERT INTO federation_grants (connection_id, workspace_id, tenant_id, "
                        "local_workspace_fid, peer_workspace_fid, peer_subject_fid, initiator, "
                        "state, outbound_scopes, inbound_scopes, grant_version, expires_at, "
                        "encryption_policy, reason) VALUES (:connection_id, "
                        "CAST(:workspace_id AS uuid), '', :local_workspace_fid, "
                        ":peer_workspace_fid, :peer_subject_fid, 'local', 'REQUESTED', "
                        ":outbound_scopes, :inbound_scopes, 1, :expires_at, "
                        "'server_readable', :reason)"
                    ),
                    {
                        k: v
                        for k, v in row.items()
                        if k
                        not in (
                            "initiator",
                            "state",
                            "grant_version",
                            "revocation_epoch",
                            "encryption_policy",
                        )
                    },
                )
        except IntegrityError as exc:
            raise FederationError(
                "FED_E_GRANT_TRANSITION", "a live connection with this workspace already exists"
            ) from exc
        await _history(
            platform, row, "request", from_state=None, actor=actor.ref, approved_by=actor.ref
        )
        await _upsert_peer(platform, workspace_id, peer_workspace, peer["parent_id"])
        # Deliver before committing: if the peer refuses, nothing is kept here.
        await _sign_and_send(
            local_fid,
            "grant.request",
            peer_workspace,
            {
                "connection_id": connection_id,
                "requester_scopes": outbound,
                "recipient_scopes": inbound,
                "reason": row["reason"],
                "expires_at": expires_at.replace(microsecond=0).isoformat(),
                "encryption_policies": list(policy.SUPPORTED),
            },
        )
        await platform.commit()
        return _out(await _row(platform, workspace_id, connection_id))


async def approve(
    workspace_id: str,
    connection_id: str,
    *,
    actor: Actor,
    outbound_scopes: list[str] | None = None,
    inbound_scopes: list[str] | None = None,
) -> dict[str, Any]:
    """The receiving owner agrees (optionally to fewer scopes than asked)."""
    _require_owner(actor)
    async with async_session_factory() as platform:
        row = await _expire_if_due(
            platform, await _row(platform, workspace_id, connection_id, lock=True)
        )
        if row["initiator"] != "peer" or row["state"] != "REQUESTED":
            await platform.commit()
            raise FederationError(
                "FED_E_GRANT_TRANSITION",
                "only a pending request from a peer can be approved",
                state=row["state"],
            )
        outbound = _narrowed(row["outbound_scopes"], outbound_scopes)
        inbound = _narrowed(row["inbound_scopes"], inbound_scopes)
        new = await _record(
            platform,
            row,
            "approve",
            to_state="ACTIVE",
            actor=actor.ref,
            approved_by=actor.ref,
            outbound=outbound,
            inbound=inbound,
        )
        await _sign_and_send(
            row["local_workspace_fid"],
            "grant.decision",
            row["peer_workspace_fid"],
            {
                "connection_id": connection_id,
                "decision": "approved",
                # From the requester's point of view.
                "requester_scopes": inbound,
                "recipient_scopes": outbound,
                "encryption_policy": row["encryption_policy"],
            },
        )
        await platform.commit()
        return _out(new)


def _narrowed(current: list[str], wanted: list[str] | None) -> list[str]:
    if wanted is None:
        return list(current)
    wanted = _check_scopes(wanted)
    if not set(wanted) <= set(current):
        raise FederationError(
            "FED_E_SCOPE_DENIED",
            "an approval can only keep or remove requested scopes",
            extra=sorted(set(wanted) - set(current)),
        )
    return wanted


async def reject(workspace_id: str, connection_id: str, *, actor: Actor) -> dict[str, Any]:
    _require_person(actor)
    async with async_session_factory() as platform:
        row = await _row(platform, workspace_id, connection_id, lock=True)
        if row["initiator"] != "peer" or row["state"] != "REQUESTED":
            raise FederationError(
                "FED_E_GRANT_TRANSITION", "only a pending request can be rejected"
            )
        new = await _record(platform, row, "reject", to_state="REJECTED", actor=actor.ref)
        await _sign_and_send(
            row["local_workspace_fid"],
            "grant.decision",
            row["peer_workspace_fid"],
            {"connection_id": connection_id, "decision": "rejected"},
        )
        await platform.commit()
        return _out(new)


async def revoke(workspace_id: str, connection_id: str, *, actor: Actor) -> dict[str, Any]:
    """Either side's person ends the connection; the peer is told."""
    _require_person(actor)
    async with async_session_factory() as platform:
        row = await _row(platform, workspace_id, connection_id, lock=True)
        if row["state"] not in ("ACTIVE", "SUSPENDED", "EXPIRED", "REQUESTED"):
            raise FederationError(
                "FED_E_GRANT_TRANSITION", "connection already ended", state=row["state"]
            )
        new = await _record(
            platform, row, "revoke", to_state="REVOKED", actor=actor.ref, bump_epoch=True
        )
        await platform.commit()
    # Best effort: the local fence already holds whether or not the peer hears.
    try:
        await _sign_and_send(
            row["local_workspace_fid"],
            "grant.revoke",
            row["peer_workspace_fid"],
            {"connection_id": connection_id},
        )
    except FederationError:
        pass
    return _out(new)


async def block(workspace_id: str, connection_id: str, *, actor: Actor) -> dict[str, Any]:
    """Local only: nothing is sent, so the peer learns nothing (ICD_I_GRANT §3)."""
    _require_person(actor)
    async with async_session_factory() as platform:
        row = await _row(platform, workspace_id, connection_id, lock=True)
        if row["state"] in ("REJECTED", "REVOKED", "BLOCKED"):
            raise FederationError(
                "FED_E_GRANT_TRANSITION", "connection already ended", state=row["state"]
            )
        new = await _record(
            platform, row, "block", to_state="BLOCKED", actor=actor.ref, bump_epoch=True
        )
        await platform.commit()
        return _out(new)


async def freeze(workspace_id: str, connection_id: str, *, actor: Actor) -> dict[str, Any]:
    _require_person(actor)
    async with async_session_factory() as platform:
        row = await _row(platform, workspace_id, connection_id, lock=True)
        if row["state"] != "ACTIVE":
            raise FederationError(
                "FED_E_GRANT_TRANSITION", "only an active connection can be frozen"
            )
        new = await _record(
            platform, row, "freeze", to_state="SUSPENDED", actor=actor.ref, bump_epoch=True
        )
        await platform.commit()
        return _out(new)


async def unfreeze(workspace_id: str, connection_id: str, *, actor: Actor) -> dict[str, Any]:
    _require_owner(actor)
    async with async_session_factory() as platform:
        row = await _row(platform, workspace_id, connection_id, lock=True)
        if row["state"] != "SUSPENDED":
            raise FederationError(
                "FED_E_GRANT_TRANSITION", "only a frozen connection can be unfrozen"
            )
        new = await _record(
            platform, row, "unfreeze", to_state="ACTIVE", actor=actor.ref, approved_by=actor.ref
        )
        new = await _expire_if_due(platform, new)
        await platform.commit()
        return _out(new)


async def change_scopes(
    workspace_id: str,
    connection_id: str,
    *,
    actor: Actor,
    outbound_scopes: list[str],
    inbound_scopes: list[str],
) -> dict[str, Any]:
    """P1 only narrows (widening needs both owners again: a new connection).
    Narrowing is a fence: anything signed before it is refused (GRANT §6)."""
    _require_person(actor)
    async with async_session_factory() as platform:
        row = await _row(platform, workspace_id, connection_id, lock=True)
        if row["state"] != "ACTIVE":
            raise FederationError("FED_E_GRANT_TRANSITION", "only an active connection can change")
        outbound = _narrowed(row["outbound_scopes"], outbound_scopes)
        inbound = _narrowed(row["inbound_scopes"], inbound_scopes)
        new = await _record(
            platform,
            row,
            "change_scopes",
            to_state="ACTIVE",
            actor=actor.ref,
            outbound=outbound,
            inbound=inbound,
            bump_epoch=True,
        )
        await platform.commit()
        return _out(new)


async def list_connections(workspace_id: str) -> list[dict[str, Any]]:
    async with async_session_factory() as platform:
        rows = (
            (
                await platform.execute(
                    text(
                        "SELECT * FROM federation_grants WHERE workspace_id = CAST(:w AS uuid) "
                        "ORDER BY created_at DESC"
                    ),
                    {"w": str(workspace_id)},
                )
            )
            .mappings()
            .all()
        )
        out = [_out(await _expire_if_due(platform, dict(r))) for r in rows]
        await platform.commit()
    return out


async def history(workspace_id: str, connection_id: str) -> list[dict[str, Any]]:
    async with async_session_factory() as platform:
        await _row(platform, workspace_id, connection_id)
        rows = (
            (
                await platform.execute(
                    text(
                        "SELECT grant_version, action, from_state, to_state, outbound_scopes, "
                        "inbound_scopes, revocation_epoch, actor, approved_by, policy_hash, "
                        "created_at FROM federation_grant_history WHERE workspace_id = "
                        "CAST(:w AS uuid) AND connection_id = :c ORDER BY grant_version"
                    ),
                    {"w": str(workspace_id), "c": connection_id},
                )
            )
            .mappings()
            .all()
        )
    return [
        {
            **dict(r),
            "outbound_scopes": list(r["outbound_scopes"]),
            "inbound_scopes": list(r["inbound_scopes"]),
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


# ── the fence (ICD_I_GRANT §6) ────────────────────────────────────────────


async def authorize(
    workspace_id: str,
    peer_workspace: str,
    scope: str,
    *,
    direction: str,
    signed_at: datetime | None = None,
) -> dict[str, Any]:
    """Every federation entry point calls this before any side effect.

    ``direction='out'``: this workspace acts on the peer; ``'in'``: the peer
    acts on this workspace. ``signed_at`` is when an inbound object was signed:
    anything signed before the latest freeze, revoke, block or narrowing is
    stale even if its signature is still valid.
    """
    if scope not in SCOPES:
        raise FederationError("FED_E_SCOPE_UNKNOWN", "unknown scope", scope=scope)
    if direction not in ("in", "out"):
        raise ValueError("direction is 'in' or 'out'")
    async with async_session_factory() as platform:
        row = (
            (
                await platform.execute(
                    text(
                        "SELECT * FROM federation_grants WHERE workspace_id = CAST(:w AS uuid) "
                        "AND peer_workspace_fid = :p ORDER BY (state IN ('REQUESTED', 'ACTIVE', "
                        "'EXPIRED', 'SUSPENDED')) DESC, updated_at DESC LIMIT 1 FOR UPDATE"
                    ),
                    {"w": str(workspace_id), "p": peer_workspace},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise FederationError("FED_E_GRANT_UNKNOWN", "no connection with this workspace")
        row = await _expire_if_due(platform, dict(row))
        await platform.commit()
        if row["state"] == "EXPIRED":
            raise FederationError("FED_E_GRANT_EXPIRED", "the connection has expired")
        if row["state"] != "ACTIVE":
            # BLOCKED, REVOKED, SUSPENDED and pending look the same to a caller.
            raise FederationError("FED_E_GRANT_INACTIVE", "the connection is not active")
        peer = await identity.resolve(peer_workspace)
        if peer["status"] != "active":
            raise FederationError("FED_E_IDENTITY_FROZEN", "peer identity is not active")
        scopes = row["outbound_scopes"] if direction == "out" else row["inbound_scopes"]
        if scope not in scopes:
            raise FederationError(
                "FED_E_SCOPE_DENIED",
                f"the connection does not allow {scope} ({direction})",
                scope=scope,
                direction=direction,
            )
        if signed_at is not None:
            fenced = await platform.scalar(
                text(
                    "SELECT max(created_at) FROM federation_grant_history WHERE workspace_id = "
                    "CAST(:w AS uuid) AND connection_id = :c AND (action IN ('freeze', "
                    "'revoke', 'block', 'change_scopes'))"
                ),
                {"w": str(workspace_id), "c": row["connection_id"]},
            )
            if fenced is not None and signed_at <= fenced:
                raise FederationError(
                    "FED_E_GRANT_STALE", "signed before the connection was last restricted"
                )
        return _out(row)


# ── inbound (what the transport hands to a workspace) ─────────────────────


async def receive(workspace_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
    async with async_session_factory() as platform:
        local_fid = await _local_fid(platform, workspace_id)
    verified = await verify_envelope(
        envelope,
        local_workspace_fid=local_fid,
        key_lookup=identity.key_lookup,
        replay_check=identity.replay_check_for(workspace_id),
    )
    issuer_type, _ = parse_federation_id(verified.issuer)
    if issuer_type != "workspace":
        raise FederationError("FED_E_TYPE_MISMATCH", "connections are between workspaces")
    handlers = {
        "grant.request": _on_request,
        "grant.decision": _on_decision,
        "grant.revoke": _on_revoke,
    }
    handler = handlers.get(verified.type)
    if handler is None:
        raise FederationError(
            "FED_E_ENVELOPE_INVALID", "not a connection object", type=verified.type
        )
    return await handler(workspace_id, local_fid, verified)


def _payload_scopes(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
        raise FederationError("FED_E_ENVELOPE_INVALID", f"{key} must be a list of scopes")
    return _check_scopes(value)


async def _on_request(workspace_id: str, local_fid: str, msg: Verified) -> dict[str, Any]:
    p = msg.payload
    connection_id = p.get("connection_id")
    if not isinstance(connection_id, str) or not connection_id.startswith("fcn_"):
        raise FederationError("FED_E_ENVELOPE_INVALID", "connection_id is required")
    requester_scopes = _payload_scopes(p, "requester_scopes")
    recipient_scopes = _payload_scopes(p, "recipient_scopes")
    chosen = policy.negotiate(p.get("encryption_policies") or [])
    try:
        expires_at = datetime.fromisoformat(p["expires_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FederationError("FED_E_ENVELOPE_INVALID", "expires_at is required") from exc
    if expires_at.tzinfo is None or expires_at > _now() + timedelta(days=MAX_TTL_DAYS + 1):
        raise FederationError("FED_E_ENVELOPE_INVALID", "expires_at out of range")
    reason = p.get("reason")
    if not isinstance(reason, str) or len(reason.strip()) < 5:
        raise FederationError("FED_E_ENVELOPE_INVALID", "reason is required")
    peer = await identity.resolve(msg.issuer)
    async with async_session_factory() as platform:
        blocked = await platform.scalar(
            text(
                "SELECT 1 FROM federation_grants WHERE workspace_id = CAST(:w AS uuid) "
                "AND peer_workspace_fid = :p AND state = 'BLOCKED' LIMIT 1"
            ),
            {"w": str(workspace_id), "p": msg.issuer},
        )
        if blocked:
            raise FederationError("FED_E_GRANT_INACTIVE", "the connection is not active")
        row = {
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "local_workspace_fid": local_fid,
            "peer_workspace_fid": msg.issuer,
            "peer_subject_fid": peer["parent_id"],
            "state": "REQUESTED",
            # This side's view: what the requester asked to do to us is inbound.
            "outbound_scopes": recipient_scopes,
            "inbound_scopes": requester_scopes,
            "grant_version": 1,
            "revocation_epoch": 0,
            "expires_at": expires_at,
            "encryption_policy": chosen,
            "reason": reason.strip(),
        }
        try:
            async with platform.begin_nested():
                await platform.execute(
                    text(
                        "INSERT INTO federation_grants (connection_id, workspace_id, tenant_id, "
                        "local_workspace_fid, peer_workspace_fid, peer_subject_fid, initiator, "
                        "state, outbound_scopes, inbound_scopes, grant_version, expires_at, "
                        "encryption_policy, reason) VALUES (:c, CAST(:w AS uuid), '', :l, :p, "
                        ":ps, 'peer', 'REQUESTED', :o, :i, 1, :x, :pol, :r)"
                    ),
                    {
                        "c": connection_id,
                        "w": str(workspace_id),
                        "l": local_fid,
                        "p": msg.issuer,
                        "ps": peer["parent_id"],
                        "o": recipient_scopes,
                        "i": requester_scopes,
                        "x": expires_at,
                        "pol": chosen,
                        "r": row["reason"],
                    },
                )
        except IntegrityError as exc:
            raise FederationError(
                "FED_E_GRANT_TRANSITION", "a live connection with this workspace already exists"
            ) from exc
        await _history(platform, row, "request", from_state=None, actor=f"peer:{msg.issuer}")
        await _upsert_peer(platform, workspace_id, msg.issuer, peer["parent_id"])
        await platform.commit()
    return {"status": "received", "connection_id": connection_id}


async def _peer_row(platform, workspace_id: str, msg: Verified) -> dict[str, Any]:
    connection_id = msg.payload.get("connection_id")
    if not isinstance(connection_id, str):
        raise FederationError("FED_E_ENVELOPE_INVALID", "connection_id is required")
    row = await _row(platform, workspace_id, connection_id, lock=True)
    if row["peer_workspace_fid"] != msg.issuer:
        # Only the other party of this connection may change it.
        raise FederationError("FED_E_GRANT_UNKNOWN", "no such connection")
    return row


async def _on_decision(workspace_id: str, local_fid: str, msg: Verified) -> dict[str, Any]:
    decision = msg.payload.get("decision")
    async with async_session_factory() as platform:
        row = await _peer_row(platform, workspace_id, msg)
        if row["initiator"] != "local" or row["state"] != "REQUESTED":
            raise FederationError("FED_E_GRANT_TRANSITION", "no pending request to decide")
        if decision == "rejected":
            new = await _record(
                platform, row, "reject", to_state="REJECTED", actor=f"peer:{msg.issuer}"
            )
        elif decision == "approved":
            # Both owners have now agreed: ours when we asked, theirs now.
            outbound = _payload_scopes(msg.payload, "requester_scopes")
            inbound = _payload_scopes(msg.payload, "recipient_scopes")
            if not (
                set(outbound) <= set(row["outbound_scopes"])
                and set(inbound) <= set(row["inbound_scopes"])
            ):
                raise FederationError("FED_E_SCOPE_DENIED", "approval grants more than was asked")
            policy.negotiate(
                [msg.payload.get("encryption_policy", "")], current=row["encryption_policy"]
            )
            requester = await platform.scalar(
                text(
                    "SELECT approved_by FROM federation_grant_history WHERE workspace_id = "
                    "CAST(:w AS uuid) AND connection_id = :c AND action = 'request'"
                ),
                {"w": str(workspace_id), "c": row["connection_id"]},
            )
            new = await _record(
                platform,
                row,
                "approve",
                to_state="ACTIVE",
                actor=f"peer:{msg.issuer}",
                approved_by=requester,
                outbound=outbound,
                inbound=inbound,
            )
        else:
            raise FederationError("FED_E_ENVELOPE_INVALID", "decision is approved or rejected")
        await platform.commit()
    return {"status": "received", "state": new["state"]}


async def _on_revoke(workspace_id: str, local_fid: str, msg: Verified) -> dict[str, Any]:
    async with async_session_factory() as platform:
        row = await _peer_row(platform, workspace_id, msg)
        if row["state"] in ("REJECTED", "REVOKED", "BLOCKED"):
            await platform.commit()
            return {"status": "received", "state": row["state"]}
        new = await _record(
            platform, row, "revoke", to_state="REVOKED", actor=f"peer:{msg.issuer}", bump_epoch=True
        )
        await platform.commit()
    return {"status": "received", "state": new["state"]}


def signed_at(envelope: dict[str, Any]) -> datetime:
    return parse_time(envelope["issued_at"])


__all__ = [
    "Actor",
    "LocalTransport",
    "SCOPES",
    "approve",
    "authorize",
    "block",
    "change_scopes",
    "freeze",
    "history",
    "list_connections",
    "receive",
    "reject",
    "request",
    "revoke",
    "set_transport",
    "signed_at",
    "unfreeze",
]
