"""Federation between independent deployments (F-11, docs/federation/ICD_I_I2I.md).

Each deployment keeps its own database, its own key-encryption key and its
own private keys. Nothing is shared but signed envelopes and public identity
records:

* ``public_records(fid)`` serves the signed identity records of a hosted
  workspace (and its subject), so another deployment can learn its public
  keys;
* ``import_records`` checks a peer's records before trusting them: the id must
  derive from its genesis key (self-certifying), and every record must be
  signed by a key the chain already vouches for. The peer is stored with
  ``hosted = false`` and public keys only;
* ``register(fid, base_url)``: an operator names where a peer lives; only
  hosts in ``federation_peer_hosts`` may be reached (default: none, so a
  closed network sends nothing out, F-022);
* ``deliver(envelope)``: the transport for a workspace that is not hosted
  here, over HTTPS to the peer's ``/v1/federation/inbox``;
* ``accept(envelope)``: what that inbox does: the same checks as for a local
  envelope, plus a refresh of the issuer's keys when they rotated.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_factory
from app.services.federation import keys
from app.services.federation.canonical import sha256_hex
from app.services.federation.envelope import parse_time, signing_bytes
from app.services.federation.errors import CODES, FederationError
from app.services.federation.ids import b64url_decode, parse_federation_id, verify_derivation

log = logging.getLogger("agentgraph.federation")

RECORD_TYPES = ("identity.record", "identity.rotation", "identity.revocation")


def host_allowed(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").lower()
    if not host:
        return False
    for pattern in get_settings().federation_peer_hosts:
        pattern = pattern.lower().strip()
        if pattern.startswith("*.") and (host == pattern[2:] or host.endswith(pattern[1:])):
            return True
        if host == pattern:
            return True
    return False


def _require_egress(base_url: str) -> None:
    if not host_allowed(base_url):
        raise FederationError(
            "FED_E_EGRESS_DISABLED",
            "this deployment does not reach that host for federation",
            host=urlsplit(base_url).hostname,
        )


# ── serving and importing identity records ────────────────────────────────


async def public_records(fid: str) -> dict[str, Any]:
    """The signed records of a hosted identity and its ancestors, parents first."""
    parse_federation_id(fid)
    chain: list[str] = []
    async with async_session_factory() as platform:
        current: str | None = fid
        while current:
            row = (
                (
                    await platform.execute(
                        text(
                            "SELECT federation_id, parent_id, hosted FROM federation_identities "
                            "WHERE federation_id = :f"
                        ),
                        {"f": current},
                    )
                )
                .mappings()
                .first()
            )
            if row is None or not row["hosted"]:
                raise FederationError("FED_E_IDENTITY_UNKNOWN", "not an identity hosted here")
            chain.insert(0, row["federation_id"])
            current = row["parent_id"]
        records = []
        for member in chain:
            rows = (
                (
                    await platform.execute(
                        text(
                            "SELECT envelope FROM federation_identity_log WHERE federation_id = :f "
                            "AND envelope IS NOT NULL ORDER BY serial"
                        ),
                        {"f": member},
                    )
                )
                .scalars()
                .all()
            )
            records.append(
                {
                    "federation_id": member,
                    "envelopes": [r if isinstance(r, dict) else json.loads(r) for r in rows],
                }
            )
    return {"federation_id": fid, "chain": records}


def _verify_with(envelope: dict[str, Any], public_raw: bytes) -> None:
    if sha256_hex(envelope.get("payload")) != envelope.get("payload_sha256"):
        raise FederationError(
            "FED_E_REMOTE_INVALID", "an identity record does not match its digest"
        )
    try:
        signature = b64url_decode(envelope["signature"])
    except (KeyError, ValueError) as exc:
        raise FederationError("FED_E_REMOTE_INVALID", "an identity record is unsigned") from exc
    if not keys.verify(public_raw, signature, signing_bytes(envelope)):
        raise FederationError("FED_E_REMOTE_INVALID", "an identity record's signature is wrong")


async def _parent_keys(platform: AsyncSession, parent: str) -> dict[str, bytes]:
    rows = (
        await platform.execute(
            text(
                "SELECT key_id, public_key FROM federation_identity_keys WHERE federation_id = :f "
                "AND state IN ('active', 'retiring')"
            ),
            {"f": parent},
        )
    ).all()
    return {r[0]: b64url_decode(r[1]) for r in rows}


async def import_records(document: dict[str, Any]) -> str:
    """Check and store another deployment's identity chain. Returns the leaf id."""
    chain = document.get("chain") if isinstance(document, dict) else None
    if not isinstance(chain, list) or not chain or len(chain) > 3:
        raise FederationError("FED_E_REMOTE_INVALID", "an identity chain has 1 to 3 members")
    leaf = None
    async with async_session_factory() as platform:
        for member in chain:
            fid = member.get("federation_id") if isinstance(member, dict) else None
            envelopes = member.get("envelopes") if isinstance(member, dict) else None
            if not isinstance(fid, str) or not isinstance(envelopes, list) or not envelopes:
                raise FederationError("FED_E_REMOTE_INVALID", "a chain member needs its records")
            leaf = await _import_one(platform, fid, envelopes)
        await platform.commit()
    return leaf


