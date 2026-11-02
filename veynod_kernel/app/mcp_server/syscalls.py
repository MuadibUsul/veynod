"""
Veynod Kernel (v4.0) - Core MCP Syscalls & REST Unified Router
Path: app/mcp_server/syscalls.py
Tech Stack: Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy 2.0 (AsyncSession)

Design Principles:
1. 6 Orthogonal Syscalls Only: Replaces the legacy 100+ MCP tools with 6 deterministic graph/compiler primitives.
2. Single Source of Truth: REST endpoints (/v1/syscalls/*) and MCP JSON-RPC (/mcp) share identical Pydantic v2 schemas.
3. Zero-Trust Boundary Enforcement: Pre-validates write_set hierarchy locks and DAG acyclicity before touching the DB or sandbox.
"""

from __future__ import annotations

import re
import time
from collections import deque
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, AsyncIterator, Callable, Coroutine
from contracts.work_scope import WorkScope
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import authenticate_worker_context, hash_token
from app.config import settings
from app.database import get_db_session
from app.engine.scheduler import AdmissionDenied
from app.engine.workspace_context import request_workspace
from app.engine.scheduler_core import scheduler_core_manager
from app.models import Workspace
from app.services.runtime_metrics import get_runtime_metrics
from app.services.submission_service import (
    SubmissionConflict,
    SubmissionRejected,
    patch_sha256,
    submission_service,
)


# ============================================================================
# 1. Enums & Core Domain Constants
# ============================================================================

class TaskType(str, Enum):
    DESIGN_ARCHITECTURE = "DESIGN_ARCHITECTURE"
    DESIGN_CONTRACT = "DESIGN_CONTRACT"
    WRITE_TEST = "WRITE_TEST"
    IMPLEMENT_BODY = "IMPLEMENT_BODY"
    REVISE_CONTRACT = "REVISE_CONTRACT"
    FIX_INTEGRATION = "FIX_INTEGRATION"


class TaskStatus(str, Enum):
    BLOCKED = "BLOCKED"
    READY = "READY"
    LEASED = "LEASED"
    VERIFYING = "VERIFYING"
    SUSPENDED = "SUSPENDED"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUSPENDED_ESCALATED = "SUSPENDED_ESCALATED"
    SPLIT_ARCHIVED = "SPLIT_ARCHIVED"


class ArtifactKind(str, Enum):
    CONTRACT = "contract"
    STUB = "stub"
    IMPLEMENTATION = "implementation"
    TEST = "test"


class ArtifactStatus(str, Enum):
    DRAFT = "DRAFT"
    FROZEN = "FROZEN"
    STALE = "STALE"
    COMMITTED = "COMMITTED"


class GateStage(str, Enum):
    WRITE_SET_BOUNDARY = "WRITE_SET_BOUNDARY"
    AST_SCOPE_DIFF = "AST_SCOPE_DIFF"
    CONTRACT_HASH_GUARD = "CONTRACT_HASH_GUARD"
    STATIC_TYPECHECK = "STATIC_TYPECHECK"
    UNIT_TEST_SUITE = "UNIT_TEST_SUITE"


# ============================================================================
# 2. Hierarchical Lock & Diff Security Utilities (Pure Functions)
# ============================================================================

class LockAlgebra:
    """
    Hierarchical AST & File Symbol Path Lock Algebra.
    Canonical format: `src/module/file.py::ClassName::method_name` or `src/module/file.py`.

    A dotted symbol segment (`file.py::Class.method`, the EDD shorthand) is
    canonicalised to `file.py::Class::method` so every producer/consumer agrees
    on one delimiter.
    """

    @staticmethod
    def normalize(symbol_path: str) -> str:
        cleaned = symbol_path.strip().replace("\\", "/").strip("/")
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        if not cleaned or "\x00" in cleaned:
            raise ValueError(f"Invalid or unsafe symbol path: {symbol_path!r}")

        parts = cleaned.split("::")
        file_segments = [segment for segment in parts[0].split("/") if segment]
        if not file_segments or any(segment in {".", ".."} for segment in file_segments):
            raise ValueError(f"Invalid or unsafe symbol path: {symbol_path!r}")

        symbols: list[str] = []
        for raw_symbol in parts[1:]:
            for symbol in raw_symbol.replace(".", "::").split("::"):
                symbol = symbol.strip()
                if not symbol:
                    continue
                if any(bad in symbol for bad in ("..", "/", "\\", "\x00")):
                    raise ValueError(f"Invalid or unsafe symbol path: {symbol_path!r}")
                symbols.append(symbol)

        return "::".join(["/".join(file_segments), *symbols])

    @classmethod
    def conflicts(cls, path_a: str, path_b: str) -> bool:
        """
        Two symbol paths conflict if they are identical or one is an ancestor of the other.
        Example: `src/auth.py` conflicts with `src/auth.py::AuthService::login`.
        """
        a = cls.normalize(path_a)
        b = cls.normalize(path_b)
        if a == b:
            return True
        return (
            a.startswith(f"{b}::")
            or b.startswith(f"{a}::")
            or a.startswith(f"{b}/")
            or b.startswith(f"{a}/")
        )

    @classmethod
    def is_covered_by_write_set(cls, target_path: str, allowed_write_set: list[str]) -> bool:
        """
        Returns True if `target_path` is equal to or a descendant of at least one entry in `allowed_write_set`.
        """
        target = cls.normalize(target_path)
        for allowed in allowed_write_set:
            norm_allowed = cls.normalize(allowed)
            if (
                target == norm_allowed
                or target.startswith(f"{norm_allowed}::")
                or target.startswith(f"{norm_allowed}/")
            ):
                return True
        return False

    @classmethod
    def extract_file_level_whitelist(cls, write_set: list[str]) -> set[str]:
        """
        Extracts the file-system path prefix from AST symbol paths (`file.py::Symbol` -> `file.py`).
        """
        return {cls.normalize(p).split("::")[0] for p in write_set}


