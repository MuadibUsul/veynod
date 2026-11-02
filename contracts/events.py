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

from .identifiers import new_correlation_id, require_opaque_id, require_uuid

EVENT_SCHEMA_VERSION = 1

PRODUCERS = frozenset({"agentgraph", "agent-runtime", "veynod-kernel"})

EVENT_TYPES: tuple[str, ...] = (
    "diagram.confirmed",
    "diagram.frozen",
    "diagram.change.proposed",
    "diagram.change.approved",
    "diagram.change.rejected",
    "chunk.ready",
    "chunk.blocked",
    "chunk.placement.requested",
    "chunk.placement.offered",
    "chunk.execution.claimed",
    "chunk.execution.started",
    "chunk.execution.heartbeat",
    "chunk.execution.failed",
    "runtime.run.started",
    "runtime.run.waiting_external",
    "runtime.run.woken",
    "runtime.run.failed",
    "runtime.run.cancelled",
    "kernel.lease.granted",
    "kernel.lease.expired",
    "kernel.submission.received",
    "kernel.gate.started",
    "kernel.gate.passed",
    "kernel.gate.failed",
    "kernel.merge.queued",
    "kernel.merge.committed",
    "kernel.merge.conflicted",
    "chunk.merged",
    "chunk.unblocked",
    "chunk.completed",
    "project.validation.started",
    "project.validation.failed",
    "project.validation.passed",
    "project.escalation.required",
    "project.completed",
    # Legacy names remain parseable until event producers are migrated.
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
    "DiagramChunkCommitted",
    "DiagramChunkUnblocked",
    "AttemptStarted",
    "AttemptFinished",
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
    schema_version: int = EVENT_SCHEMA_VERSION
    idempotency_key: str | None = None
    trace_id: str | None = None
    diagram_id: int | None = None
    diagram_version: int | None = None
    chunk_key: str | None = None
    product_task_key: str | None = None
    runtime_run_id: str | None = None
    attempt_id: str | None = None
    kernel_task_id: str | None = None
    lease_id: str | None = None
    submission_id: str | None = None
    placement_id: str | None = None

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
            event_id=f"evt_{uuid4().hex}",
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
        require_opaque_id(self.event_id, "event_id")
        require_opaque_id(self.tenant_id, "tenant_id")
        require_uuid(self.workspace_id, "workspace_id")
        if self.event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event_type {self.event_type!r}")
        if self.event_version < 1:
            raise ValueError("event_version must be >= 1")
        if self.schema_version != EVENT_SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        if self.aggregate_type not in {
            "product_task",
            "agent_run",
            "workflow",
            "kernel_task",
            "submission",
            "merge",
            "delivery",
            "diagram_chunk",
            "placement",
            "attempt",
        }:
            raise ValueError(f"unknown aggregate_type {self.aggregate_type!r}")
        require_opaque_id(self.aggregate_id, "aggregate_id")
        if self.aggregate_version < 1:
            raise ValueError("aggregate_version must be >= 1")
        if self.producer not in PRODUCERS:
            raise ValueError(f"unknown producer {self.producer!r}")
        if not self.correlation_id:
            raise ValueError("correlation_id is required")
        if self.idempotency_key is not None and not self.idempotency_key:
            raise ValueError("idempotency_key must be nonempty when present")
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
            "schema_version": self.schema_version,
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
            "idempotency_key": self.idempotency_key,
            "trace_id": self.trace_id,
            "diagram_id": self.diagram_id,
            "diagram_version": self.diagram_version,
            "chunk_key": self.chunk_key,
            "product_task_key": self.product_task_key,
            "runtime_run_id": self.runtime_run_id,
            "attempt_id": self.attempt_id,
            "kernel_task_id": self.kernel_task_id,
            "lease_id": self.lease_id,
            "submission_id": self.submission_id,
            "placement_id": self.placement_id,
            "payload": self.payload,
        }


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "EVENT_TYPES",
    "EventEnvelope",
    "PRODUCERS",
]
