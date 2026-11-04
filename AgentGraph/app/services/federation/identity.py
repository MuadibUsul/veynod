"""Hosted federation identities (I-ID, F-02a): create, resolve, rotate,
revoke, freeze, aliases, and the database-backed lookups the envelope
verifier uses.

All tables touched here except ``federation_replay_guard`` are platform-only
(ADR-011): this module uses platform sessions, and nothing it returns to a
caller contains a local id (ID-INV-4, ID-INV-8). ``map_local`` is the single,
platform-internal way back to local rows.

P1 placeholders (I-ID §11, ADR-001a): one subject per root tenant (TBD-2);
handles are 1–64 of ``[a-z0-9_-]`` after NFKC + casefold and are never
reallocated (TBD-1, TBD-6); a lost key means freeze and re-issue (TBD-3).
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_factory
from app.services.federation import is_enabled, keys
from app.services.federation.canonical import sha256_hex
from app.services.federation.envelope import KeyView, build_envelope, format_time
from app.services.federation.errors import FederationError
from app.services.federation.ids import (
    b64url_decode,
    derive_federation_id,
    parse_federation_id,
    require_type,
)

_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")  # TBD-1 placeholder
_PARENT_LAYER = {"workspace": "subject", "agent": "workspace"}


@dataclass(frozen=True)
class LocalRef:
    """Platform-internal mapping to local rows. Never serialised (ID-INV-4)."""

    tenant_id: str
    workspace_id: str | None
    principal_id: str | None

    def __repr__(self) -> str:
        return "LocalRef(<platform-internal>)"

    def __reduce__(self):  # pickling is a form of export
        raise TypeError("LocalRef is platform-internal and cannot be exported")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_enabled() -> None:
    if not is_enabled():
        raise FederationError("FED_E_DISABLED", "federation is disabled on this deployment")


def normalize_handle(handle: str) -> str:
    """NFKC + casefold, then the P1 character set (TBD-1 placeholder)."""
    if not isinstance(handle, str):
        raise FederationError("FED_E_ALIAS_INVALID", "a handle is a string")
    normal = unicodedata.normalize("NFKC", handle).casefold()
    if not _HANDLE_RE.fullmatch(normal):
        raise FederationError(
            "FED_E_ALIAS_INVALID",
            "a handle is 1-64 of a-z, 0-9, '_' or '-' and starts with a letter or digit",
        )
    return normal


# ── reading ───────────────────────────────────────────────────────────────


async def _identity_row(platform: AsyncSession, fid: str) -> dict[str, Any]:
    row = (
        (
            await platform.execute(
                text("SELECT * FROM federation_identities WHERE federation_id = :f"), {"f": fid}
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise FederationError(
            "FED_E_IDENTITY_UNKNOWN", "unknown federation identity", federation_id=fid
        )
    return dict(row)


async def _keys(platform: AsyncSession, fid: str) -> list[dict[str, Any]]:
    rows = (
        (
            await platform.execute(
                text(
                    "SELECT key_id, alg, public_key, state, not_before, not_after FROM "
                    "federation_identity_keys "
                    "WHERE federation_id = :f ORDER BY created_at, key_id"
                ),
                {"f": fid},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def _alias(platform: AsyncSession, fid: str) -> str | None:
    return await platform.scalar(
        text(
            "SELECT display_handle FROM federation_addresses WHERE target_id = :f AND status = "
            "'active'"
        ),
        {"f": fid},
    )


def _key_public(key: dict[str, Any]) -> dict[str, Any]:
    return {
        "key_id": key["key_id"],
        "alg": key["alg"],
        "public_key": key["public_key"],
        "not_before": format_time(key["not_before"]),
        "not_after": format_time(key["not_after"]) if key["not_after"] else None,
        "state": key["state"],
    }


async def _view(platform: AsyncSession, fid: str) -> dict[str, Any]:
    """IdentityView (I-ID §7.1): ids, keys, status and a non-authoritative alias."""
    row = await _identity_row(platform, fid)
    previous = await platform.scalar(
        text(
            "SELECT object_hash FROM federation_identity_log WHERE federation_id = :f "
            "ORDER BY serial DESC LIMIT 1"
        ),
        {"f": fid},
    )
    return {
        "federation_id": row["federation_id"],
        "id_type": row["id_type"],
        "parent_id": row["parent_id"],
        "genesis_public_key": row["genesis_public_key"],
        "keys": [_key_public(k) for k in await _keys(platform, fid)],
        "key_set_version": row["key_set_version"],
        "status": row["status"],
        "display_alias": await _alias(platform, fid),
        "issued_at": format_time(row["issued_at"]),
        "expires_at": format_time(row["expires_at"]),
        "previous_hash": previous,
    }


async def resolve(fid: str) -> dict[str, Any]:
    _require_enabled()
    parse_federation_id(fid)
    async with async_session_factory() as platform:
        return await _view(platform, fid)


async def local_identity(workspace_id: str) -> dict[str, Any] | None:
    """This workspace's identity, or None when federation is off or not enabled for it."""
    if not is_enabled():
        return None
    async with async_session_factory() as platform:
        fid = await platform.scalar(
            text(
                "SELECT federation_id FROM federation_local_map "
                "WHERE id_type = 'workspace' AND workspace_id = CAST(:w AS uuid)"
            ),
            {"w": str(workspace_id)},
        )
        return None if fid is None else await _view(platform, fid)