class DiffBoundaryInspector:
    """
    Zero-trust Unified Diff parser to block unauthorized file modifications before sandbox execution.
    """

    _DIFF_HEADER_RE = re.compile(r"^(?:---|\+\+\+)\s+(?:a/|b/)?([^\t\n\r]+)", re.MULTILINE)

    @classmethod
    def extract_touched_files(cls, unified_diff: str) -> set[str]:
        touched: set[str] = set()
        for match in cls._DIFF_HEADER_RE.finditer(unified_diff):
            raw_path = match.group(1).strip()
            if raw_path.startswith('"') and raw_path.endswith('"'):
                raw_path = raw_path[1:-1]
                # The git prefix was inside the quotes, so the regex did not
                # consume it.
                if raw_path.startswith(("a/", "b/", "A/", "B/")):
                    raw_path = raw_path[2:]
            if raw_path not in {"/dev/null", "dev/null"}:
                touched.add(LockAlgebra.normalize(raw_path))
        return touched

    @classmethod
    def assert_within_write_set(cls, unified_diff: str, write_set: list[str]) -> set[str]:
        touched_files = cls.extract_touched_files(unified_diff)
        if not touched_files:
            raise ValueError("Submitted git_diff contains no valid file headers (--- a/... / +++ b/...).")

        allowed_files = LockAlgebra.extract_file_level_whitelist(write_set)
        violations = {
            f for f in touched_files
            if not any(f == allowed or f.startswith(f"{allowed}/") for allowed in allowed_files)
        }
        if violations:
            raise PermissionError(
                f"Write-Set Violation! Agent modified files outside leased write_set: {sorted(violations)}. "
                f"Allowed file scope: {sorted(allowed_files)}"
            )
        return touched_files


# ============================================================================
# 3. Pydantic v2 Schemas for the 6 Core Syscalls
# ============================================================================

# --- Syscall 1: pull_task_bundle ---

class PullTaskBundleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_hub_id: str = Field(..., min_length=2, max_length=128, description="Runner hub identifier")
    supported_task_types: list[TaskType] = Field(
        default_factory=lambda: list(TaskType),
        description="Task categories this worker is capable of executing",
    )
    max_context_tokens: int = Field(default=6000, ge=1024, le=32000, description="Hard token ceiling for sliced context")
    wait_seconds: float = Field(
        default=0.0,
        ge=0.0,
        le=30.0,
        description=(
            "Optional server-side hold time when the gate queue is full. "
            "The request releases its DB connection while waiting."
        ),
    )
    task_id: UUID | None = Field(
        default=None,
        description=(
            "Lease this specific task instead of any orthogonal task. The Agent "
            "Runtime always names the node it created, so one product task can "
            "never take another product task's DAG node."
        ),
    )


class SlicedArtifactView(BaseModel):
    artifact_id: UUID
    symbol_path: str
    kind: ArtifactKind
    status: ArtifactStatus
    content_hash: str
    sliced_content: str = Field(..., description="AST signature (for read_set) or mutable skeleton (for write_set)")


class TaskBundleResponse(BaseModel):
    leased: bool = Field(..., description="True if a disjoint task was leased; False if queue is empty/locked")
    task_id: UUID | None = None
    lease_token: str | None = Field(default=None, description="Cryptographic lease token required for mutations")
    lease_expires_at: datetime | None = None
    base_commit_sha: str | None = Field(default=None, description="Git revision the worker should branch from")
    worktree_path: str | None = Field(
        default=None,
        description="Absolute path of the worktree_pool lease directory. The agent cwd.",
    )
    worktree_anchor: str | None = Field(
        default=None,
        description="Host-mount anchor (parent of the repository) for rewriting worktree_path.",
    )
    worktree_rel: str | None = Field(
        default=None,
        description="Path under the anchor. Always contains lease/<task-hash>.",
    )
    task_type: TaskType | None = None
    specification: str | None = Field(default=None, description="Concise natural language contract requirement")
    last_error_traceback: str | None = Field(
        default=None, description="Deterministic gate diagnostics from the previous attempt, if any"
    )
    write_set: list[str] = Field(default_factory=list, description="Strict AST/file paths unlocked for modification")
    work_scope: dict[str, Any] | None = None
    scope_hash: str | None = None
    read_only_signatures: list[SlicedArtifactView] = Field(
        default_factory=list,
        description="1-Hop upstream dependency type definitions and signatures (implementation stripped)",
    )
    frozen_tests: list[SlicedArtifactView] = Field(
        default_factory=list,
        description="Read-only unit tests that must pass in the verification gate",
    )
    mutable_stubs: list[SlicedArtifactView] = Field(
        default_factory=list,
        description="Target stub files/AST nodes to be filled by the worker",
    )
    estimated_tokens: int = 0
    claim_timing: dict[str, int] = Field(
        default_factory=dict,
        description="Claim decision and bundle assembly timings in milliseconds.",
    )
    admission_denied: bool = Field(
        default=False,
        description="True when a defer-mode admission pause returned leased=false.",
    )
    retry_after_seconds: int | None = Field(
        default=None, description="Suggested retry delay when admission is paused."
    )
    queue_depth: dict[str, int] = Field(
        default_factory=dict, description="Observed gate/merge queue depth."
    )


