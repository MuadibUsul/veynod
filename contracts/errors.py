"""Cross-domain error codes.

These mirror the error classes used by the API but are stable across
AgentGraph, Agent Runtime and Kernel.  Services may add local details in
``details``; they must not invent a new class for the same condition.
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    # identity / authorization
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    TENANT_MISMATCH = "tenant_mismatch"
    WORKSPACE_MISMATCH = "workspace_mismatch"
    # tasks / lifecycle
    TASK_NOT_FOUND = "task_not_found"
    INVALID_STATE_TRANSITION = "invalid_state_transition"
    VERSION_CONFLICT = "version_conflict"
    LEASE_NOT_ACTIVE = "lease_not_active"
    LEASE_EXPIRED = "lease_expired"
    LEASE_LOST = "lease_lost"
    FENCED_OUT = "fenced_out"
    # submissions / gate
    SUBMISSION_NOT_FOUND = "submission_not_found"
    IDEMPOTENCY_KEY_CONFLICT = "idempotency_key_conflict"
    PATCH_BINDING_CONFLICT = "patch_binding_conflict"
    GATE_REJECTED = "gate_rejected"
    GATE_FAILED = "gate_failed"
    GATE_EVIDENCE_MISSING = "gate_evidence_missing"
    # merge / delivery
    MERGE_CONFLICT = "merge_conflict"
    MERGE_FENCE_LOST = "merge_fence_lost"
    MERGE_REJECTED = "merge_rejected"
    DELIVERY_NOT_READY = "delivery_not_ready"
    # infrastructure
    RATE_LIMITED = "rate_limited"
    BACKPRESSURE = "backpressure"
    INTERNAL_ERROR = "internal_error"


RETRYABLE: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.RATE_LIMITED,
        ErrorCode.BACKPRESSURE,
        ErrorCode.INTERNAL_ERROR,
    }
)


__all__ = ["ErrorCode", "RETRYABLE"]