async def map_local(fid: str) -> LocalRef:
    """Platform-internal only: the local rows behind a hosted identity."""
    async with async_session_factory() as platform:
        row = (
            await platform.execute(
                text(
                    "SELECT tenant_id, workspace_id, principal_id FROM federation_local_map "
                    "WHERE federation_id = :f"
                ),
                {"f": fid},
            )
        ).first()
    if row is None:
        raise FederationError("FED_E_IDENTITY_UNKNOWN", "not a hosted identity", federation_id=fid)
    return LocalRef(row[0], str(row[1]) if row[1] else None, row[2])


# ── signing ───────────────────────────────────────────────────────────────


async def _current_key(platform: AsyncSession, fid: str, *, exclude: str | None = None):
    row = (
        await platform.execute(
            text(
                "SELECT key_id, private_key_sealed FROM federation_identity_keys "
                "WHERE federation_id = :f AND state = 'active' AND private_key_sealed IS NOT NULL "
                "AND (CAST(:x AS text) IS NULL OR key_id <> :x) "
                "ORDER BY created_at DESC, key_id LIMIT 1"
            ),
            {"f": fid, "x": exclude},
        )
    ).first()
    if row is None:
        return None
    return row[0], keys.unseal(row[1], row[0])


async def _sign(
    platform: AsyncSession,
    signer: str,
    *,
    type: str,
    audience: str | None,
    payload: dict[str, Any],
    ttl: timedelta = timedelta(hours=1),
    exclude: str | None = None,
) -> dict[str, Any] | None:
    current = await _current_key(platform, signer, exclude=exclude)
    if current is None:
        return None
    key_id, private = current
    return build_envelope(
        type=type,
        issuer=signer,
        key_id=key_id,
        private=private,
        audience=audience,
        payload=payload,
        ttl=ttl,
    )


async def sign_as(
    fid: str,
    *,
    type: str,
    audience: str | None,
    payload: dict[str, Any],
    ttl: timedelta = timedelta(hours=1),
) -> dict[str, Any]:
    _require_enabled()
    async with async_session_factory() as platform:
        row = await _identity_row(platform, fid)
        if row["status"] != "active":
            raise FederationError(
                "FED_E_IDENTITY_FROZEN", "identity is not active", status=row["status"]
            )
        envelope = await _sign(
            platform, fid, type=type, audience=audience, payload=payload, ttl=ttl
        )
    if envelope is None:
        raise FederationError("FED_E_KEY_NOT_CURRENT", "identity has no usable hosted key")
    return envelope