# --- Syscall 2: submit_patch ---

class SubmitPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    lease_token: str = Field(..., min_length=16)
    git_diff: str = Field(..., min_length=10, max_length=500_000, description="Unified git diff (git diff HEAD)")
    touched_symbols: list[str] = Field(
        ...,
        min_length=1,
        description="Explicit list of AST symbol paths implemented or modified in this patch",
    )
    local_test_passed: bool = Field(
        ...,
        description="Runner must attest that local pre-check passed before hitting the central gate",
    )

    @field_validator("touched_symbols")
    @classmethod
    def validate_symbols(cls, v: list[str]) -> list[str]:
        return [LockAlgebra.normalize(s) for s in v]


class GateCheckResult(BaseModel):
    stage: GateStage
    passed: bool
    exit_code: int
    diagnostics: str = Field(default="", description="Compiler/Linter/Test traceback on failure (truncated to 4KB)")
    duration_ms: int = 0


class SubmitPatchResponse(BaseModel):
    task_id: UUID
    new_status: TaskStatus
    committed_revision_hash: str | None = None
    unlocked_downstream_task_ids: list[UUID] = Field(default_factory=list)
    gate_results: list[GateCheckResult] = Field(default_factory=list)
    merge_mode: str | None = Field(
        default=None,
        description="gate_rerun (full gate on the new HEAD) or tree_merge (verified patch tree-merged)",
    )
    merge_timing: dict[str, int] = Field(
        default_factory=dict,
        description="Critical-section timings in milliseconds (lock wait, dirty check, Git, DB, core sync).",
    )


class SubmitPatchV2Request(SubmitPatchRequest):
    """Versioned async submit body.  The idempotency key may also be a header."""

    idempotency_key: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Opaque retry key. It is bound to task_id, lease identity and the "
            "patch SHA-256; reuse with different material returns 409."
        ),
    )
    correlation: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Trace context carried from the Agent Run (tenant/workspace/"
            "project/product_task/run/trace). Stored on the submission and "
            "echoed in the final event."
        ),
    )


class SubmissionAcceptedResponse(BaseModel):
    schema_version: str = "v2"
    submission_id: UUID
    task_id: UUID
    status: str
    status_url: str
    idempotency_key: str
    patch_sha256: str
    derived_idempotency_key: bool = False
    deduplicated: bool = False
    final: bool = False
    accepted_at: str | None = None


class SubmissionStatusResponse(BaseModel):
    schema_version: str = "v2"
    submission_id: UUID
    task_id: UUID
    status: str
    final: bool
    task_status: str | None = None
    result_commit_sha: str | None = None
    merge_mode: str | None = None
    gate_results: list[dict[str, Any]] = Field(default_factory=list)
    merge_timing: dict[str, Any] = Field(default_factory=dict)
    unlocked_downstream_task_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_detail: str | None = None
    attempts: int = 0
    patch_sha256: str | None = None
    idempotency_key: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str | None = None
    status_url: str


# --- Syscall 3: request_task_split (Graph Fission) ---

class SubTaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_key: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{2,64}$", description="Temporary ID within the sub-DAG")
    task_type: TaskType
    specification: str = Field(..., min_length=10, max_length=4000)
    read_set_symbols: list[str] = Field(default_factory=list)
    write_set_symbols: list[str] = Field(..., min_length=1, description="Must be a strict subset of parent write_set")
    depends_on_local_keys: list[str] = Field(
        default_factory=list,
        description="Upstream local_keys within this split sub-DAG",
    )

    @field_validator("read_set_symbols", "write_set_symbols")
    @classmethod
    def normalize_paths(cls, v: list[str]) -> list[str]:
        return [LockAlgebra.normalize(p) for p in v]


class RequestTaskSplitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    lease_token: str = Field(..., min_length=16)
    split_reason: str = Field(
        ...,
        min_length=15,
        max_length=1000,
        description="Why this node exceeds complexity/LOC bounds (e.g., 'Estimated >350 LOC across 3 state machines')",
    )
    sub_tasks: list[SubTaskSpec] = Field(..., min_length=2, max_length=12, description="Bounded sub-DAG (2 to 12 nodes)")

    @model_validator(mode="after")
    def verify_sub_dag_acyclicity(self) -> "RequestTaskSplitRequest":
        """
        Kahn's topological sort at validation time, plus the orthogonality rule:
        siblings that are NOT ordered by the sub-DAG may not write the same
        symbol hierarchy. Ordered siblings are serialized by their edge and may
        overlap.
        """
        key_set = {node.local_key for node in self.sub_tasks}
        if len(key_set) != len(self.sub_tasks):
            raise ValueError("Duplicate local_key detected in sub_tasks.")

        in_degree: dict[str, int] = {k: 0 for k in key_set}
        adj: dict[str, list[str]] = {k: [] for k in key_set}

        for node in self.sub_tasks:
            for dep in node.depends_on_local_keys:
                if dep not in key_set:
                    raise ValueError(f"Sub-task {node.local_key!r} depends on unknown local_key {dep!r}.")
                if dep == node.local_key:
                    raise ValueError(f"Self-dependency cycle on sub-task {node.local_key!r}.")
                adj[dep].append(node.local_key)
                in_degree[node.local_key] += 1

        queue = deque([k for k, deg in in_degree.items() if deg == 0])
        visited_count = 0
        while queue:
            curr = queue.popleft()
            visited_count += 1
            for nxt in adj[curr]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)

        if visited_count != len(self.sub_tasks):
            raise ValueError("Cycle detected in proposed sub-tasks DAG! Fission sub-graph must be strictly acyclic.")

        # Transitive reachability for the orthogonality check.
        reachable: dict[str, set[str]] = {k: set() for k in key_set}
        for key in key_set:
            stack = list(adj[key])
            while stack:
                nxt = stack.pop()
                if nxt in reachable[key]:
                    continue
                reachable[key].add(nxt)
                stack.extend(adj[nxt])

        nodes = self.sub_tasks
        for i, left in enumerate(nodes):
            for right in nodes[i + 1 :]:
                if right.local_key in reachable[left.local_key] or left.local_key in reachable[right.local_key]:
                    continue
                for left_path in left.write_set_symbols:
                    for right_path in right.write_set_symbols:
                        if LockAlgebra.conflicts(left_path, right_path):
                            raise ValueError(
                                f"Concurrently-runnable sub-tasks {left.local_key!r} and {right.local_key!r} "
                                f"declare conflicting write paths: {left_path!r} vs {right_path!r}."
                            )
        return self


class RequestTaskSplitResponse(BaseModel):
    parent_task_id: UUID
    parent_new_status: TaskStatus = TaskStatus.SPLIT_ARCHIVED
    spawned_task_map: dict[str, UUID] = Field(..., description="Mapping of local_key -> persisted UUID")


# --- Syscall 4: escalate_contract_defect (Interrupt & Blast Radius Control) ---

class EscalateContractDefectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    lease_token: str = Field(..., min_length=16)
    defective_artifact_id: UUID = Field(..., description="The FROZEN upstream contract artifact ID")
    defect_reason: str = Field(..., min_length=20, max_length=2000)
    proposed_ast_signature: str = Field(
        ...,
        min_length=10,
        max_length=8000,
        description="Proposed updated interface/trait/type signature",
    )
    breaking_symbol_paths: list[str] = Field(
        ...,
        min_length=1,
        description="Specific fields/methods altered, used for LSP symbol-edge dirty propagation",
    )


class EscalateContractDefectResponse(BaseModel):
    suspended_task_id: UUID
    contract_revision_task_id: UUID = Field(..., description="Newly spawned high-priority L1 task to update contract")
    stale_marked_artifact_ids: list[UUID] = Field(
        default_factory=list,
        description="Downstream artifacts marked STALE via symbol_edges recursive traversal",
    )
    interrupted_running_task_ids: list[UUID] = Field(
        default_factory=list,
        description="Active leased tasks killed/rolled back due to blast-radius overlap",
    )


# --- Syscall 5: inject_dependency_task ---

class InjectDependencyTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    lease_token: str = Field(..., min_length=16)
    prerequisite_symbol_path: str = Field(..., description="New utility/component symbol path to create before this task")
    prerequisite_specification: str = Field(..., min_length=15, max_length=3000)
    prerequisite_write_set: list[str] = Field(..., min_length=1)

    @field_validator("prerequisite_symbol_path")
    @classmethod
    def norm_symbol(cls, v: str) -> str:
        return LockAlgebra.normalize(v)

    @field_validator("prerequisite_write_set")
    @classmethod
    def norm_write_set(cls, v: list[str]) -> list[str]:
        return [LockAlgebra.normalize(p) for p in v]


class InjectDependencyTaskResponse(BaseModel):
    current_task_id: UUID
    current_task_new_status: TaskStatus = TaskStatus.BLOCKED
    injected_prerequisite_task_id: UUID


# --- Syscall 6: query_symbol_signature ---

class QuerySymbolSignatureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    symbol_queries: list[str] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="Exact or prefix symbol paths to inspect (returns ONLY signatures, never bodies)",
    )


class SymbolSignatureRecord(BaseModel):
    artifact_id: UUID
    symbol_path: str
    kind: ArtifactKind
    status: ArtifactStatus
    ast_signature: str = Field(..., description="Pure type/function signature extracted by Tree-sitter")
    docstring: str | None = None


class QuerySymbolSignatureResponse(BaseModel):
    results: list[SymbolSignatureRecord]


# ============================================================================
# 4. Kernel Engine Service Interface (Dependency Injection Contract)
# ============================================================================

