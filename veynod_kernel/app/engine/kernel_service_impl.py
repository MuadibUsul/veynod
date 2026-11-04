"""SQLAlchemy-backed implementation of the 6-syscall KernelEngineService.

Mounted in ``app/main.py`` with a one-line dependency override:

    app.dependency_overrides[get_kernel_engine] = \\
        PostgresKernelEngineService.from_session_dep
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import constant_time_equals, hash_token
from app.config import settings
from app.database import get_db_session
from app.engine.lock_algebra import LockAlgebra
from app.engine.mutator import GraphMutator, SubTaskNodeSpec
from app.engine.scheduler import DisjointWriteSetScheduler
from app.engine.scheduler_core import scheduler_core_manager
from app.engine.workspace_context import current_request_workspace
from app.mcp_server.syscalls import (
    ActiveLeaseContext,
    ArtifactKind as ApiArtifactKind,
    ArtifactStatus as ApiArtifactStatus,
    EscalateContractDefectRequest,
    EscalateContractDefectResponse,
    InjectDependencyTaskRequest,
    InjectDependencyTaskResponse,
    KernelEngineService,
    PullTaskBundleRequest,
    QuerySymbolSignatureRequest,
    QuerySymbolSignatureResponse,
    RequestTaskSplitRequest,
    RequestTaskSplitResponse,
    SlicedArtifactView,
    SymbolSignatureRecord,
    TaskBundleResponse,
    TaskStatus as ApiTaskStatus,
    TaskType as ApiTaskType,
)
from app.models import (
    ArtifactKind,
    CodeArtifact,
    DagTask,
    TaskStatus,
    Workspace,
)
from app.services.artifact_store import ensure_artifacts, infer_kind
from app.services.ast_slicer import get_slicer
from app.services.event_bus import EventBus
from app.services.git_ops import GitRepository
from app.services.worktree_pool import allocate_lease_worktree
from app.services.runtime_metrics import get_runtime_metrics
from app.services.verification_gate import DeterministicVerificationGate


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _leaf(symbol_path: str) -> str:
    return LockAlgebra.normalize(symbol_path).split("::")[-1]


class PostgresKernelEngineService(KernelEngineService):
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.slicer = get_slicer()
        self.event_bus = EventBus(session)
        self.scheduler = DisjointWriteSetScheduler(session, self.event_bus)
        self.mutator = GraphMutator(session, self.event_bus)
        self.gate = DeterministicVerificationGate(
            session, self.slicer, self.scheduler, self.event_bus
        )

    @classmethod
    async def from_session_dep(
        cls,
        session: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> "PostgresKernelEngineService":
        """FastAPI dependency: new engine per request, bound to the request session."""
        return cls(session)

    # ------------------------------------------------------------- workspace
    async def _default_workspace_id(self) -> UUID:
        # A routed request (X-Workspace-Id validated against the runner token)
        # wins over every process-wide default.
        routed = current_request_workspace()
        if routed is not None:
            return routed

        if settings.default_workspace_id is not None:
            exists = await self.session.scalar(
                select(Workspace.workspace_id).where(
                    Workspace.workspace_id == settings.default_workspace_id
                )
            )
            if exists is not None:
                return exists
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"VN_DEFAULT_WORKSPACE_ID={settings.default_workspace_id} does not "
                    "exist in this database."
                ),
            )

        if settings.cell_id is not None:
            workspace_id = await self.session.scalar(
                text(
                    """
                    SELECT w.workspace_id
                    FROM workspaces w
                    JOIN repository_placements rp
                      ON rp.workspace_id = w.workspace_id
                    WHERE rp.cell_id = :cell_id AND rp.status = 'ACTIVE'
                    ORDER BY w.created_at ASC
                    LIMIT 1
                    """
                ),
                {"cell_id": str(settings.cell_id)},
            )
        else:
            workspace_id = await self.session.scalar(
                select(Workspace.workspace_id)
                .order_by(Workspace.created_at.asc())
                .limit(1)
            )
        if workspace_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "No workspace exists. Create one with "
                    "POST /v1/admin/workspaces (X-Admin-Key required)."
                ),
            )
        return workspace_id

    async def _workspace(self, workspace_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workspace {workspace_id} not found.",
            )
        return workspace

    # ---------------------------------------------------------- lease verify
    async def verify_active_lease(
        self, task_id: UUID, lease_token: str, worker_id: str
    ) -> ActiveLeaseContext:
        task = await self.session.get(DagTask, task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Task {task_id} not found.",
            )
        routed = current_request_workspace()
        if routed is not None and task.workspace_id != routed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Task belongs to a different workspace than this request.",
            )
        if task.status != TaskStatus.LEASED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Task {task_id} is {task.status.value}, not LEASED.",
            )
        if task.leased_by_runner != worker_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Lease belongs to a different runner.",
            )
        if task.lease_expires_at is None or task.lease_expires_at <= _now():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Lease has expired.",
            )
        if task.lease_token_hash is None or not constant_time_equals(
            task.lease_token_hash, hash_token(lease_token)
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid lease token.",
            )
        return ActiveLeaseContext(
            task_id=task.task_id,
            worker_hub_id=worker_id,
            write_set=list(task.write_locks),
            work_scope=task.lease_scope,
            scope_hash=task.lease_scope_hash,
            read_set_artifact_ids=list(task.read_artifact_ids),
            status=ApiTaskStatus(task.status.value),
        )

    # ---------------------------------------------------------------- pull
    async def lease_next_disjoint_task(
        self, req: PullTaskBundleRequest, worker_id: str
    ) -> TaskBundleResponse:
        started = time.monotonic()
        workspace_id = await self._default_workspace_id()
        workspace = await self._workspace(workspace_id)

        raw_token = "vnl_" + secrets.token_urlsafe(32)
        token_hash = hash_token(raw_token)
        supported = [task_type.value for task_type in req.supported_task_types]

        row = await self.scheduler.claim_next_orthogonal_task(
            workspace_id=workspace_id,
            runner_id=worker_id,
            supported_task_types=supported,
            lease_token_hash=token_hash,
            lease_seconds=settings.lease_seconds,
            wait_seconds=req.wait_seconds,
            requested_task_id=req.task_id,
        )
        claim_decision_ms = int((time.monotonic() - started) * 1000)
        if row is None:
            await self.session.commit()
            timing = {
                "claim_decision_ms": claim_decision_ms,
                "bundle_ms": 0,
                "total_ms": int((time.monotonic() - started) * 1000),
            }
            get_runtime_metrics().record_claim(False, timing)
            return TaskBundleResponse(leased=False, claim_timing=timing)

        # Release the claim transaction before doing git reads. The session
        # may have been closed and reopened while the claim long-polled, so
        # refresh the workspace instead of using a detached instance.
        await self.session.commit()
        task = await self.session.get(DagTask, row["task_id"])
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Claimed task disappeared.",
            )
        if task.work_scope is not None:
            task.lease_scope = task.work_scope
            task.lease_scope_hash = task.scope_hash
            await self.session.commit()
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Workspace disappeared.",
            )

        bundle_started = time.monotonic()
        repo_path = str(workspace.repo_path)
        main_branch = workspace.main_branch
        leased_task_id = task.task_id
        try:
            bundle = await self._build_bundle(task, workspace, raw_token, req.max_context_tokens)
        except Exception:
            get_runtime_metrics().record_claim_error("bundle_failed")
            await self.session.rollback()
            await self.scheduler.release_task(
                task.task_id,
                token_hash,
                "Failed to assemble task bundle",
                retry_counted=False,
            )
            await self.session.commit()
            raise
        bundle_ms = int((time.monotonic() - bundle_started) * 1000)
        try:
            path, anchor, rel = await allocate_lease_worktree(
                GitRepository(repo_path, main_branch),
                leased_task_id,
                bundle.base_commit_sha or "HEAD",
            )
            bundle.worktree_path = str(path)
            bundle.worktree_anchor = anchor
            bundle.worktree_rel = rel
        except Exception:
            # Fail closed at the runner: no path means it must not touch the
            # shared checkout. The lease itself stays so a retry can allocate.
            import logging
            logging.getLogger("app.engine.kernel").exception(
                "worktree_pool lease allocation failed for %s", leased_task_id
            )

        await self.event_bus.publish(
            workspace_id,
            "TASK_LEASED",
            {
                "task_id": str(task.task_id),
                "runner_id": worker_id,
                "write_set": list(task.write_locks),
            },
        )
        await self.session.commit()
        bundle.claim_timing = {
            "claim_decision_ms": claim_decision_ms,
            "bundle_ms": bundle_ms,
            "total_ms": int((time.monotonic() - started) * 1000),
        }
        get_runtime_metrics().record_claim(True, bundle.claim_timing)
        return bundle

    async def _build_bundle(
        self,
        task: DagTask,
        workspace: Workspace,
        raw_token: str,
        max_context_tokens: int,
    ) -> TaskBundleResponse:
        repo = GitRepository(workspace.repo_path, workspace.main_branch)
        artifact_ids = {
            *task.read_artifact_ids,
            *task.target_artifact_ids,
            *task.test_artifact_ids,
        }
        by_id: dict[UUID, CodeArtifact] = {}
        if artifact_ids:
            rows = (
                await self.session.execute(
                    select(CodeArtifact).where(
                        CodeArtifact.artifact_id.in_(list(artifact_ids))
                    )
                )
            ).scalars().all()
            by_id = {artifact.artifact_id: artifact for artifact in rows}

        # Git/AST assembly below is read-only and can take time; do not pin a
        # pooled DB connection while reading files. Attribute values are loaded.
        await self.session.close()
        rev = task.base_commit_sha or "HEAD"

        # One cat-file process for all artifacts instead of one `git show`
        # per file; the batch result primes GitRepository's cache.
        artifact_paths = {
            artifact.file_path
            for artifact_id, artifact in by_id.items()
            if artifact_id in artifact_ids
        }
        if artifact_paths:
            await repo.read_files_batch(artifact_paths, rev)

        read_views: list[SlicedArtifactView] = []
        for artifact_id in task.read_artifact_ids:
            artifact = by_id.get(artifact_id)
            if artifact is None:
                continue
            content = artifact.ast_signature or await self._signature_from_head(
                artifact, repo, rev
            )
            read_views.append(self._view(artifact, content))

        mutable_views: list[SlicedArtifactView] = []
        for artifact_id in task.target_artifact_ids:
            artifact = by_id.get(artifact_id)
            if artifact is None:
                continue
            content = await self._current_code(artifact, repo, for_test=False, rev=rev)
            mutable_views.append(self._view(artifact, content))

        test_views: list[SlicedArtifactView] = []
        for artifact_id in task.test_artifact_ids:
            artifact = by_id.get(artifact_id)
            if artifact is None:
                continue
            content = await self._current_code(artifact, repo, for_test=True, rev=rev)
            test_views.append(self._view(artifact, content))

        bundle = TaskBundleResponse(
            leased=True,
            task_id=task.task_id,
            lease_token=raw_token,
            lease_expires_at=task.lease_expires_at,
            base_commit_sha=task.base_commit_sha,
            task_type=ApiTaskType(task.type.value),
            specification=task.spec_prompt,
            last_error_traceback=task.last_error_traceback,
            write_set=list(task.write_locks),
            work_scope=task.lease_scope,
            scope_hash=task.lease_scope_hash,
            read_only_signatures=read_views,
            frozen_tests=test_views,
            mutable_stubs=mutable_views,
        )
        _fit_bundle(bundle, max_context_tokens)
        return bundle

    def _view(self, artifact: CodeArtifact, content: str) -> SlicedArtifactView:
        return SlicedArtifactView(
            artifact_id=artifact.artifact_id,
            symbol_path=artifact.symbol_path,
            kind=ApiArtifactKind(artifact.kind.value),
            status=ApiArtifactStatus(artifact.status.value),
            content_hash=artifact.content_hash or "",
            sliced_content=content,
        )

    async def _signature_from_head(
        self, artifact: CodeArtifact, repo: GitRepository, rev: str = "HEAD"
    ) -> str:
        content = await repo.read_file(artifact.file_path, rev)
        if content is None:
            return artifact.ast_signature
        info = self.slicer.extract_artifact_signature(content, artifact.symbol_path)
        return info.skeleton if info.found else artifact.ast_signature

    async def _current_code(
        self,
        artifact: CodeArtifact,
        repo: GitRepository,
        *,
        for_test: bool,
        rev: str = "HEAD",
    ) -> str:
        content = await repo.read_file(artifact.file_path, rev)
        if content is not None:
            source = self.slicer.extract_node_source(content, artifact.symbol_path)
            if source is not None:
                return source
            if for_test:
                # A new test function in an existing test module: show the
                # module so the worker can extend it coherently.
                return content
        if artifact.ast_signature:
            return artifact.ast_signature
        return f"# Target artifact {artifact.symbol_path}\n# Create it in {artifact.file_path}."

    # --------------------------------------------------------------- submit
    # ---------------------------------------------------------------- split
    async def execute_graph_split(
        self, lease: ActiveLeaseContext, req: RequestTaskSplitRequest
    ) -> RequestTaskSplitResponse:
        parent = await self.session.get(DagTask, lease.task_id)
        if parent is None or parent.status != TaskStatus.LEASED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Parent task is not LEASED.",
            )
        workspace_id = parent.workspace_id
        workspace = await self._workspace(workspace_id)

        repo = GitRepository(workspace.repo_path, workspace.main_branch)
        existing_files = await repo.list_files()

        nodes: list[SubTaskNodeSpec] = []
        try:
            for sub in req.sub_tasks:
                write_paths = LockAlgebra.promote_new_files(
                    sub.write_set_symbols, existing_files
                )
                write_locks, ancestor_locks = LockAlgebra.expand_write_set(write_paths)

                read_artifacts = await ensure_artifacts(
                    self.session,
                    workspace,
                    sub.read_set_symbols,
                    default_kind=ArtifactKind.CONTRACT,
                    repo=repo,
                    slicer=self.slicer,
                )
                target_artifacts: list[CodeArtifact] = []
                for write_symbol in sub.write_set_symbols:
                    target_artifacts.extend(
                        await ensure_artifacts(
                            self.session,
                            workspace,
                            [write_symbol],
                            default_kind=infer_kind(
                                write_symbol, sub.task_type.value
                            ),
                            repo=repo,
                            slicer=self.slicer,
                        )
                    )

                read_ids = {
                    *[artifact.artifact_id for artifact in read_artifacts],
                    *parent.read_artifact_ids,
                }
                test_ids = (
                    set(parent.test_artifact_ids)
                    if sub.task_type
                    in {ApiTaskType.IMPLEMENT_BODY, ApiTaskType.FIX_INTEGRATION}
                    else set()
                )

                nodes.append(
                    SubTaskNodeSpec(
                        local_id=sub.local_key,
                        title=f"{parent.title} :: {sub.local_key}"[:256],
                        spec_prompt=sub.specification,
                        task_type=sub.task_type.value,
                        read_artifact_ids=sorted(read_ids),
                        target_artifact_ids=[
                            artifact.artifact_id for artifact in target_artifacts
                        ],
                        test_artifact_ids=sorted(test_ids),
                        write_locks=write_locks,
                        ancestor_locks=ancestor_locks,
                        depends_on_local_ids=sub.depends_on_local_keys,
                    )
                )

            result = await self.mutator.split_task_into_subdag(
                workspace_id, parent.task_id, nodes
            )
            await self.session.commit()
            await scheduler_core_manager.sync_tasks(
                self.session,
                workspace_id,
                [parent.task_id, *result.child_ids],
            )
        except PermissionError as exc:
            await self.session.rollback()
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
        except ValueError as exc:
            await self.session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            )

        return RequestTaskSplitResponse(
            parent_task_id=result.parent_task_id,
            parent_new_status=ApiTaskStatus.SPLIT_ARCHIVED,
            spawned_task_map={
                local_id: task_id for local_id, task_id in result.local_map.items()
            },
        )

    # ------------------------------------------------------------- escalate
    async def escalate_and_propagate_stale(
        self, lease: ActiveLeaseContext, req: EscalateContractDefectRequest
    ) -> EscalateContractDefectResponse:
        task = await self.session.get(DagTask, lease.task_id)
        if task is None or task.status != TaskStatus.LEASED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Task is not LEASED.",
            )
        workspace_id = task.workspace_id
        workspace = await self._workspace(workspace_id)

        artifact = await self.session.get(CodeArtifact, req.defective_artifact_id)
        if artifact is None or artifact.workspace_id != workspace_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Defective artifact not found in the active workspace.",
            )

        repo = GitRepository(workspace.repo_path, workspace.main_branch)
        existing_files = await repo.list_files()
        write_paths = LockAlgebra.promote_new_files(
            [artifact.symbol_path], existing_files
        )
        write_locks, ancestor_locks = LockAlgebra.expand_write_set(write_paths)
        affected_fields = sorted({_leaf(path) for path in req.breaking_symbol_paths})

        try:
            result = await self.mutator.escalate_and_propagate_blast_radius(
                workspace_id=workspace_id,
                reporting_task_id=task.task_id,
                defective_artifact_id=artifact.artifact_id,
                affected_fields=affected_fields,
                reason=req.defect_reason,
                proposed_signature=req.proposed_ast_signature,
                revise_write_locks=write_locks,
                revise_ancestor_locks=ancestor_locks,
            )
            await self.session.commit()
            await scheduler_core_manager.sync_tasks(
                self.session,
                workspace_id,
                [
                    result["suspended_task_id"],
                    *result["interrupted_running_task_ids"],
                    result["contract_revision_task_id"],
                ],
            )
        except ValueError as exc:
            await self.session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            )

        return EscalateContractDefectResponse(
            suspended_task_id=result["suspended_task_id"],
            contract_revision_task_id=result["contract_revision_task_id"],
            stale_marked_artifact_ids=result["stale_marked_artifact_ids"],
            interrupted_running_task_ids=result["interrupted_running_task_ids"],
        )

    # --------------------------------------------------------------- inject
    async def inject_prerequisite_node(
        self, lease: ActiveLeaseContext, req: InjectDependencyTaskRequest
    ) -> InjectDependencyTaskResponse:
        task = await self.session.get(DagTask, lease.task_id)
        if task is None or task.status != TaskStatus.LEASED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Task is not LEASED.",
            )
        workspace_id = task.workspace_id
        workspace = await self._workspace(workspace_id)

        repo = GitRepository(workspace.repo_path, workspace.main_branch)
        existing_files = await repo.list_files()
        requested_paths = sorted(
            {req.prerequisite_symbol_path, *req.prerequisite_write_set}
        )
        write_paths = LockAlgebra.promote_new_files(requested_paths, existing_files)
        write_locks, ancestor_locks = LockAlgebra.expand_write_set(write_paths)

        target_artifacts = await ensure_artifacts(
            self.session,
            workspace,
            [req.prerequisite_symbol_path],
            default_kind=infer_kind(req.prerequisite_symbol_path, "DESIGN_CONTRACT"),
            repo=repo,
            slicer=self.slicer,
        )
        if not target_artifacts:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Could not resolve the prerequisite artifact.",
            )

        try:
            new_task_id = await self.mutator.inject_prerequisite_node(
                workspace_id=workspace_id,
                current_task_id=task.task_id,
                prerequisite_title=f"Prerequisite: {_leaf(req.prerequisite_symbol_path)}",
                prerequisite_spec=req.prerequisite_specification,
                prerequisite_target_ids=[
                    artifact.artifact_id for artifact in target_artifacts
                ],
                prerequisite_write_locks=write_locks,
                prerequisite_ancestor_locks=ancestor_locks,
            )
            await self.session.commit()
            await scheduler_core_manager.sync_tasks(
                self.session, workspace_id, [task.task_id, new_task_id]
            )
        except ValueError as exc:
            await self.session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            )

        return InjectDependencyTaskResponse(
            current_task_id=task.task_id,
            current_task_new_status=ApiTaskStatus.BLOCKED,
            injected_prerequisite_task_id=new_task_id,
        )

    # --------------------------------------------------------------- lookup
    async def lookup_symbol_signatures(
        self, req: QuerySymbolSignatureRequest
    ) -> QuerySymbolSignatureResponse:
        workspace_id = await self._default_workspace_id()
        queries = sorted({LockAlgebra.normalize(query) for query in req.symbol_queries})
        conditions: list[Any] = []
        for query in queries:
            conditions.append(CodeArtifact.symbol_path == query)
            conditions.append(CodeArtifact.symbol_path.like(f"{query}::%"))
            conditions.append(CodeArtifact.symbol_path.like(f"{query}/%"))

        rows = (
            await self.session.execute(
                select(CodeArtifact)
                .where(
                    CodeArtifact.workspace_id == workspace_id,
                    or_(*conditions),
                )
                .order_by(CodeArtifact.symbol_path.asc())
                .limit(200)
            )
        ).scalars().all()

        records = [
            SymbolSignatureRecord(
                artifact_id=artifact.artifact_id,
                symbol_path=artifact.symbol_path,
                kind=ApiArtifactKind(artifact.kind.value),
                status=ApiArtifactStatus(artifact.status.value),
                ast_signature=artifact.ast_signature,
                docstring=artifact.docstring or None,
            )
            for artifact in rows
        ]
        return QuerySymbolSignatureResponse(results=records)


def _estimate_tokens(text: str | None) -> int:
    return max(0, (len(text or "") + 3) // 4)


def _fit_bundle(bundle: TaskBundleResponse, max_context_tokens: int) -> None:
    """Truncate the largest sliced fields until the hard token ceiling holds.

    The write set and task specification are never dropped; over-long contents
    are halved with an explicit marker so the worker knows context was cut.
    """
    views = [
        *bundle.read_only_signatures,
        *bundle.frozen_tests,
        *bundle.mutable_stubs,
    ]

    def total() -> int:
        return (
            _estimate_tokens(bundle.specification)
            + _estimate_tokens(bundle.last_error_traceback)
            + sum(_estimate_tokens(view.sliced_content) for view in views)
        )

    while total() > max_context_tokens:
        largest = max(views, key=lambda view: len(view.sliced_content), default=None)
        if largest is not None and len(largest.sliced_content) > 256:
            content = largest.sliced_content
            largest.sliced_content = (
                content[: max(128, len(content) // 2)] + "\n# ...[context truncated]"
            )
            continue
        if bundle.specification and len(bundle.specification) > 256:
            specification = bundle.specification
            bundle.specification = (
                specification[: max(128, len(specification) // 2)] + "...[truncated]"
            )
            continue
        if bundle.last_error_traceback and len(bundle.last_error_traceback) > 256:
            traceback = bundle.last_error_traceback
            bundle.last_error_traceback = (
                "...[truncated] "
                + traceback[-max(128, len(traceback) // 2):]
            )
            continue
        break

    bundle.estimated_tokens = total()


__all__ = ["PostgresKernelEngineService"]
