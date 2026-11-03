from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

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
        event_type="kernel.lease.granted",
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
    assert data["event_type"] == "kernel.lease.granted"
    assert data["event_id"].startswith("evt_")
    assert data["schema_version"] == 1
    assert data["event_version"] == 1
    assert data["aggregate_version"] == 3
    assert data["correlation_id"] == "corr-1"
    assert data["payload"] == {"task_id": "t-1"}
    schema = json.loads(
        (Path(__file__).parents[1] / "event-envelope.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(data)


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


def test_diagram_event_uses_actual_opaque_ids() -> None:
    event = EventEnvelope.create(
        event_type="DiagramChunkCommitted",
        tenant_id="ten_demo",
        workspace_id=_uuid(),
        aggregate_type="diagram_chunk",
        aggregate_id="42/api",
        aggregate_version=2,
        producer="agentgraph",
    ).validate()
    assert event.to_dict()["aggregate_id"] == "42/api"


def test_identity_chain_requires_stable_ids() -> None:
    chain = IdentityChain(tenant_id=_uuid(), workspace_id=_uuid()).validate()
    assert chain.product_task_id is None
    assert IdentityChain(tenant_id="ten_demo", workspace_id=_uuid()).validate()
    with pytest.raises(ValueError, match="tenant_id"):
        IdentityChain(tenant_id="", workspace_id=_uuid()).validate()
    assert IdentityChain(
        tenant_id="ten_demo", workspace_id=_uuid(), diagram_id=7,
        chunk_key="api", product_task_key="DG-plan-api", placement_id="plc_123",
    ).validate().to_dict()["placement_id"] == "plc_123"
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