async def _import_one(platform: AsyncSession, fid: str, envelopes: list[Any]) -> str:
    id_type, _ = parse_federation_id(fid)
    existing = (
        (
            await platform.execute(
                text(
                    "SELECT hosted, key_set_version FROM federation_identities "
                    "WHERE federation_id = :f"
                ),
                {"f": fid},
            )
        )
        .mappings()
        .first()
    )
    if existing is not None and existing["hosted"]:
        raise FederationError("FED_E_REMOTE_INVALID", "an identity hosted here is never imported")
    first = envelopes[0]
    if not isinstance(first, dict) or first.get("type") not in RECORD_TYPES:
        raise FederationError("FED_E_REMOTE_INVALID", "records start with identity.record")
    view = first.get("payload") or {}
    if view.get("federation_id") != fid or view.get("id_type") != id_type:
        raise FederationError("FED_E_REMOTE_INVALID", "the record describes another identity")
    genesis = b64url_decode(view.get("genesis_public_key", ""))
    # Self-certifying: the id is the hash of the genesis key, whoever serves it.
    if not verify_derivation(fid, genesis):
        raise FederationError("FED_E_REMOTE_INVALID", "the id does not derive from its key")
    parent = view.get("parent_id")
    if id_type == "subject":
        if parent is not None:
            raise FederationError("FED_E_REMOTE_INVALID", "a subject has no parent")
        trusted = {keys_id(genesis): genesis}
    else:
        if not isinstance(parent, str):
            raise FederationError("FED_E_REMOTE_INVALID", "the parent comes first in the chain")
        trusted = await _parent_keys(platform, parent)
        if not trusted:
            raise FederationError("FED_E_REMOTE_INVALID", "the parent comes first in the chain")
    latest = view
    for envelope in envelopes:
        if not isinstance(envelope, dict) or envelope.get("type") not in RECORD_TYPES:
            raise FederationError("FED_E_REMOTE_INVALID", "not an identity record")
        signer_key = trusted.get(envelope.get("key_id"))
        if signer_key is None:
            raise FederationError("FED_E_REMOTE_INVALID", "a record is signed by an unknown key")
        _verify_with(envelope, signer_key)
        payload = envelope.get("payload") or {}
        if payload.get("federation_id") != fid:
            raise FederationError("FED_E_REMOTE_INVALID", "a record describes another identity")
        latest = payload
        if id_type == "subject":
            # A subject's later records are signed by its own current keys.
            trusted = {
                k["key_id"]: b64url_decode(k["public_key"])
                for k in payload.get("keys", [])
                if k.get("state") in ("active", "retiring")
            } or trusted
    version = int(latest.get("key_set_version") or 1)
    if existing is not None and version < existing["key_set_version"]:
        raise FederationError("FED_E_REMOTE_INVALID", "an older key set than the one on file")
    status = latest.get("status", "active")
    if existing is None:
        await platform.execute(
            text(
                "INSERT INTO federation_identities (federation_id, id_type, parent_id, "
                "genesis_public_key, status, key_set_version, display_alias, hosted, issued_at, "
                "expires_at) VALUES (:f, :t, :p, :g, :s, :v, :a, FALSE, :i, :e)"
            ),
            {
                "f": fid,
                "t": id_type,
                "p": parent,
                "g": view["genesis_public_key"],
                "s": status,
                "v": version,
                "a": latest.get("display_alias"),
                "i": parse_time(latest["issued_at"]),
                "e": parse_time(latest["expires_at"]),
            },
        )
    else:
        await platform.execute(
            text(
                "UPDATE federation_identities SET status = :s, key_set_version = :v, "
                "display_alias = :a, expires_at = :e WHERE federation_id = :f AND NOT hosted"
            ),
            {
                "f": fid,
                "s": status,
                "v": version,
                "a": latest.get("display_alias"),
                "e": parse_time(latest["expires_at"]),
            },
        )
    for key in latest.get("keys", []):
        raw = b64url_decode(key["public_key"])
        if keys_id(raw) != key.get("key_id"):
            raise FederationError("FED_E_REMOTE_INVALID", "a key id does not match its key")
        await platform.execute(
            text(
                "INSERT INTO federation_identity_keys (key_id, federation_id, public_key, state, "
                "is_genesis, not_before, not_after, revoked_at) VALUES (:k, :f, :p, :s, :g, :nb, "
                ":na, :ra) ON CONFLICT (key_id) DO UPDATE "
                "SET state = EXCLUDED.state, not_after = EXCLUDED.not_after, revoked_at = "
                "EXCLUDED.revoked_at WHERE federation_identity_keys.federation_id = :f"
            ),
            {
                "k": key["key_id"],
                "f": fid,
                "p": key["public_key"],
                "s": key.get("state", "active"),
                "g": raw == genesis,
                "nb": parse_time(key["not_before"]),
                "na": parse_time(key["not_after"]) if key.get("not_after") else None,
                "ra": datetime.now(timezone.utc) if key.get("state") == "revoked" else None,
            },
        )
    return fid