async def _log(
    platform: AsyncSession,
    fid: str,
    object_type: str,
    payload: dict[str, Any],
    *,
    actor: str,
    signer: str | None,
) -> None:
    last = (
        await platform.execute(
            text(
                "SELECT serial, object_hash FROM federation_identity_log WHERE federation_id = :f "
                "ORDER BY serial DESC LIMIT 1 FOR UPDATE"
            ),
            {"f": fid},
        )
    ).first()
    serial, previous = (1, None) if last is None else (last[0] + 1, last[1])
    body = {**payload, "previous_hash": previous, "serial": serial}
    envelope = None
    if signer is not None:
        envelope = await _sign(
            platform,
            signer,
            type=_signed_type(object_type),
            audience=None,
            payload=body,
            ttl=timedelta(hours=24),
        )
    await platform.execute(
        text(
            "INSERT INTO federation_identity_log (federation_id, serial, object_type, object_hash, "
            "previous_hash, envelope, actor) VALUES (:f, :s, :t, :h, :p, CAST(:e AS jsonb), :a)"
        ),
        {
            "f": fid,
            "s": serial,
            "t": object_type,
            "h": sha256_hex(body),
            "p": previous,
            "e": None if envelope is None else json.dumps(envelope),
            "a": actor,
        },
    )
    await platform.execute(
        text("UPDATE federation_identities SET record_hash = :h WHERE federation_id = :f"),
        {"h": sha256_hex(body), "f": fid},
    )


def _signed_type(object_type: str) -> str:
    return (
        object_type
        if object_type in ("identity.record", "identity.rotation", "identity.revocation")
        else "identity.record"
    )


# ── creating identities ───────────────────────────────────────────────────


async def _create(
    platform: AsyncSession,
    id_type: str,
    parent_id: str | None,
    *,
    tenant_id: str,
    workspace_id: str | None,
    principal_id: str | None,
    actor: str,
) -> str:
    settings = get_settings()
    pair = keys.generate()
    fid = derive_federation_id(id_type, pair.public_raw)
    sealed, kek_id = keys.seal(pair)
    now = _now()
    await platform.execute(
        text(
            "INSERT INTO federation_identities (federation_id, id_type, parent_id, "
            "genesis_public_key, "
            "issued_at, expires_at) VALUES (:f, :t, :p, :g, :i, :e)"
        ),
        {
            "f": fid,
            "t": id_type,
            "p": parent_id,
            "g": pair.public_b64,
            "i": now,
            "e": now + timedelta(days=settings.federation_record_lifetime_days),
        },
    )
    await platform.execute(
        text(
            "INSERT INTO federation_identity_keys (key_id, federation_id, public_key, "
            "private_key_sealed, "
            "kek_id, state, is_genesis, not_before) VALUES (:k, :f, :p, :s, :kek, 'active', "
            "TRUE, :n)"
        ),
        {"k": pair.key_id, "f": fid, "p": pair.public_b64, "s": sealed, "kek": kek_id, "n": now},
    )
    await platform.execute(
        text(
            "INSERT INTO federation_local_map (federation_id, id_type, tenant_id, workspace_id, "
            "principal_id) "
            "VALUES (:f, :t, :ten, CAST(:w AS uuid), :p)"
        ),
        {"f": fid, "t": id_type, "ten": tenant_id, "w": workspace_id, "p": principal_id},
    )
    # Subjects sign their own record with the genesis key; workspaces and
    # agents are signed by their parent's current key (I-ID §5.1).
    view = await _view(platform, fid)
    await _log(platform, fid, "identity.record", view, actor=actor, signer=parent_id or fid)
    return fid


async def _workspace_tenants(platform: AsyncSession, workspace_id: str) -> tuple[str, str]:
    row = (
        await platform.execute(
            text(
                "SELECT w.tenant_id, t.root_tenant_id FROM workspaces w JOIN tenants t ON t.id "
                "= w.tenant_id "
                "WHERE w.id = CAST(:w AS uuid)"
            ),
            {"w": str(workspace_id)},
        )
    ).first()
    if row is None:
        raise FederationError("FED_E_IDENTITY_UNKNOWN", "workspace not found")
    return row[0], row[1]


