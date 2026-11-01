"""Versioned event envelope shared by AgentGraph, Agent Runtime and Kernel.

The envelope is deliberately independent of any transport.  The current
systems use PostgreSQL outboxes and HTTP polling; later phases may add a bus
without changing the payload contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .identifiers import new_correlation_id, require_uuid

EVENT_SCHEMA_VERSION = 1

PRODUCERS = frozenset({"agentgraph", "agent-runtime", "veynod-kernel"})

EVENT_TYPES: tuple[str, ...] = (
    "ProductTaskCreated",
    "ProductTaskCancelled",
    "AgentRunCreated",
    "AgentRunStarted",
    "AgentRunPaused",
    "AgentRunResumed",
    "AgentRunCompleted",
    "AgentRunFailed",
    "AgentRunCancelled",
    "PlanCreated",
    "PlanRevised",
    "TaskDelegated",
    "KernelTaskCreated",
    "KernelTaskLeased",
    "KernelTaskBlocked",
    "KernelTaskCompleted",
    "KernelTaskFailed",
    "KernelTaskCancelled",
    "PatchSubmitted",
    "GateStarted",
    "GatePassed",
    "GateFailed",
    "MergeStarted",
    "MergeCommitted",
    "MergeConflict",
    "DeliveryReady",
    "DeliveryApproved",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class EventEnvelope:
    """At-least-once event contract.

    ``aggregate_version`` lets consumers reject out-of-order projections.
    ``payload_ref`` is used for large bodies; ``payload`` must remain small.
    """

    event_id: str
    event_type: str
    event_version: int
    tenant_id: str
    workspace_id: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    producer: str
    payload: dict[str, Any] = field(default_factory=dict)
    project_id: str | None = None
    occurred_at: str = field(default_factory=_now)
    correlation_id: str = field(default_factory=new_correlation_id)
    causation_id: str | None = None
    payload_ref: str | None = None

    @classmethod
    def create(
        cls,
        *,
        event_type: str,
        tenant_id: str,
        workspace_id: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        producer: str,
        payload: dict[str, Any] | None = None,
        project_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        payload_ref: str | None = None,
        event_version: int = EVENT_SCHEMA_VERSION,
    ) -> "EventEnvelope":
        return cls(
            event_id=str(uuid4()),
            event_type=event_type,
            event_version=event_version,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            project_id=project_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_version=aggregate_version,
            producer=producer,
            payload=dict(payload or {}),
            occurred_at=_now(),
            correlation_id=correlation_id or new_correlation_id(),
            causation_id=causation_id,
            payload_ref=payload_ref,
        )

    def validate(self) -> "EventEnvelope":
        require_uuid(self.event_id, "event_id")
        require_uuid(self.tenant_id, "tenant_id")
        require_uuid(self.workspace_id, "workspace_id")
        if self.event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event_type {self.event_type!r}")
        if self.event_version < 1:
            raise ValueError("event_version must be >= 1")
        if self.aggregate_type not in {
            "product_task",
            "agent_run",
            "workflow",
            "kernel_task",
            "submission",
            "merge",
            "delivery",
        }:
            raise ValueError(f"unknown aggregate_type {self.aggregate_type!r}")
        require_uuid(self.aggregate_id, "aggregate_id")
        if self.aggregate_version < 1:
            raise ValueError("aggregate_version must be >= 1")
        if self.producer not in PRODUCERS:
            raise ValueError(f"unknown producer {self.producer!r}")
        if not self.correlation_id:
            raise ValueError("correlation_id is required")
        try:
            datetime.fromisoformat(self.occurred_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("occurred_at must be ISO-8601") from exc
        if len(str(self.payload)) > 64_000:
            raise ValueError("payload is too large; use payload_ref")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_version": self.event_version,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "aggregate_version": self.aggregate_version,
            "occurred_at": self.occurred_at,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "producer": self.producer,
            "payload_ref": self.payload_ref,
            "payload": self.payload,
        }


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "EVENT_TYPES",
    "EventEnvelope",
    "PRODUCERS",
]