class ActiveLeaseContext(BaseModel):
    task_id: UUID
    worker_hub_id: str
    write_set: list[str]
    work_scope: dict[str, Any] | None = None
    scope_hash: str | None = None
    read_set_artifact_ids: list[UUID]
    status: TaskStatus


class KernelEngineService:
    """
    Abstract façade connecting the HTTP/MCP Router to `app/engine/` (Scheduler, Slicer, Gate, DAG Rewriter).
    In production, bind your SQLAlchemy AsyncSession implementation via FastAPI `app.dependency_overrides`.
    """

    async def verify_active_lease(self, task_id: UUID, lease_token: str, worker_id: str) -> ActiveLeaseContext:
        raise NotImplementedError("Bind KernelEngineService implementation in app/main.py")

    async def lease_next_disjoint_task(self, req: PullTaskBundleRequest, worker_id: str) -> TaskBundleResponse:
        raise NotImplementedError

    async def verify_and_commit_patch(
        self, lease: ActiveLeaseContext, req: SubmitPatchRequest, touched_files: set[str]
    ) -> SubmitPatchResponse:
        raise NotImplementedError

    async def execute_graph_split(
        self, lease: ActiveLeaseContext, req: RequestTaskSplitRequest
    ) -> RequestTaskSplitResponse:
        raise NotImplementedError

    async def escalate_and_propagate_stale(
        self, lease: ActiveLeaseContext, req: EscalateContractDefectRequest
    ) -> EscalateContractDefectResponse:
        raise NotImplementedError

    async def inject_prerequisite_node(
        self, lease: ActiveLeaseContext, req: InjectDependencyTaskRequest
    ) -> InjectDependencyTaskResponse:
        raise NotImplementedError

    async def lookup_symbol_signatures(self, req: QuerySymbolSignatureRequest) -> QuerySymbolSignatureResponse:
        raise NotImplementedError


# Dependency placeholders for FastAPI DI
async def get_kernel_engine() -> KernelEngineService:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="KernelEngineService dependency not bound.",
    )


async def get_authenticated_worker_id(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    authorization: Annotated[str | None, Header()] = None,
    x_workspace_id: Annotated[str | None, Header(alias="X-Workspace-Id")] = None,
) -> AsyncIterator[str]:
    """
    Validates the Runner's Bearer token against `agent_tokens` and returns the
    authenticated `worker_hub_id`. `VN_AUTH_MODE=dev` keeps the legacy
    hash-derived behaviour for a well-formed but unregistered token.

    Workspace routing: an optional ``X-Workspace-Id`` selects the request's
    workspace. A token bound to another workspace is rejected; a token bound to
    one workspace may omit the header. The resolved workspace is installed for
    the rest of the request so the engine never falls back to a process-wide
    default when the caller routed elsewhere.
    """
    if not authorization or not authorization.startswith("Bearer vnk_"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Veynod Kernel Runner token (expected 'Bearer vnk_...').",
        )
    identity = await authenticate_worker_context(
        session, authorization[7:].strip()
    )
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runner token is unknown or revoked.",
        )

    requested: UUID | None = None
    if x_workspace_id:
        try:
            requested = UUID(x_workspace_id.strip())
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="X-Workspace-Id must be a UUID.",
            ) from exc

    target = requested or identity.workspace_id or settings.default_workspace_id
    if target is None:
        target = await session.scalar(
            select(Workspace.workspace_id)
            .order_by(Workspace.created_at.asc())
            .limit(1)
        )
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No workspace exists in this Kernel deployment.",
        )
    exists = await session.scalar(
        select(Workspace.workspace_id).where(Workspace.workspace_id == target)
    )
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {target} does not exist.",
        )
    if identity.workspace_id is not None and identity.workspace_id != target:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Runner token is bound to a different workspace.",
        )
    with request_workspace(target):
        yield identity.worker_id


# ============================================================================
# 5. FastAPI Router: 6 Core Syscalls (REST + Unified MCP JSON-RPC Dispatcher)
# ============================================================================

router = APIRouter(tags=["Veynod Kernel Syscalls"])


@router.post(
    "/v1/syscalls/pull_task_bundle",
    response_model=TaskBundleResponse,
    summary="[Syscall 1] Lease a disjoint write-set task with AST-sliced context",
)
async def syscall_pull_task_bundle(
    req: PullTaskBundleRequest,
    worker_id: Annotated[str, Depends(get_authenticated_worker_id)],
    engine: Annotated[KernelEngineService, Depends(get_kernel_engine)],
):
    try:
        return await engine.lease_next_disjoint_task(req, worker_id=worker_id)
    except AdmissionDenied as exc:
        if settings.admission_mode == "defer":
            return TaskBundleResponse(
                leased=False,
                admission_denied=True,
                retry_after_seconds=exc.retry_after,
                queue_depth=exc.queue_depth,
                claim_timing={"admission_paused": 1},
            )
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(exc.retry_after)},
            content={
                "detail": {
                    "error": "admission_control",
                    "reason": exc.reason,
                    "retry_after_seconds": exc.retry_after,
                    "queue_depth": exc.queue_depth,
                }
            },
        )