async def enable_workspace(workspace_id: str, *, actor: str) -> dict[str, Any]:
    """Create this workspace's identity (and its root tenant's subject if needed).

    Idempotent. The caller authorises: only an owner-level person may enable
    federation for a workspace (route layer).
    """
    _require_enabled()
    async with async_session_factory() as platform:
        await platform.execute(text("SELECT pg_advisory_xact_lock(hashtext('federation_enable'))"))
        tenant_id, root_id = await _workspace_tenants(platform, workspace_id)
        existing = await platform.scalar(
            text(
                "SELECT federation_id FROM federation_local_map "
                "WHERE id_type = 'workspace' AND workspace_id = CAST(:w AS uuid)"
            ),
            {"w": str(workspace_id)},
        )
        if existing is None:
            subject = await platform.scalar(
                text(
                    "SELECT federation_id FROM federation_local_map "
                    "WHERE id_type = 'subject' AND tenant_id = :t"
                ),
                {"t": root_id},
            )
            if subject is None:
                subject = await _create(
                    platform,
                    "subject",
                    None,
                    tenant_id=root_id,
                    workspace_id=None,
                    principal_id=None,
                    actor=actor,
                )
            existing = await _create(
                platform,
                "workspace",
                subject,
                tenant_id=tenant_id,
                workspace_id=str(workspace_id),
                principal_id=None,
                actor=actor,
            )
        view = await _view(platform, existing)
        await platform.commit()
    return view


async def register_agent(workspace_id: str, principal_id: str, *, actor: str) -> dict[str, Any]:
    """Identity for an agent principal (``prn_…``) of an enabled workspace. Idempotent."""
    _require_enabled()
    async with async_session_factory() as platform:
        tenant_id, _ = await _workspace_tenants(platform, workspace_id)
        workspace_fid = await platform.scalar(
            text(
                "SELECT federation_id FROM federation_local_map "
                "WHERE id_type = 'workspace' AND workspace_id = CAST(:w AS uuid)"
            ),
            {"w": str(workspace_id)},
        )
        if workspace_fid is None:
            raise FederationError(
                "FED_E_IDENTITY_UNKNOWN", "federation is not enabled for this workspace"
            )
        fid = await platform.scalar(
            text(
                "SELECT federation_id FROM federation_local_map WHERE id_type = 'agent' "
                "AND workspace_id = CAST(:w AS uuid) AND principal_id = :p"
            ),
            {"w": str(workspace_id), "p": principal_id},
        )
        if fid is None:
            fid = await _create(
                platform,
                "agent",
                workspace_fid,
                tenant_id=tenant_id,
                workspace_id=str(workspace_id),
                principal_id=principal_id,
                actor=actor,
            )
        view = await _view(platform, fid)
        await platform.commit()
    return view


# ── key lifecycle (I-ID §5) ───────────────────────────────────────────────


