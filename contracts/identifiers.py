"""Cross-domain identifiers for the Veynod Agent Coding Runtime.

These are contract-level definitions, not database models.  They intentionally
contain no framework imports so AgentGraph, Agent Runtime and the Kernel can
share them without sharing a schema or a deployment unit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

# Stable object names used in events, logs and API payloads.
IDENTIFIER_FIELDS: tuple[str, ...] = (
    "tenant_id",
    "workspace_id",
    "project_id",
    "repository_id",
    "coding_session_id",
    "product_task_id",
    "agent_run_id",
    "workflow_id",
    "workflow_version",
    "kernel_task_id",
    "attempt_id",
    "submission_id",
    "verification_run_id",
    "commit_sha",
    "delivery_id",
)


def new_id() -> str:
    """Return a new opaque UUID string (no database dependency)."""
    return str(uuid4())


def new_correlation_id() -> str:
    return f"corr_{uuid4().hex}"


def is_uuid(value: Any) -> bool:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def require_uuid(value: Any, field_name: str) -> str:
    if not is_uuid(value):
        raise ValueError(f"{field_name} must be a UUID")
    return str(value)


@dataclass(frozen=True)
class IdentityChain:
    """The stable link between product, runtime and kernel facts.

    Only ``tenant_id`` and ``workspace_id`` are mandatory.  Later domains fill
    in their own identifiers as the work progresses; a missing identifier is
    not the same as a different identifier.
    """

    tenant_id: str
    workspace_id: str
    project_id: str | None = None
    repository_id: str | None = None
    coding_session_id: str | None = None
    product_task_id: str | None = None
    agent_run_id: str | None = None
    workflow_id: str | None = None
    workflow_version: int | None = None
    kernel_task_id: str | None = None
    attempt_id: str | None = None
    submission_id: str | None = None
    verification_run_id: str | None = None
    commit_sha: str | None = None
    delivery_id: str | None = None
    correlation_id: str = field(default_factory=new_correlation_id)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def validate(self) -> "IdentityChain":
        require_uuid(self.tenant_id, "tenant_id")
        require_uuid(self.workspace_id, "workspace_id")
        for name in (
            "project_id",
            "repository_id",
            "coding_session_id",
            "product_task_id",
            "agent_run_id",
            "workflow_id",
            "kernel_task_id",
            "attempt_id",
            "submission_id",
            "verification_run_id",
            "delivery_id",
        ):
            value = getattr(self, name)
            if value is not None:
                require_uuid(value, name)
        if self.workflow_version is not None and self.workflow_version < 1:
            raise ValueError("workflow_version must be >= 1")
        if self.commit_sha is not None and not (
            7 <= len(self.commit_sha) <= 64
            and all(ch in "0123456789abcdefABCDEF" for ch in self.commit_sha)
        ):
            raise ValueError("commit_sha must be a 7-64 character hex string")
        if not self.correlation_id:
            raise ValueError("correlation_id is required")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in (
                "tenant_id",
                "workspace_id",
                "project_id",
                "repository_id",
                "coding_session_id",
                "product_task_id",
                "agent_run_id",
                "workflow_id",
                "workflow_version",
                "kernel_task_id",
                "attempt_id",
                "submission_id",
                "verification_run_id",
                "commit_sha",
                "delivery_id",
                "correlation_id",
                "created_at",
            )
        }


__all__ = [
    "IDENTIFIER_FIELDS",
    "IdentityChain",
    "is_uuid",
    "new_correlation_id",
    "new_id",
    "require_uuid",
]
