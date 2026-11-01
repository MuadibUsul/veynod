from __future__ import annotations

from uuid import uuid4

import pytest

from contracts import (
    ErrorCode,
    EventEnvelope,
    IdentityChain,
    RETRYABLE,
    canonical_key,
    normalize_key,
)


def _uuid() -> str:
    return str(uuid4())


def test_event_envelope_round_trip() -> None:
    event = EventEnvelope.create(
        event_type="KernelTaskLeased",
        tenant_id=_uuid(),
        workspace_id=_uuid(),
        aggregate_type="kernel_task",
        aggregate_id=_uuid(),
        aggregate_version=3,
        producer="veynod-kernel",
        payload={"task_id": "t-1"},
        correlation_id="corr-1",
    ).validate()
    data = event.to_dict()
    assert data["event_type"] == "KernelTaskLeased"
    assert data["event_version"] == 1
    assert data["aggregate_version"] == 3
    assert data["correlation_id"] == "corr-1"
    assert data["payload"] == {"task_id": "t-1"}


def test_event_envelope_rejects_unknown_type_and_bad_version() -> None:
    with pytest.raises(ValueError, match="unknown event_type"):
        EventEnvelope.create(
            event_type="NotARealEvent",
            tenant_id=_uuid(),
            workspace_id=_uuid(),
            aggregate_type="kernel_task",
            aggregate_id=_uuid(),
            aggregate_version=1,
            producer="veynod-kernel",
        ).validate()
    with pytest.raises(ValueError, match="aggregate_version"):
        EventEnvelope.create(
            event_type="KernelTaskCreated",
            tenant_id=_uuid(),
            workspace_id=_uuid(),
            aggregate_type="kernel_task",
            aggregate_id=_uuid(),
            aggregate_version=0,
            producer="veynod-kernel",
        ).validate()


def test_identity_chain_requires_stable_ids() -> None:
    chain = IdentityChain(tenant_id=_uuid(), workspace_id=_uuid()).validate()
    assert chain.product_task_id is None
    with pytest.raises(ValueError, match="tenant_id"):
        IdentityChain(tenant_id="not-a-uuid", workspace_id=_uuid()).validate()
    with pytest.raises(ValueError, match="commit_sha"):
        IdentityChain(
            tenant_id=_uuid(), workspace_id=_uuid(), commit_sha="zzzz"
        ).validate()


def test_idempotency_key_is_stable_and_material_sensitive() -> None:
    first = canonical_key("kernel", "submit_patch", "task-1", "lease-1", "abc")
    second = canonical_key("kernel", "submit_patch", "task-1", "lease-1", "abc")
    other = canonical_key("kernel", "submit_patch", "task-1", "lease-1", "def")
    assert first == second
    assert first != other
    assert normalize_key("  abc  ") == "abc"
    assert normalize_key("   ") is None
    with pytest.raises(ValueError, match="control"):
        normalize_key("a\x00b")
    with pytest.raises(ValueError, match="200"):
        normalize_key("x" * 201)


def test_error_retryable_set_is_explicit() -> None:
    assert ErrorCode.RATE_LIMITED in RETRYABLE
    assert ErrorCode.LEASE_LOST not in RETRYABLE