async def rotate(fid: str, *, actor: str) -> dict[str, Any]:
    """New active key; the current ones retire after the overlap window."""
    _require_enabled()
    settings = get_settings()
    async with async_session_factory() as platform:
        row = await _identity_row(platform, fid)
        if row["status"] != "active":
            raise FederationError(
                "FED_E_IDENTITY_FROZEN", "identity is not active", status=row["status"]
            )
        now = _now()
        until = now + timedelta(days=settings.federation_key_overlap_days)
        pair = keys.generate()
        sealed, kek_id = keys.seal(pair)
        retiring = [
            r[0]
            for r in (
                await platform.execute(
                    text(
                        "SELECT key_id FROM federation_identity_keys WHERE federation_id = :f "
                        "AND state = 'active'"
                    ),
                    {"f": fid},
                )
            ).all()
        ]
        # The rotation object is signed by the outgoing key (I-ID §5.2).
        signer_view = {
            "federation_id": fid,
            "new_keys": [{"key_id": pair.key_id, "alg": "Ed25519", "public_key": pair.public_b64}],
            "retire": [{"key_id": k, "not_after": format_time(until)} for k in retiring],
            "rotation_serial": row["key_set_version"],
        }
        await _log(platform, fid, "identity.rotation", signer_view, actor=actor, signer=fid)
        await platform.execute(
            text(
                "UPDATE federation_identity_keys SET state = 'retiring', not_after = :u "
                "WHERE federation_id = :f AND state = 'active'"
            ),
            {"u": until, "f": fid},
        )
        await platform.execute(
            text(
                "INSERT INTO federation_identity_keys (key_id, federation_id, public_key, "
                "private_key_sealed, "
                "kek_id, state, not_before) VALUES (:k, :f, :p, :s, :kek, 'active', :n)"
            ),
            {
                "k": pair.key_id,
                "f": fid,
                "p": pair.public_b64,
                "s": sealed,
                "kek": kek_id,
                "n": now,
            },
        )
        await platform.execute(
            text(
                "UPDATE federation_identities SET key_set_version = key_set_version + 1 "
                "WHERE federation_id = :f"
            ),
            {"f": fid},
        )
        view = await _view(platform, fid)
        await platform.commit()
    return view


async def revoke_key(fid: str, key_id: str, *, actor: str) -> dict[str, Any]:
    """Immediate (I-ID §5.3). Signed by the parent, or by another active key of a subject."""
    _require_enabled()
    async with async_session_factory() as platform:
        row = await _identity_row(platform, fid)
        state = await platform.scalar(
            text(
                "SELECT state FROM federation_identity_keys WHERE federation_id = :f AND key_id "
                "= :k "
                "FOR UPDATE"
            ),
            {"f": fid, "k": key_id},
        )
        if state is None:
            raise FederationError("FED_E_IDENTITY_UNKNOWN", "key does not belong to this identity")
        if state != "revoked":
            await platform.execute(
                text(
                    "UPDATE federation_identity_keys SET state = 'revoked', revoked_at = NOW() "
                    "WHERE federation_id = :f AND key_id = :k"
                ),
                {"f": fid, "k": key_id},
            )
            await platform.execute(
                text(
                    "UPDATE federation_identities SET key_set_version = key_set_version + 1 "
                    "WHERE federation_id = :f"
                ),
                {"f": fid},
            )
            await _log(
                platform,
                fid,
                "identity.revocation",
                {"federation_id": fid, "key_id": key_id},
                actor=actor,
                signer=row["parent_id"] or fid,
            )
        view = await _view(platform, fid)
        await platform.commit()
    return view


async def set_status(fid: str, status: str, *, actor: str) -> dict[str, Any]:
    """freeze (frozen), unfreeze (active) or tombstone; recorded, not signed (I-ID §5.3)."""
    _require_enabled()
    actions = {
        "frozen": "identity.freeze",
        "active": "identity.unfreeze",
        "tombstoned": "identity.tombstone",
    }
    if status not in actions:
        raise FederationError("FED_E_IDENTITY_INVALID", "unsupported status change", status=status)
    async with async_session_factory() as platform:
        row = await _identity_row(platform, fid)
        if row["status"] == "tombstoned":
            raise FederationError("FED_E_IDENTITY_TOMBSTONED", "identity is tombstoned")
        if row["status"] != status:
            await platform.execute(
                text(
                    "UPDATE federation_identities SET status = :s, status_changed_at = NOW() "
                    "WHERE federation_id = :f"
                ),
                {"s": status, "f": fid},
            )
            if status == "tombstoned":
                await platform.execute(
                    text(
                        "UPDATE federation_addresses SET status = 'tombstoned', redirect_to = "
                        "NULL, "
                        "updated_at = NOW() WHERE target_id = :f AND status <> 'tombstoned'"
                    ),
                    {"f": fid},
                )
            await _log(
                platform,
                fid,
                actions[status],
                {"federation_id": fid, "status": status},
                actor=actor,
                signer=None,
            )
        view = await _view(platform, fid)
        await platform.commit()
    return view


