"""Ed25519 federation keys and KEK sealing (ADR-013, I-ID §5.4).

Federation private keys are separate from ``tenant_signing_keys`` and are
stored only sealed with ``FEDERATION_KEK`` (AES-256-GCM, the key id bound as
associated data). They are opened in memory to sign and never logged,
returned, or put into an error.
"""

from __future__ import annotations

import base64
import binascii
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings
from app.services.federation.errors import FederationError
from app.services.federation.ids import b64url, b64url_decode, key_id_for

SIGNATURE_DOMAIN = b"veynod-federation-sig\x00"


@dataclass(frozen=True)
class KeyPair:
    private: Ed25519PrivateKey
    public_raw: bytes

    @property
    def key_id(self) -> str:
        return key_id_for(self.public_raw)

    @property
    def public_b64(self) -> str:
        return b64url(self.public_raw)

    def __repr__(self) -> str:  # never print key material
        return f"KeyPair(key_id={self.key_id!r})"


def generate() -> KeyPair:
    private = Ed25519PrivateKey.generate()
    return KeyPair(private, _public_raw(private))


def _public_raw(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def sign(private: Ed25519PrivateKey, message: bytes) -> bytes:
    return private.sign(SIGNATURE_DOMAIN + message)


def verify(public_raw: bytes, signature: bytes, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_raw).verify(signature, SIGNATURE_DOMAIN + message)
    except (InvalidSignature, ValueError):
        return False
    return True


def _kek() -> tuple[bytes, str]:
    settings = get_settings()
    try:
        kek = base64.b64decode(settings.federation_kek or "", validate=True)
    except (binascii.Error, ValueError):
        kek = b""
    if len(kek) != 32:
        raise FederationError(
            "FED_E_KEK_MISSING",
            "FEDERATION_KEK must be set to base64 of 32 random bytes to host federation keys",
        )
    return kek, settings.federation_kek_id


def seal(pair: KeyPair) -> tuple[str, str]:
    """(sealed private key, kek id) for storage."""
    kek, kek_id = _kek()
    raw = pair.private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    nonce = secrets.token_bytes(12)
    sealed = AESGCM(kek).encrypt(nonce, raw, pair.key_id.encode("ascii"))
    return f"v1.{b64url(nonce)}.{b64url(sealed)}", kek_id


def unseal(sealed: str, key_id: str) -> Ed25519PrivateKey:
    kek, _ = _kek()
    try:
        version, nonce, body = sealed.split(".")
        if version != "v1":
            raise ValueError("unknown seal version")
        raw = AESGCM(kek).decrypt(b64url_decode(nonce), b64url_decode(body), key_id.encode("ascii"))
        private = Ed25519PrivateKey.from_private_bytes(raw)
    except Exception as exc:  # never echo key material
        raise FederationError(
            "FED_E_KEK_MISSING", "a hosted federation key could not be opened"
        ) from exc
    if key_id_for(_public_raw(private)) != key_id:
        raise FederationError(
            "FED_E_KEK_MISSING", "a hosted federation key does not match its key id"
        )
    return private
