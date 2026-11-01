"""SQLAlchemy 2.0 ORM models for the bipartite contract DAG."""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ArtifactKind(str, enum.Enum):
    CONTRACT = "contract"
    STUB = "stub"
    IMPLEMENTATION = "implementation"
    TEST = "test"


class ArtifactStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    FROZEN = "FROZEN"
    STALE = "STALE"
    COMMITTED = "COMMITTED"


class TaskType(str, enum.Enum):
    DESIGN_ARCHITECTURE = "DESIGN_ARCHITECTURE"
    DESIGN_CONTRACT = "DESIGN_CONTRACT"
    WRITE_TEST = "WRITE_TEST"
    IMPLEMENT_BODY = "IMPLEMENT_BODY"
    REVISE_CONTRACT = "REVISE_CONTRACT"
    FIX_INTEGRATION = "FIX_INTEGRATION"


class TaskStatus(str, enum.Enum):
    BLOCKED = "BLOCKED"
    READY = "READY"
    LEASED = "LEASED"
    VERIFYING = "VERIFYING"
    SUSPENDED = "SUSPENDED"
    SUSPENDED_ESCALATED = "SUSPENDED_ESCALATED"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SPLIT_ARCHIVED = "SPLIT_ARCHIVED"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


def _pg_enum(enum_cls: type[enum.Enum], name: str) -> PGEnum:
    return PGEnum(
        enum_cls,
        name=name,
        values_callable=_enum_values,
        create_type=False,
        validate_strings=True,
    )


artifact_kind_enum = _pg_enum(ArtifactKind, "artifact_kind")
artifact_status_enum = _pg_enum(ArtifactStatus, "artifact_status")
task_type_enum = _pg_enum(TaskType, "task_type")
task_status_enum = _pg_enum(TaskStatus, "task_status")

UUID_ARRAY = ARRAY(PG_UUID(as_uuid=True))
TEXT_ARRAY = ARRAY(Text)


class Workspace(Base):
    __tablename__ = "workspaces"

    workspace_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    repo_path: Mapped[str] = mapped_column(Text, nullable=False)
    main_branch: Mapped[str] = mapped_column(String(64), nullable=False, default="main")
    head_commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    language_toolchain: Mapped[str] = mapped_column(
        String(32), nullable=False, default="python"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CodeArtifact(Base):
    __tablename__ = "code_artifacts"
    __table_args__ = (UniqueConstraint("workspace_id", "symbol_path"),)

    artifact_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    symbol_path: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[ArtifactKind] = mapped_column(artifact_kind_enum, nullable=False)
    status: Mapped[ArtifactStatus] = mapped_column(
        artifact_status_enum, nullable=False, default=ArtifactStatus.DRAFT
    )
    ast_signature: Mapped[str] = mapped_column(Text, nullable=False, default="")
    docstring: Mapped[str] = mapped_column(Text, nullable=False, default="")
    signature_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class DagTask(Base):
    __tablename__ = "dag_tasks"
    __table_args__ = (CheckConstraint("in_degree >= 0", name="ck_dag_tasks_in_degree"),)

    task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_task_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dag_tasks.task_id"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    spec_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[TaskType] = mapped_column(task_type_enum, nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        task_status_enum, nullable=False, default=TaskStatus.BLOCKED
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    in_degree: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    read_artifact_ids: Mapped[list[UUID]] = mapped_column(
        UUID_ARRAY, nullable=False, default=list, server_default=text("'{}'::UUID[]")
    )
    target_artifact_ids: Mapped[list[UUID]] = mapped_column(
        UUID_ARRAY, nullable=False, default=list, server_default=text("'{}'::UUID[]")
    )
    test_artifact_ids: Mapped[list[UUID]] = mapped_column(
        UUID_ARRAY, nullable=False, default=list, server_default=text("'{}'::UUID[]")
    )

    write_locks: Mapped[list[str]] = mapped_column(
        TEXT_ARRAY, nullable=False, default=list, server_default=text("'{}'::TEXT[]")
    )
    ancestor_locks: Mapped[list[str]] = mapped_column(
        TEXT_ARRAY, nullable=False, default=list, server_default=text("'{}'::TEXT[]")
    )

    leased_by_runner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when the Agent Runtime created this node (idempotent retries).
    runtime_request_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    result_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    snapshot_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )

    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_error_traceback: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TaskDependency(Base):
    __tablename__ = "task_dependencies"

    upstream_task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dag_tasks.task_id", ondelete="CASCADE"),
        primary_key=True,
    )
    downstream_task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dag_tasks.task_id", ondelete="CASCADE"),
        primary_key=True,
    )


class SymbolEdge(Base):
    __tablename__ = "symbol_edges"

    workspace_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        primary_key=True,
    )
    caller_artifact_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("code_artifacts.artifact_id", ondelete="CASCADE"),
        primary_key=True,
    )
    callee_artifact_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("code_artifacts.artifact_id", ondelete="CASCADE"),
        primary_key=True,
    )
    referenced_fields: Mapped[list[str]] = mapped_column(
        TEXT_ARRAY, nullable=False, default=list, server_default=text("'{}'::TEXT[]")
    )


class KernelEvent(Base):
    __tablename__ = "kernel_events"

    event_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    event_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), server_default=func.gen_random_uuid()
    )
    event_version: Mapped[int] = mapped_column(
        Integer, server_default="1", nullable=False
    )
    producer: Mapped[str] = mapped_column(
        String(64), server_default="kernel", nullable=False
    )
    correlation_id: Mapped[str | None] = mapped_column(Text)
    causation_id: Mapped[str | None] = mapped_column(Text)


class AgentToken(Base):
    __tablename__ = "agent_tokens"

    token_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    worker_hub_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    workspace_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=True,
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