# ── aliases (I-ID §4) ─────────────────────────────────────────────────────


async def set_alias(fid: str, handle: str, *, actor: str) -> dict[str, Any]:
    """Publish or rename an identity's handle. A person confirms it (route layer)."""
    _require_enabled()
    normal = normalize_handle(handle)
    async with async_session_factory() as platform:
        row = await _identity_row(platform, fid)
        if row["status"] == "tombstoned":
            raise FederationError("FED_E_IDENTITY_TOMBSTONED", "identity is tombstoned")
        scope = row["parent_id"] or ""
        current = (
            await platform.execute(
                text(
                    "SELECT address_id, handle FROM federation_addresses "
                    "WHERE target_id = :f AND status = 'active' FOR UPDATE"
                ),
                {"f": fid},
            )
        ).first()
        if current is not None and current[1] == normal:
            return await _view(platform, fid)
        try:
            async with platform.begin_nested():
                if current is not None:
                    # free the one-active-alias slot before inserting the new row
                    await platform.execute(
                        text(
                            "UPDATE federation_addresses SET status = 'tombstoned', updated_at "
                            "= NOW() "
                            "WHERE address_id = :a"
                        ),
                        {"a": current[0]},
                    )
                new_id = await platform.scalar(
                    text(
                        "INSERT INTO federation_addresses (level, scope_id, handle, "
                        "display_handle, "
                        "target_id, created_by) VALUES (:l, :s, :h, :d, :t, :a) RETURNING "
                        "address_id"
                    ),
                    {
                        "l": row["id_type"],
                        "s": scope,
                        "h": normal,
                        "d": handle,
                        "t": fid,
                        "a": actor,
                    },
                )
        except IntegrityError as exc:
            raise FederationError(
                "FED_E_ALIAS_TAKEN", "handle is taken (handles are never reused)"
            ) from exc
        if current is not None:
            await platform.execute(
                text(
                    "UPDATE federation_addresses SET status = 'redirect', redirect_to = :n, "
                    "updated_at = NOW() "
                    "WHERE address_id = :a"
                ),
                {"n": new_id, "a": current[0]},
            )
        await platform.execute(
            text(
                "INSERT INTO federation_address_history (address_id, action, old_handle, "
                "new_handle, "
                "target_id, actor) VALUES (:a, :act, :o, :n, :t, :who)"
            ),
            {
                "a": new_id,
                "act": "rename" if current else "create",
                "o": current[1] if current else None,
                "n": normal,
                "t": fid,
                "who": actor,
            },
        )
        view = await _view(platform, fid)
        await platform.commit()
    return view


async def resolve_alias(
    *, subject: str | None = None, workspace: str | None = None, agent: str | None = None
) -> dict[str, Any]:
    """AliasResolution (I-ID §4.3). Positions are given separately because the
    joined string format is still TBD-1. Ambiguity is reported, never resolved."""
    _require_enabled()
    parts = [
        (lvl, h)
        for lvl, h in (("subject", subject), ("workspace", workspace), ("agent", agent))
        if h is not None
    ]
    if not parts:
        raise FederationError("FED_E_ALIAS_INVALID", "give at least one handle")
    query = {lvl: normalize_handle(h) for lvl, h in parts}
    deepest = parts[-1][0]
    async with async_session_factory() as platform:
        rows = (
            (
                await platform.execute(
                    text(
                        "SELECT a.address_id, a.level, a.scope_id, a.handle, a.status, "
                        "a.redirect_to, a.target_id, "
                        "i.id_type, i.parent_id FROM federation_addresses a "
                        "JOIN federation_identities i ON i.federation_id = a.target_id "
                        "WHERE a.level = :l AND a.handle = :h"
                    ),
                    {"l": deepest, "h": query[deepest]},
                )
            )
            .mappings()
            .all()
        )
        candidates = []
        for r in rows:
            if await _ancestors_match(platform, r["target_id"], query, deepest):
                candidates.append(dict(r))
        result = await _resolution(platform, candidates)
    result["alias"] = {lvl: h for lvl, h in parts}
    return result


