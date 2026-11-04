"""Self-certifying federation ids (I-ID §3, ADR-001a) and helper ids.

    digest = SHA-256("veynod-federation-id" 0x00 id_type 0x00 genesis_public_key_raw)
    id     = "fed:" id_type ":" lowercase(base32_nopad(digest[0:16]))

The id is derived from the identity's genesis Ed25519 public key (the raw 32
bytes, not text), never reused, and reveals nothing about local ids.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets

from app.services.federation.errors import FederationError

ID_TYPES = ("subject", "workspace", "agent")
_ID_RE = re.compile(r"^fed:(subject|workspace|agent):([a-z2-7]{26})$")
_KEY_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def b32(data: bytes) -> str:
    return base64.b32encode(data).decode("ascii").rstrip("=").lower()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def derive_federation_id(id_type: str, genesis_public_key_raw: bytes) -> str:
    if id_type not in ID_TYPES:
        raise FederationError("FED_E_TYPE_MISMATCH", f"unknown identity layer {id_type!r}")
    if len(genesis_public_key_raw) != 32:
        raise FederationError("FED_E_IDENTITY_INVALID", "an Ed25519 public key is 32 bytes")
    digest = hashlib.sha256(
        b"veynod-federation-id\x00" + id_type.encode("ascii") + b"\x00" + genesis_public_key_raw
    ).digest()
    return f"fed:{id_type}:{b32(digest[:16])}"


def parse_federation_id(value: object) -> tuple[str, str]:
    """(id_type, value); raises FED_E_IDENTITY_INVALID for anything malformed.

    Only the lowercase form is canonical: uppercase input is a format error.
    """
    match = _ID_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise FederationError("FED_E_IDENTITY_INVALID", "not a federation id")
    return match.group(1), match.group(2)


def require_type(value: object, id_type: str) -> str:
    """ID-INV-1: an argument declares the layer it expects."""
    actual, _ = parse_federation_id(value)
    if actual != id_type:
        raise FederationError(
            "FED_E_TYPE_MISMATCH", f"expected a {id_type} id", expected=id_type, got=actual
        )
    return value  # type: ignore[return-value]


def verify_derivation(federation_id: str, genesis_public_key_raw: bytes) -> bool:
    """ID-INV-7: a verifier recomputes the id from the claimed genesis key."""
    id_type, _ = parse_federation_id(federation_id)
    return derive_federation_id(id_type, genesis_public_key_raw) == federation_id


def key_id_for(public_key_raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(public_key_raw).hexdigest()


def is_key_id(value: object) -> bool:
    return isinstance(value, str) and _KEY_ID_RE.fullmatch(value) is not None


def new_connection_id() -> str:
    return "fcn_" + b32(secrets.token_bytes(16))[:26]


def new_object_id(prefix: str = "fobj") -> str:
    return f"{prefix}_" + b32(secrets.token_bytes(16))[:26]


def new_nonce() -> str:
    return b64url(secrets.token_bytes(18))
