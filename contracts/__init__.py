"""Veynod cross-domain contracts (Phase 1 foundation).

This package is intentionally dependency-free.  It is the shared vocabulary
for AgentGraph, the future Agent Runtime and the Kernel; it does not read or
write either database.

Status: Proposed.  Phase 1 wires the contracts into both services and adds
consumer/projection tests; this package alone does not prove end-to-end use.
"""

from .errors import ErrorCode, RETRYABLE
from .events import EVENT_SCHEMA_VERSION, EVENT_TYPES, EventEnvelope, PRODUCERS
from .identifiers import (
    IDENTIFIER_FIELDS,
    IdentityChain,
    is_uuid,
    new_correlation_id,
    new_id,
    require_uuid,
    require_opaque_id,
)
from .idempotency import (
    binding_matches,
    canonical_key,
    command_key,
    normalize_key,
)
from .work_scope import WorkScope, normalize_path

__all__ = [
    "ErrorCode",
    "RETRYABLE",
    "EVENT_SCHEMA_VERSION",
    "EVENT_TYPES",
    "EventEnvelope",
    "PRODUCERS",
    "IDENTIFIER_FIELDS",
    "IdentityChain",
    "is_uuid",
    "new_correlation_id",
    "new_id",
    "require_uuid",
    "require_opaque_id",
    "binding_matches",
    "canonical_key",
    "command_key",
    "normalize_key",
    "WorkScope",
    "normalize_path",
]
