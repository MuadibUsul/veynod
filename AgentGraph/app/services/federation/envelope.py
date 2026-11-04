"""The federation envelope: build and verify (ICD_I_MSG §2–§3, F-02b).

``verify_envelope`` runs steps 1–7 of ICD_I_MSG §3 in order and raises before
any side effect. It is storage-agnostic: the caller passes a key lookup and a
replay check (``identity.key_lookup`` / ``identity.replay_check`` are the
database-backed ones).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import get_settings
from app.services.federation import keys
from app.services.federation.canonical import CanonicalError, canonical_bytes, sha256_hex
from app.services.federation.errors import FederationError
from app.services.federation.ids import (
    b64url,
    b64url_decode,
    is_key_id,
    new_nonce,
    new_object_id,
    parse_federation_id,
)

PROTOCOL = "veynod-federation"
PROTOCOL_VERSION = "0.1"
MAX_ENVELOPE_BYTES = 64 * 1024  # P1 placeholder (ICD_I_MSG §2)
MAX_LIFETIME = timedelta(hours=24)  # P1 placeholder
MAX_PAYLOAD_DEPTH = 16
PUBLIC_TYPES = frozenset({"identity.record", "identity.rotation", "identity.revocation"})
KNOWN_TYPES = PUBLIC_TYPES | frozenset(
    {
        "identity.lookup",
        "identity.challenge",
        "identity.response",
        "grant.request",
        "grant.decision",
        "grant.update",
        "grant.revoke",
        "chat.message",
        "delivery.receipt",
    }
)
FIELDS = (
    "protocol",
    "protocol_version",
    "type",
    "object_id",
    "issuer",
    "key_id",
    "audience",
    "issued_at",
    "expires_at",
    "nonce",
    "payload",
    "payload_sha256",
    "signature",
)
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
        raise FederationError("FED_E_ENVELOPE_INVALID", "times are RFC 3339 UTC with seconds and Z")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def signing_bytes(envelope: dict[str, Any]) -> bytes:
    return canonical_bytes({k: v for k, v in envelope.items() if k != "signature"})


def build_envelope(
    *,
    type: str,
    issuer: str,
    key_id: str,
    private: Ed25519PrivateKey,
    audience: str | None,
    payload: dict[str, Any],
    ttl: timedelta = timedelta(hours=1),
    object_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if type not in KNOWN_TYPES:
        raise FederationError("FED_E_ENVELOPE_INVALID", "unknown envelope type", type=type)
    if audience is None and type not in PUBLIC_TYPES:
        raise FederationError("FED_E_AUDIENCE_MISMATCH", "this object type needs an audience")
    now = now or datetime.now(timezone.utc)
    envelope: dict[str, Any] = {
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "type": type,
        "object_id": object_id or new_object_id("fmsg" if type == "chat.message" else "fobj"),
        "issuer": issuer,
        "key_id": key_id,
        "audience": audience,
        "issued_at": format_time(now),
        "expires_at": format_time(now + ttl),
        "nonce": new_nonce(),
        "payload": payload,
        "payload_sha256": sha256_hex(payload),
    }
    envelope["signature"] = b64url(keys.sign(private, signing_bytes(envelope)))
    return envelope


@dataclass(frozen=True)
class KeyView:
    """What a verifier needs to know about one key of one identity."""

    public_raw: bytes
    state: str  # active | retiring | revoked | expired
    not_after: datetime | None
    identity_status: str  # active | frozen | recovering | suspended | tombstoned


KeyLookup = Callable[[str, str], Awaitable[KeyView | None]]
ReplayCheck = Callable[[str, datetime], Awaitable[bool]]


@dataclass(frozen=True)
class Verified:
    envelope: dict[str, Any]
    issuer: str
    type: str
    payload: dict[str, Any]
    signature_digest: str


def _structure(envelope: object) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise FederationError("FED_E_ENVELOPE_INVALID", "an envelope is a JSON object")
    try:
        size = len(canonical_bytes(envelope))
    except CanonicalError as exc:
        raise FederationError("FED_E_ENVELOPE_INVALID", str(exc)) from exc
    if size > MAX_ENVELOPE_BYTES:
        raise FederationError(
            "FED_E_TOO_LARGE", "envelope exceeds the size limit", limit=MAX_ENVELOPE_BYTES
        )
    if set(envelope) != set(FIELDS):
        raise FederationError(
            "FED_E_ENVELOPE_INVALID",
            "unknown or missing envelope fields",
            missing=sorted(set(FIELDS) - set(envelope)),
            unknown=sorted(set(envelope) - set(FIELDS)),
        )
    if envelope["protocol"] != PROTOCOL or envelope["protocol_version"] != PROTOCOL_VERSION:
        raise FederationError("FED_E_PROTOCOL_VERSION", "unsupported protocol version")
    if envelope["type"] not in KNOWN_TYPES:
        raise FederationError("FED_E_ENVELOPE_INVALID", "unknown envelope type")
    if not isinstance(envelope["payload"], dict):
        raise FederationError("FED_E_ENVELOPE_INVALID", "payload is a JSON object")
    try:
        canonical_bytes(envelope["payload"], max_depth=MAX_PAYLOAD_DEPTH)
    except CanonicalError as exc:
        raise FederationError("FED_E_ENVELOPE_INVALID", str(exc)) from exc
    for field in ("object_id", "nonce", "signature", "payload_sha256"):
        if not isinstance(envelope[field], str) or not envelope[field]:
            raise FederationError("FED_E_ENVELOPE_INVALID", f"{field} is required")
    if len(envelope["nonce"]) < 22:
        raise FederationError("FED_E_ENVELOPE_INVALID", "nonce is too short")
    if not is_key_id(envelope["key_id"]):
        raise FederationError("FED_E_ENVELOPE_INVALID", "malformed key_id")
    parse_federation_id(envelope["issuer"])
    if envelope["audience"] is not None:
        parse_federation_id(envelope["audience"])
    return envelope


async def verify_envelope(
    envelope: object,
    *,
    local_workspace_fid: str,
    key_lookup: KeyLookup,
    replay_check: ReplayCheck,
    now: datetime | None = None,
    skew_seconds: int | None = None,
) -> Verified:
    env = _structure(envelope)  # 1
    # 2 audience, before anything that depends on the sender
    if env["audience"] is None:
        if env["type"] not in PUBLIC_TYPES:
            raise FederationError("FED_E_AUDIENCE_MISMATCH", "this object type needs an audience")
    elif env["audience"] != local_workspace_fid:
        raise FederationError("FED_E_AUDIENCE_MISMATCH", "envelope is addressed elsewhere")
    # 3 time window
    now = now or datetime.now(timezone.utc)
    skew = timedelta(
        seconds=get_settings().federation_clock_skew_seconds
        if skew_seconds is None
        else skew_seconds
    )
    issued, expires = parse_time(env["issued_at"]), parse_time(env["expires_at"])
    if expires <= issued or expires - issued > MAX_LIFETIME:
        raise FederationError("FED_E_ENVELOPE_INVALID", "envelope lifetime out of range")
    if now + skew < issued:
        raise FederationError("FED_E_NOT_YET_VALID", "envelope is from the future")
    if now >= expires + skew:
        raise FederationError("FED_E_EXPIRED", "envelope has expired")
    # 4 payload digest
    if sha256_hex(env["payload"]) != env["payload_sha256"]:
        raise FederationError("FED_E_BAD_DIGEST", "payload does not match payload_sha256")
    # 5 issuer and key state
    key = await key_lookup(env["issuer"], env["key_id"])
    if key is None:
        raise FederationError(
            "FED_E_IDENTITY_UNKNOWN",
            "unknown issuer or key",
            issuer=env["issuer"],
            key_id=env["key_id"],
        )
    if key.identity_status == "tombstoned":
        raise FederationError("FED_E_IDENTITY_TOMBSTONED", "issuer identity is tombstoned")
    if key.identity_status != "active":
        raise FederationError(
            "FED_E_IDENTITY_FROZEN", "issuer identity is not active", status=key.identity_status
        )
    if key.state == "revoked":
        raise FederationError("FED_E_KEY_REVOKED", "signing key is revoked")
    if key.state == "retiring":
        if key.not_after is None or now >= key.not_after:
            raise FederationError("FED_E_KEY_NOT_CURRENT", "retired key is past its overlap window")
    elif key.state != "active":
        raise FederationError("FED_E_KEY_NOT_CURRENT", "signing key is not current")
    # 6 signature
    try:
        signature = b64url_decode(env["signature"])
    except ValueError as exc:
        raise FederationError("FED_E_BAD_SIGNATURE", "signature is not base64url") from exc
    if not keys.verify(key.public_raw, signature, signing_bytes(env)):
        raise FederationError("FED_E_BAD_SIGNATURE", "signature does not verify")
    # 7 replay
    digest = hashlib.sha256(signature).hexdigest()
    if not await replay_check(digest, expires + skew):
        raise FederationError("FED_E_REPLAY", "envelope was already accepted")
    return Verified(env, env["issuer"], env["type"], env["payload"], digest)