async def _validate_submission(
    engine: KernelEngineService,
    req: SubmitPatchRequest,
    worker_id: str,
) -> tuple[ActiveLeaseContext, set[str]]:
    if not req.local_test_passed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Runner must pass local pre-flight checks (local_test_passed=True) before invoking central gate.",
        )
    lease = await engine.verify_active_lease(req.task_id, req.lease_token, worker_id)
    for sym in req.touched_symbols:
        if not LockAlgebra.is_covered_by_write_set(sym, lease.write_set):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"AST Symbol Violation: {sym!r} is outside leased write_set {lease.write_set}.",
            )
    try:
        touched_files = DiffBoundaryInspector.assert_within_write_set(
            req.git_diff, lease.write_set
        )
        if lease.work_scope is not None:
            scope = WorkScope.from_dict(lease.work_scope)
            if lease.scope_hash != scope.scope_hash:
                raise PermissionError("Kernel lease WorkScope hash does not match its snapshot")
            scope.assert_files_allowed(touched_files)
            if any(not scope.allows_symbol(symbol) for symbol in req.touched_symbols):
                raise PermissionError("submission touches symbols outside the frozen WorkScope")
    except (ValueError, PermissionError) as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    return lease, touched_files


@router.post(
    "/v2/syscalls/submit_patch",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SubmissionAcceptedResponse,
    summary="[Syscall 2 v2] Persist an immutable patch submission and return 202",
)
@router.post(
    "/v1/submissions",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SubmissionAcceptedResponse,
    include_in_schema=False,
)
async def syscall_submit_patch_v2(
    req: SubmitPatchV2Request,
    response: Response,
    worker_id: Annotated[str, Depends(get_authenticated_worker_id)],
    engine: Annotated[KernelEngineService, Depends(get_kernel_engine)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_header: Annotated[
        str | None, Header(alias="Idempotency-Key")
    ] = None,
) -> dict:
    if (
        req.idempotency_key
        and idempotency_header
        and req.idempotency_key != idempotency_header
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Body idempotency_key and Idempotency-Key header disagree; "
                "send exactly one value."
            ),
        )
    lease_hash = hash_token(req.lease_token)
    digest = patch_sha256(req.git_diff)
    raw_key = idempotency_header or req.idempotency_key
    started = time.perf_counter()
    metrics = get_runtime_metrics()

    def record_accept() -> None:
        metrics.observe(
            "submit.accept_server_ms", (time.perf_counter() - started) * 1000
        )

    find_started = time.perf_counter()
    try:
        # A retry must win over lease re-validation: the first request may
        # already have committed, so the task is no longer LEASED.
        existing = await submission_service.find_existing(
            session,
            runner_id=worker_id,
            task_id=req.task_id,
            lease_token_hash=lease_hash,
            patch_sha256=digest,
            idempotency_key=raw_key,
        )
    except SubmissionConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": exc.code, "message": exc.detail},
        ) from exc
    except SubmissionRejected as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": exc.code, "message": exc.detail},
        ) from exc
    metrics.observe(
        "submit.find_ms", (time.perf_counter() - find_started) * 1000
    )
    if existing is not None:
        response.headers["Location"] = existing["status_url"]
        response.headers["Idempotency-Key"] = existing["idempotency_key"]
        if not existing["final"]:
            response.headers["Retry-After"] = "1"
        record_accept()
        return existing

    validate_started = time.perf_counter()
    await _validate_submission(engine, req, worker_id)
    metrics.observe(
        "submit.validate_ms", (time.perf_counter() - validate_started) * 1000
    )
    persist_started = time.perf_counter()
    try:
        payload = await submission_service.accept(
            session,
            runner_id=worker_id,
            task_id=req.task_id,
            lease_token_hash=lease_hash,
            idempotency_key=raw_key,
            git_diff=req.git_diff,
            touched_symbols=req.touched_symbols,
            local_test_passed=req.local_test_passed,
            existing=existing,
            correlation=req.correlation,
        )
    except SubmissionConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": exc.code, "message": exc.detail},
        ) from exc
    except SubmissionRejected as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": exc.code, "message": exc.detail},
        ) from exc
    metrics.observe(
        "submit.persist_ms", (time.perf_counter() - persist_started) * 1000
    )
    payload["accepted_at"] = payload.get("created_at")
    response.headers["Location"] = payload["status_url"]
    response.headers["Retry-After"] = "1"
    response.headers["Idempotency-Key"] = payload["idempotency_key"]
    record_accept()
    return payload


@router.get(
    "/v2/submissions/find",
    response_model=SubmissionStatusResponse,
    summary="Look up a submission by its idempotency key",
)
@router.get(
    "/v1/submissions/find",
    response_model=SubmissionStatusResponse,
    include_in_schema=False,
)
async def find_submission(
    response: Response,
    worker_id: Annotated[str, Depends(get_authenticated_worker_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Query(min_length=1, max_length=200)],
) -> dict:
    payload = await submission_service.find_by_key(
        session, idempotency_key, worker_id
    )
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No submission is bound to this idempotency key for this runner.",
        )
    if not payload["final"]:
        response.headers["Retry-After"] = "1"
    else:
        # A gate worker may have committed in another process; make this API
        # process's Rust core observe the durable task state immediately.
        ids = [payload["task_id"], *payload.get("unlocked_downstream_task_ids", [])]
        await scheduler_core_manager.sync_tasks(
            session, UUID(payload["workspace_id"]), [UUID(value) for value in ids]
        )
        await session.commit()
    return payload


