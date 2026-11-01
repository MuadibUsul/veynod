"""Canonical idempotency keys for cross-domain commands."""

from __future__ import annotations

import hashlib
from typing import Iterable


def canonical_key(*parts: object) -> str:
    """Stable SHA-256 over ordered, typed parts.

    Callers must use the same tuple ordering on retries.  The function is
    intentionally dependency-free so both services can compute it locally.
    """
    material = "\x1f".join(f"{type(part).__name__}:{part}" for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def command_key(namespace: str, command: str, *parts: object) -> str:
    return canonical_key(namespace, command, *parts)


def normalize_key(raw: str | None, *, max_length: int = 200) -> str | None:
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        return None
    if len(key) > max_length:
        raise ValueError(f"idempotency key must be <= {max_length} characters")
    if any(ord(char) < 32 for char in key):
        raise ValueError("idempotency key contains control characters")
    return key


def binding_matches(
    stored: Iterable[object], expected: Iterable[object]
) -> bool:
    return tuple(stored) == tuple(expected)


__all__ = [
    "binding_matches",
    "canonical_key",
    "command_key",
    "normalize_key",
]
