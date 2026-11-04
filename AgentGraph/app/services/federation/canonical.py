"""Canonical JSON for signing: the RFC 8785 (JCS) subset federation uses.

Objects sort their keys by UTF-16 code units, there is no insignificant
whitespace, strings are UTF-8 with JSON's minimal escaping, and numbers are
integers only (|n| <= 2**53 - 1) so every implementation serialises them the
same way. Floats, NaN and non-string keys are rejected rather than coerced
(ICD_I_MSG §1, ADR-003).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

MAX_SAFE_INT = 2**53 - 1


class CanonicalError(ValueError):
    pass


def _check(value: Any, depth: int, max_depth: int) -> None:
    if depth > max_depth:
        raise CanonicalError(f"nesting deeper than {max_depth}")
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INT:
            raise CanonicalError("integer outside the safe range")
        return
    if isinstance(value, float):
        raise CanonicalError("floating-point numbers are not allowed")
    if isinstance(value, list):
        for item in value:
            _check(item, depth + 1, max_depth)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalError("object keys must be strings")
            _check(item, depth + 1, max_depth)
        return
    raise CanonicalError(f"unsupported type {type(value).__name__}")


def _encode(value: Any) -> str:
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        return (
            "{"
            + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _encode(v) for k, v in items)
            + "}"
        )
    if isinstance(value, list):
        return "[" + ",".join(_encode(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def canonical_bytes(value: Any, *, max_depth: int = 32) -> bytes:
    _check(value, 0, max_depth)
    return _encode(value).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