@router.get(
    "/v2/submissions/{submission_id}",
    response_model=SubmissionStatusResponse,
    summary="Query the durable final status of an accepted patch submission",
)
@router.get(
    "/v1/submissions/{submission_id}",
    response_model=SubmissionStatusResponse,
    include_in_schema=False,
)
async def get_submission_status(
    submission_id: UUID,
    response: Response,
    worker_id: Annotated[str, Depends(get_authenticated_worker_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    payload = await submission_service.get_status(
        session, submission_id, worker_id
    )
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Submission {submission_id} not found for this runner.",
        )
    if not payload["final"]:
        response.headers["Retry-After"] = "1"
    else:
        # A gate worker may have committed in another process; make this API
        # process's Rust core observe the durable task state immediately.
        ids = [payload["task_id"], *payload.get("unlocked_downstream_task_ids", [])]
        await scheduler_core_manager.sync_tasks(
            session, UUID(payload["workspace_id"]), [UUID(value) for value in ids]
        )
        await session.commit()
    return payload


@router.post(
    "/v1/syscalls/submit_patch",
    response_model=SubmitPatchResponse,
    deprecated=True,
    summary="[Deprecated] Synchronous submit; use POST /v2/syscalls/submit_patch",
)
async def syscall_submit_patch(
    req: SubmitPatchRequest,
    response: Response,
    worker_id: Annotated[str, Depends(get_authenticated_worker_id)],
    engine: Annotated[KernelEngineService, Depends(get_kernel_engine)],
) -> SubmitPatchResponse:
    if settings.legacy_submit_mode == "reject":
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "error": "legacy_submit_endpoint_retired",
                "message": (
                    "POST /v1/syscalls/submit_patch no longer blocks on the "
                    "deterministic gate. Use POST /v2/syscalls/submit_patch, "
                    "then poll GET /v2/submissions/{submission_id}."
                ),
                "migration": {
                    "endpoint": "/v2/syscalls/submit_patch",
                    "status_endpoint": "/v2/submissions/{submission_id}",
                    "find_by_key": "/v2/submissions/find?idempotency_key=...",
                    "idempotency_header": "Idempotency-Key",
                    "temporary_escape_hatch": "VN_LEGACY_SUBMIT_MODE=sync",
                },
            },
            headers={
                "Deprecation": "true",
                "Link": '</v2/syscalls/submit_patch>; rel="successor-version"',
            },
        )
    response.headers["Deprecation"] = "true"
    lease, touched_files = await _validate_submission(engine, req, worker_id)
    return await engine.verify_and_commit_patch(lease, req, touched_files)


@router.post(
    "/v1/syscalls/request_task_split",
    response_model=RequestTaskSplitResponse,
    summary="[Syscall 3] Fission a complex task into a bounded acyclic sub-DAG",
)
async def syscall_request_task_split(
    req: RequestTaskSplitRequest,
    worker_id: Annotated[str, Depends(get_authenticated_worker_id)],
    engine: Annotated[KernelEngineService, Depends(get_kernel_engine)],
) -> RequestTaskSplitResponse:
    lease = await engine.verify_active_lease(req.task_id, req.lease_token, worker_id)

    # Enforce Conservation of Write-Set Authority: Union(child.write_set) <= parent.write_set
    for sub in req.sub_tasks:
        for child_path in sub.write_set_symbols:
            if not LockAlgebra.is_covered_by_write_set(child_path, lease.write_set):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        f"Sub-task {sub.local_key!r} requests write lock on {child_path!r}, "
                        f"which exceeds parent task write_set {lease.write_set}."
                    ),
                )

    return await engine.execute_graph_split(lease, req)


@router.post(
    "/v1/syscalls/escalate_contract_defect",
    response_model=EscalateContractDefectResponse,
    summary="[Syscall 4] Raise upstream contract interrupt and trigger symbol-level STALE propagation",
)
async def syscall_escalate_contract_defect(
    req: EscalateContractDefectRequest,
    worker_id: Annotated[str, Depends(get_authenticated_worker_id)],
    engine: Annotated[KernelEngineService, Depends(get_kernel_engine)],
) -> EscalateContractDefectResponse:
    lease = await engine.verify_active_lease(req.task_id, req.lease_token, worker_id)
    if req.defective_artifact_id not in lease.read_set_artifact_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Artifact {req.defective_artifact_id} is not in this task's upstream read_set.",
        )
    return await engine.escalate_and_propagate_stale(lease, req)


@router.post(
    "/v1/syscalls/inject_dependency_task",
    response_model=InjectDependencyTaskResponse,
    summary="[Syscall 5] Dynamically insert a missing prerequisite node ahead of current task",
)
async def syscall_inject_dependency_task(
    req: InjectDependencyTaskRequest,
    worker_id: Annotated[str, Depends(get_authenticated_worker_id)],
    engine: Annotated[KernelEngineService, Depends(get_kernel_engine)],
) -> InjectDependencyTaskResponse:
    lease = await engine.verify_active_lease(req.task_id, req.lease_token, worker_id)
    return await engine.inject_prerequisite_node(lease, req)