def keys_id(public_raw: bytes) -> str:
    from app.services.federation.ids import key_id_for

    return key_id_for(public_raw)


# ── peers ─────────────────────────────────────────────────────────────────


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=get_settings().federation_remote_timeout_seconds)


async def _fetch_records(base_url: str, fid: str) -> dict[str, Any]:
    _require_egress(base_url)
    try:
        async with _client() as http:
            answer = await http.get(f"{base_url.rstrip('/')}/v1/federation/public/identities/{fid}")
    except httpx.HTTPError as exc:
        raise FederationError("FED_E_UNREACHABLE", "the peer deployment did not answer") from exc
    if answer.status_code != 200:
        raise FederationError(
            "FED_E_REMOTE_INVALID",
            "the peer deployment has no such identity",
            status=answer.status_code,
        )
    return answer.json()


async def register(fid: str, base_url: str, *, operator: str) -> dict[str, Any]:
    """An operator names where a peer workspace lives; its identity is imported."""
    from app.services.federation import identity

    id_type, _ = parse_federation_id(fid)
    if id_type != "workspace":
        raise FederationError("FED_E_TYPE_MISMATCH", "register a workspace")
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FederationError("FED_E_REMOTE_INVALID", "base_url is http(s)://host[:port]")
    if get_settings().is_production and parts.scheme != "https":
        raise FederationError("FED_E_REMOTE_INVALID", "production peers use https")
    leaf = await import_records(await _fetch_records(base_url, fid))
    if leaf != fid:
        raise FederationError("FED_E_REMOTE_INVALID", "the peer served another identity")
    async with async_session_factory() as platform:
        await platform.execute(
            text(
                "INSERT INTO federation_remote_endpoints (federation_id, base_url, registered_by, "
                "refreshed_at) VALUES (:f, :u, :by, NOW()) ON CONFLICT (federation_id) DO UPDATE "
                "SET base_url = EXCLUDED.base_url, registered_by = EXCLUDED.registered_by, "
                "refreshed_at = NOW()"
            ),
            {"f": fid, "u": base_url.rstrip("/"), "by": f"operator:{operator}"},
        )
        await platform.commit()
    return {"federation_id": fid, "base_url": base_url.rstrip("/"), **await identity.resolve(fid)}


async def endpoint_of(fid: str) -> str | None:
    async with async_session_factory() as platform:
        return await platform.scalar(
            text("SELECT base_url FROM federation_remote_endpoints WHERE federation_id = :f"),
            {"f": fid},
        )


async def refresh(fid: str) -> bool:
    """Re-import a registered peer's records (its keys may have rotated)."""
    base = await endpoint_of(fid)
    if base is None:
        return False
    await import_records(await _fetch_records(base, fid))
    async with async_session_factory() as platform:
        await platform.execute(
            text(
                "UPDATE federation_remote_endpoints SET refreshed_at = NOW() "
                "WHERE federation_id = :f"
            ),
            {"f": fid},
        )
        await platform.commit()
    return True


async def deliver(envelope: dict[str, Any]) -> dict[str, Any]:
    """Send an envelope to a workspace on another deployment."""
    audience = envelope.get("audience")
    base = await endpoint_of(audience) if isinstance(audience, str) else None
    if base is None:
        raise FederationError("FED_E_IDENTITY_UNKNOWN", "no such workspace here or at a known peer")
    _require_egress(base)
    try:
        async with _client() as http:
            answer = await http.post(f"{base}/v1/federation/inbox", json=envelope)
    except httpx.HTTPError as exc:
        raise FederationError("FED_E_UNREACHABLE", "the peer deployment did not answer") from exc
    try:
        body = answer.json()
    except ValueError:
        body = {}
    if answer.status_code < 300:
        return body
    error = body.get("error", {}) if isinstance(body, dict) else {}
    code = (error.get("details") or {}).get("federation_code")
    if code in CODES and code not in ("FED_E_DISABLED",):
        raise FederationError(code, error.get("message") or "refused by the peer")
    raise FederationError("FED_E_UNREACHABLE", "the peer deployment could not take it")


async def accept(envelope: Any) -> dict[str, Any]:
    """The inbox: an envelope from another deployment, for a workspace here."""
    from app.services.federation import grants, messaging

    if not isinstance(envelope, dict):
        raise FederationError("FED_E_ENVELOPE_INVALID", "an envelope is a JSON object")
    audience = envelope.get("audience")
    messaging.buckets.take(f"inbox:{audience}", messaging._claimed_issuer(envelope))
    local = grants.LocalTransport()
    try:
        return await local.deliver_here(envelope)
    except FederationError as exc:
        issuer = envelope.get("issuer")
        if exc.code in ("FED_E_IDENTITY_UNKNOWN", "FED_E_KEY_NOT_CURRENT") and isinstance(
            issuer, str
        ):
            if await refresh(issuer):
                return await local.deliver_here(envelope)
        raise