async def _ancestors_match(
    platform: AsyncSession, fid: str, query: dict[str, str], level: str
) -> bool:
    chain = {"agent": ["workspace", "subject"], "workspace": ["subject"], "subject": []}[level]
    current = fid
    for parent_level in chain:
        current = await platform.scalar(
            text("SELECT parent_id FROM federation_identities WHERE federation_id = :f"),
            {"f": current},
        )
        if parent_level in query:
            handles = (
                (
                    await platform.execute(
                        text(
                            "SELECT handle FROM federation_addresses WHERE target_id = :f AND "
                            "status <> 'tombstoned'"
                        ),
                        {"f": current},
                    )
                )
                .scalars()
                .all()
            )
            if query[parent_level] not in handles:
                return False
    return True


async def _resolution(platform: AsyncSession, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    def match(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "federation_id": r["target_id"],
            "id_type": r["id_type"],
            "parent_id": r["parent_id"],
        }

    live = [r for r in candidates if r["status"] != "tombstoned"]
    if not candidates:
        return {"status": "not_found", "code": "FED_E_ALIAS_NOT_FOUND", "matches": []}
    if not live:
        return {"status": "tombstoned", "code": "FED_E_ALIAS_TOMBSTONED", "matches": []}
    if len(live) > 1:
        return {
            "status": "ambiguous",
            "code": "FED_E_ALIAS_AMBIGUOUS",
            "matches": [match(r) for r in live],
        }
    only = live[0]
    if only["status"] == "redirect":
        target = (
            await platform.execute(
                text(
                    "SELECT display_handle, status FROM federation_addresses WHERE address_id = :a"
                ),
                {"a": only["redirect_to"]},
            )
        ).first()
        return {
            "status": "redirect",
            "matches": [match(only)],
            "redirect_to": target[0] if target and target[1] == "active" else None,
        }
    return {"status": "unique", "matches": [match(only)]}


# ── verifier adapters ─────────────────────────────────────────────────────


async def key_lookup(issuer: str, key_id: str) -> KeyView | None:
    async with async_session_factory() as platform:
        row = (
            await platform.execute(
                text(
                    "SELECT k.public_key, k.state, k.not_after, i.status, i.expires_at "
                    "FROM federation_identity_keys k JOIN federation_identities i "
                    "ON i.federation_id = k.federation_id WHERE k.federation_id = :f AND "
                    "k.key_id = :k"
                ),
                {"f": issuer, "k": key_id},
            )
        ).first()
    if row is None:
        return None
    status = row[3]
    if status == "active" and row[4] <= _now():
        status = "expired"
    return KeyView(
        public_raw=b64url_decode(row[0]), state=row[1], not_after=row[2], identity_status=status
    )


def replay_check_for(workspace_id: str):
    """A ReplayCheck that records accepted signatures for one workspace."""

    async def check(digest: str, expires_at: datetime) -> bool:
        async with async_session_factory() as platform:
            inserted = await platform.scalar(
                text(
                    "INSERT INTO federation_replay_guard (workspace_id, signature_digest, "
                    "expires_at) "
                    "VALUES (CAST(:w AS uuid), :d, :e) ON CONFLICT DO NOTHING RETURNING 1"
                ),
                {"w": str(workspace_id), "d": digest, "e": expires_at},
            )
            await platform.commit()
        return inserted is not None

    return check


def require_workspace_id(value: object) -> str:
    return require_type(value, "workspace")


__all__ = [
    "LocalRef",
    "enable_workspace",
    "register_agent",
    "resolve",
    "local_identity",
    "map_local",
    "sign_as",
    "rotate",
    "revoke_key",
    "set_status",
    "set_alias",
    "resolve_alias",
    "normalize_handle",
    "key_lookup",
    "replay_check_for",
    "require_workspace_id",
]