@router.post(
    "/v1/syscalls/query_symbol_signature",
    response_model=QuerySymbolSignatureResponse,
    summary="[Syscall 6] Query 1-hop external AST type/function signatures (implementation bodies stripped)",
)
async def syscall_query_symbol_signature(
    req: QuerySymbolSignatureRequest,
    worker_id: Annotated[str, Depends(get_authenticated_worker_id)],
    engine: Annotated[KernelEngineService, Depends(get_kernel_engine)],
) -> QuerySymbolSignatureResponse:
    return await engine.lookup_symbol_signatures(req)


# ============================================================================
# 6. Auto-Generated MCP over HTTP JSON-RPC 2.0 Bridge (/mcp)
#    Zero duplicate tool definitions: dynamically derived from the 6 Pydantic models
# ============================================================================

class MCPToolDescriptor(BaseModel):
    name: str
    description: str
    inputSchema: dict[str, Any]


class MCPCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jsonrpc: str = Field(default="2.0", pattern=r"^2\.0$")
    id: str | int | None = None
    method: str = Field(..., min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)


_SYSCALL_REGISTRY: dict[str, tuple[type[BaseModel], str, Callable[..., Coroutine[Any, Any, BaseModel]]]] = {
    "pull_task_bundle": (
        PullTaskBundleRequest,
        "Pull a locked task bundle with minimal AST-sliced context (<6k tokens).",
        syscall_pull_task_bundle,
    ),
    "submit_patch": (
        SubmitPatchRequest,
        "Submit a git diff within your leased write_set to run the deterministic compiler & test gate.",
        syscall_submit_patch,
    ),
    "request_task_split": (
        RequestTaskSplitRequest,
        "Refuse coding an oversized task (>300 LOC) and split it into an acyclic sub-DAG (2-12 nodes).",
        syscall_request_task_split,
    ),
    "escalate_contract_defect": (
        EscalateContractDefectRequest,
        "Suspend current task and report a defect in an upstream FROZEN contract signature.",
        syscall_escalate_contract_defect,
    ),
    "inject_dependency_task": (
        InjectDependencyTaskRequest,
        "Inject a missing utility/prerequisite task before the current node and release lease.",
        syscall_inject_dependency_task,
    ),
    "query_symbol_signature": (
        QuerySymbolSignatureRequest,
        "Query read-only AST signatures of external symbols without loading their implementation bodies.",
        syscall_query_symbol_signature,
    ),
}


@router.post("/mcp", summary="Unified MCP JSON-RPC 2.0 Endpoint (Auto-generated from the 6 Syscalls)")
async def mcp_jsonrpc_endpoint(
    rpc: MCPCallRequest,
    worker_id: Annotated[str, Depends(get_authenticated_worker_id)],
    engine: Annotated[KernelEngineService, Depends(get_kernel_engine)],
) -> dict[str, Any]:
    # Streamable-HTTP MCP handshake, so real clients (Claude Code, Codex) can
    # mount this endpoint directly.
    if rpc.method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rpc.id,
            "result": {
                "protocolVersion": rpc.params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "veynod-kernel", "version": "4.0.0"},
            },
        }
    if rpc.method in {"notifications/initialized", "notifications/cancelled"}:
        # JSON-RPC notifications carry no id and expect no response body.
        return {}
    if rpc.method == "ping":
        return {"jsonrpc": "2.0", "id": rpc.id, "result": {}}

    if rpc.method == "tools/list":
        tools = [
            MCPToolDescriptor(
                name=name,
                description=desc,
                inputSchema=schema_cls.model_json_schema(),
            ).model_dump()
            for name, (schema_cls, desc, _) in _SYSCALL_REGISTRY.items()
        ]
        return {"jsonrpc": "2.0", "id": rpc.id, "result": {"tools": tools}}

    if rpc.method != "tools/call":
        return {
            "jsonrpc": "2.0",
            "id": rpc.id,
            "error": {
                "code": -32601,
                "message": f"Unknown MCP method {rpc.method!r}.",
            },
        }

    # tools/call
    tool_name = rpc.params.get("name")
    arguments = rpc.params.get("arguments", {})
    if tool_name not in _SYSCALL_REGISTRY:
        return {
            "jsonrpc": "2.0",
            "id": rpc.id,
            "error": {
                "code": -32601,
                "message": f"Unknown syscall tool {tool_name!r}. Allowed: {list(_SYSCALL_REGISTRY.keys())}",
            },
        }

    schema_cls, _, handler = _SYSCALL_REGISTRY[tool_name]
    try:
        validated_req = schema_cls.model_validate(arguments)
        result_model = await handler(req=validated_req, worker_id=worker_id, engine=engine)
        return {
            "jsonrpc": "2.0",
            "id": rpc.id,
            "result": {
                "content": [{"type": "text", "text": result_model.model_dump_json(indent=2)}],
                "isError": False,
            },
        }
    except HTTPException as http_exc:
        return {
            "jsonrpc": "2.0",
            "id": rpc.id,
            "result": {
                "content": [{"type": "text", "text": f"[SYSCALL_REJECTED {http_exc.status_code}] {http_exc.detail}"}],
                "isError": True,
            },
        }
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": rpc.id,
            "result": {
                "content": [{"type": "text", "text": f"[VALIDATION_ERROR] {exc}"}],
                "isError": True,
            },
        }
