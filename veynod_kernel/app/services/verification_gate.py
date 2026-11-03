"""Four-stage deterministic verification gate + atomic Git merge.

This is the only path into the main branch. There is no LLM review:
  1. write-set boundary guard (files and symbols)
  2. frozen contract signature conservation (AST skeleton SHA-256)
  3. static type check
  4. associated unit tests (or test collection for WRITE_TEST)
Then the commit is fast-forwarded into the main branch with a compare-and-swap
``git update-ref`` while a per-workspace advisory merge lock is held.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contracts.work_scope import WorkScope
from typing import Awaitable, Callable, Sequence
from uuid import UUID

from fastapi import HTTPException, status as http_status
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.engine.lock_algebra import LockAlgebra
from app.engine.scheduler import DisjointWriteSetScheduler
from app.engine.scheduler_core import scheduler_core_manager
from app.mcp_server.syscalls import GateCheckResult, GateStage, SubmitPatchResponse, TaskStatus
from app.models import (
    ArtifactStatus,
    CodeArtifact,
    DagTask,
    TaskStatus as ModelTaskStatus,
    TaskType,
    Workspace,
)
from app.services.artifact_snapshot import artifact_snapshot_cache
from app.services.artifact_sync import ArtifactSynchronizer
from app.services.ast_slicer import Slicer
from app.services.cell_registry import assert_workspace_in_current_cell
from app.services.edge_rebuild import EdgeRebuildJob, edge_rebuild_service
from app.services.event_bus import EventBus
from app.services.gate_sandbox import run_sandboxed_command
from app.services.git_ops import CommandResult, GitError, GitRepository
from app.services.runtime_metrics import get_runtime_metrics
from app.services.worktree_pool import worktree_pool_manager

class MergeFenceLost(RuntimeError):
    """The durable worker lease was lost before the Git CAS merge."""


async def assert_task_mergeable(
    session: AsyncSession, task: DagTask, expected_lease_hash: str | None
) -> None:
    """Fence the merge critical section on the *task* lease, not just the coordinator.

    The row lock is held until the surrounding transaction commits, so a reaper
    or a re-lease cannot change the task between this check and
    ``on_task_committed``.  A stale merge intent from an expired/reassigned task
    must never advance the main branch.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT status::text AS status, lease_token_hash
                FROM dag_tasks
                WHERE task_id = :task_id
                FOR UPDATE
                """
            ),
            {"task_id": task.task_id},
        )
    ).mappings().first()
    if (
        row is None
        or row["status"] != "VERIFYING"
        or not expected_lease_hash
        or row["lease_token_hash"] != expected_lease_hash
    ):
        raise MergeFenceLost(
            f"task {task.task_id} is no longer VERIFYING under lease "
            f"{str(expected_lease_hash)[:12]}"
        )


_MERGE_LOCKS: dict = {}


def _workspace_merge_lock(workspace_id) -> asyncio.Lock:
    lock = _MERGE_LOCKS.get(workspace_id)
    if lock is None:
        lock = asyncio.Lock()
        _MERGE_LOCKS[workspace_id] = lock
    return lock


# Types whose pinned targets may change. `protected` means "pinned and not
# allowed to change", so leaving a type out freezes its own targets — which is
# what froze every file-level artifact: a file's signature is its whole
# bodiless skeleton, so adding a function to the target file changed the hash
# and the gate refused the task's own work.
#
# The contract-defining types stay listed because they legitimately change a
# contract, and the gate holds them to it. An implementation type is handed
# the file to change, so its target must be mutable — that is what pinning it
# for is.
_ALLOWED_TO_CHANGE = {
    TaskType.DESIGN_CONTRACT,
    TaskType.REVISE_CONTRACT,
    TaskType.WRITE_TEST,
    TaskType.FIX_INTEGRATION,
    TaskType.IMPLEMENT_BODY,
}
_TESTS_REQUIRED = {TaskType.IMPLEMENT_BODY, TaskType.FIX_INTEGRATION}


@dataclass
class _AttemptResult:
    success: bool
    retryable: bool
    gate_results: list[GateCheckResult]
    new_sha: str | None = None
    ready_task_ids: list[UUID] | None = None
    changed_files: list[str] | None = None
    pool_wait_ms: int = 0


@dataclass
class _MergeOutcome:
    retry: bool
    response: SubmitPatchResponse | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _delta(seconds: int) -> timedelta:
    return timedelta(seconds=seconds)


def _quote(path: str) -> str:
    if os.name == "nt":
        if any(char in path for char in ' \t"&|<>^'):
            return '"' + path.replace('"', '""') + '"'
        return path
    return shlex.quote(path)


def _truncate(output: str) -> str:
    limit = settings.gate_output_chars
    return output if len(output) <= limit else output[-limit:]


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _gate(
    stage: GateStage,
    passed: bool,
    code: int,
    diagnostics: str = "",
    duration_ms: int = 0,
) -> GateCheckResult:
    return GateCheckResult(
        stage=stage,
        passed=passed,
        exit_code=code,
        diagnostics=_truncate(diagnostics),
        duration_ms=duration_ms,
    )


def _safe_path(worktree: Path, relative: str) -> Path | None:
    root = worktree.resolve()
    candidate = (worktree / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _source_hash(source: str | None) -> str | None:
    if source is None:
        return None
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _symbol_source(slicer: Slicer, source: str, symbol_path: str) -> str | None:
    if "::" in symbol_path:
        return slicer.extract_node_source(source, symbol_path)
    return source


def sensitive_file_unchanged(
    slicer: Slicer,
    file_path: str,
    base_content: str,
    head_content: str,
    symbol_paths: list[str],
) -> bool:
    """True when only unrelated function bodies changed and the file contract is stable.

    - whole-file skeleton (imports, module-level globals, class attributes,
      signatures, docstrings) must be unchanged;
    - each read/test/target symbol's full source must be unchanged.
    A function body in the same file that is not part of this task is allowed.
    """
    base_file = slicer.extract_artifact_signature(base_content, file_path)
    head_file = slicer.extract_artifact_signature(head_content, file_path)
    if (
        not base_file.found
        or not head_file.found
        or base_file.signature_hash != head_file.signature_hash
    ):
        return False
    for symbol_path in symbol_paths:
        base_source = _symbol_source(slicer, base_content, symbol_path)
        head_source = _symbol_source(slicer, head_content, symbol_path)
        if base_source is None or head_source is None:
            return False
        if _source_hash(base_source) != _source_hash(head_source):
            return False
    return True


@dataclass(frozen=True)
class VerifiedPatch:
    base_sha: str
    task_commit_sha: str
    changed_files: list[str]
    gate_results: list[GateCheckResult]
    pool_wait_ms: int


@dataclass(frozen=True)
class GateVerification:
    verified: VerifiedPatch | None
    rejection: SubmitPatchResponse | None


class DeterministicVerificationGate:
    def __init__(
        self,
        session: AsyncSession,
        slicer: Slicer,
        scheduler: DisjointWriteSetScheduler,
        event_bus: EventBus,
    ) -> None:
        self.session = session
        self.slicer = slicer
        self.scheduler = scheduler
        self.event_bus = event_bus
        self.synchronizer = ArtifactSynchronizer(session, slicer)

    async def verify_and_merge_patch(
        self,
        task: DagTask,
        workspace: Workspace,
        git_diff: str,
        touched_symbols: Sequence[str],
        lease_token_hash: str,
        *,
        before_merge: Callable[[], Awaitable[None]] | None = None,
    ) -> SubmitPatchResponse:
        repo = GitRepository(workspace.repo_path, workspace.main_branch)
        await repo.validate_usable()

        # Single-use lease transition: two concurrent submissions with the same
        # token must not both enter the gate.
        transitioned = await self.session.execute(
            text(
                """
                UPDATE dag_tasks
                SET status = 'VERIFYING',
                    lease_expires_at = NOW() + (:verify_seconds * INTERVAL '1 second'),
                    updated_at = NOW()
                WHERE task_id = :task_id
                  AND status = 'LEASED'
                  AND lease_token_hash = :lease_token_hash
                  AND lease_expires_at > NOW()
                RETURNING task_id
                """
            ),
            {
                "task_id": task.task_id,
                "lease_token_hash": lease_token_hash,
                "verify_seconds": settings.verify_lease_seconds,
            },
        )
        if transitioned.first() is None:
            await self.session.rollback()
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="Lease is no longer active (already submitted, expired, or revoked).",
            )
        await self.session.commit()
        get_runtime_metrics().submit_started()

        last_results: list[GateCheckResult] = []
        last_error = "Verification failed."

        for attempt in range(1, settings.max_merge_attempts + 1):
            base_sha = await repo.head_sha()
            attempt_result = await self._run_attempt(
                repo=repo,
                task=task,
                workspace=workspace,
                base_sha=base_sha,
                git_diff=git_diff,
                touched_symbols=touched_symbols,
            )
            last_results = attempt_result.gate_results

            if attempt_result.success:
                if before_merge is not None:
                    await before_merge()
                sensitive_by_file, sensitive_paths = await self._sensitive_metadata(task)
                await self._precompute_sensitive_snapshots(
                    repo, base_sha, sensitive_paths
                )
                await self.session.close()
                lock_started = time.monotonic()
                try:
                    async with _workspace_merge_lock(workspace.workspace_id):
                        merge_outcome = await self._merge(
                            repo=repo,
                            task=task,
                            workspace=workspace,
                            base_sha=base_sha,
                            new_sha=attempt_result.new_sha or "",
                            changed_files=attempt_result.changed_files or [],
                            gate_results=attempt_result.gate_results,
                            sensitive_by_file=sensitive_by_file,
                            lock_wait_ms=_elapsed_ms(lock_started),
                            gate_pool_wait_ms=attempt_result.pool_wait_ms,
                            before_merge=before_merge,
                        )
                except MergeFenceLost:
                    raise
                except Exception as exc:  # noqa: BLE001 - surfaced to the caller
                    return await self._reject(
                        task, last_results, f"[Kernel Merge Error] {exc}"
                    )

                if merge_outcome.retry:
                    last_error = "[Merge Retry] main advanced during merge."
                    if attempt < settings.max_merge_attempts:
                        continue
                    break
                assert merge_outcome.response is not None
                return merge_outcome.response

            failed = next((r for r in attempt_result.gate_results if not r.passed), None)
            last_error = (
                f"[{failed.stage.value}] exit={failed.exit_code}\n{failed.diagnostics}"
                if failed
                else "Verification failed without a stage result."
            )

            if attempt_result.retryable and attempt < settings.max_merge_attempts:
                # The main branch moved while we verified. Re-run the whole gate
                # on the new HEAD; write-sets are disjoint, so the patch should
                # still apply, but integrated tests must be re-proven.
                continue
            break

        return await self._reject(task, last_results, last_error)

    async def verify_patch(
        self,
        task: DagTask,
        workspace: Workspace,
        git_diff: str,
        touched_symbols: Sequence[str],
        lease_token_hash: str,
    ) -> "GateVerification":
        """Run the gate only; do not merge.

        The caller persists a merge intent and a per-repository coordinator
        performs the Git/DB critical section.  This keeps gate concurrency
        independent from the serialized main-branch commit point.
        """
        repo = GitRepository(workspace.repo_path, workspace.main_branch)
        await repo.validate_usable()
        transitioned = await self.session.execute(
            text(
                """
                UPDATE dag_tasks
                SET status = 'VERIFYING',
                    lease_expires_at = NOW() + (:verify_seconds * INTERVAL '1 second'),
                    updated_at = NOW()
                WHERE task_id = :task_id
                  AND status = 'LEASED'
                  AND lease_token_hash = :lease_token_hash
                  AND lease_expires_at > NOW()
                RETURNING task_id
                """
            ),
            {
                "task_id": task.task_id,
                "lease_token_hash": lease_token_hash,
                "verify_seconds": settings.verify_lease_seconds,
            },
        )
        if transitioned.first() is None:
            await self.session.rollback()
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="Lease is no longer active (already submitted, expired, or revoked).",
            )
        await self.session.commit()
        get_runtime_metrics().submit_started()

        last_results: list[GateCheckResult] = []
        last_error = "Verification failed."
        for attempt in range(1, settings.max_merge_attempts + 1):
            base_sha = await repo.head_sha()
            attempt_result = await self._run_attempt(
                repo=repo,
                task=task,
                workspace=workspace,
                base_sha=base_sha,
                git_diff=git_diff,
                touched_symbols=touched_symbols,
            )
            last_results = attempt_result.gate_results
            if attempt_result.success:
                return GateVerification(
                    verified=VerifiedPatch(
                        base_sha=base_sha,
                        task_commit_sha=attempt_result.new_sha or "",
                        changed_files=list(attempt_result.changed_files or []),
                        gate_results=attempt_result.gate_results,
                        pool_wait_ms=attempt_result.pool_wait_ms,
                    ),
                    rejection=None,
                )
            failed = next((item for item in attempt_result.gate_results if not item.passed), None)
            last_error = (
                f"[{failed.stage.value}] exit={failed.exit_code}\n{failed.diagnostics}"
                if failed
                else "Verification failed without a stage result."
            )
            if attempt_result.retryable and attempt < settings.max_merge_attempts:
                continue
            break
        response = await self._reject(task, last_results, last_error)
        return GateVerification(verified=None, rejection=response)

    async def merge_verified(
        self,
        task: DagTask,
        workspace: Workspace,
        verified: "VerifiedPatch",
        *,
        before_merge: Callable[[], Awaitable[None]] | None = None,
    ) -> _MergeOutcome:
        """Merge an already verified task commit; called by the coordinator."""
        repo = GitRepository(workspace.repo_path, workspace.main_branch)
        await repo.validate_usable()
        sensitive_by_file, sensitive_paths = await self._sensitive_metadata(task)
        await self._precompute_sensitive_snapshots(
            repo, verified.base_sha, sensitive_paths
        )
        await self.session.close()
        lock_started = time.monotonic()
        async with _workspace_merge_lock(workspace.workspace_id):
            return await self._merge(
                repo=repo,
                task=task,
                workspace=workspace,
                base_sha=verified.base_sha,
                new_sha=verified.task_commit_sha,
                changed_files=verified.changed_files,
                gate_results=verified.gate_results,
                sensitive_by_file=sensitive_by_file,
                lock_wait_ms=_elapsed_ms(lock_started),
                gate_pool_wait_ms=verified.pool_wait_ms,
                before_merge=before_merge,
            )

    # ---------------------------------------------------------------- attempt
    async def _run_attempt(
        self,
        repo: GitRepository,
        task: DagTask,
        workspace: Workspace,
        base_sha: str,
        git_diff: str,
        touched_symbols: Sequence[str],
    ) -> _AttemptResult:
        gate_results: list[GateCheckResult] = []
        pool = await worktree_pool_manager.get(workspace)
        pool_started = time.monotonic()
        worktree = await pool.acquire(base_sha)
        pool_wait_ms = _elapsed_ms(pool_started)
        tmp_root = Path(tempfile.mkdtemp(prefix=f"veynod-gate-{task.task_id}-"))
        patch_file = tmp_root / "submit.patch"
        patch_file.write_text(git_diff, encoding="utf-8")

        try:
            applied = await repo.apply_patch(worktree, patch_file)
            if not applied.ok:
                current_head = await repo.head_sha()
                return _AttemptResult(
                    success=False,
                    retryable=current_head != base_sha,
                    gate_results=[
                        _gate(
                            GateStage.WRITE_SET_BOUNDARY,
                            False,
                            applied.returncode,
                            "[Stage 0: Git Apply Conflict]\n" + applied.output,
                        )
                    ],
                )
            gate_results.append(
                _gate(GateStage.WRITE_SET_BOUNDARY, True, 0, "patch applied", 0)
            )

            await repo.stage_all(worktree)
            changed_files = await repo.changed_files_staged(worktree, base_sha)
            if not changed_files:
                gate_results[-1] = _gate(
                    GateStage.WRITE_SET_BOUNDARY,
                    False,
                    1,
                    "[Stage 1: Empty Patch] No files were modified.",
                )
                return _AttemptResult(False, False, gate_results)

            # Symlinks can escape the sandbox when the gate reads files or runs
            # tests; reject them outright.
            symlinks = []
            for path in changed_files:
                candidate = worktree
                for segment in Path(path).parts:
                    candidate = candidate / segment
                    if candidate.is_symlink():
                        symlinks.append(path)
                        break
            if symlinks:
                gate_results[-1] = _gate(
                    GateStage.WRITE_SET_BOUNDARY,
                    False,
                    1,
                    f"[Stage 1: Symlink Rejected] {sorted(symlinks)}",
                )
                return _AttemptResult(False, False, gate_results)

            allowed_files = LockAlgebra.file_paths_for_locks(task.write_locks)
            violations = [
                path
                for path in changed_files
                if not any(
                    path == allowed or path.startswith(f"{allowed}/")
                    for allowed in allowed_files
                )
            ]
            if violations:
                gate_results[-1] = _gate(
                    GateStage.WRITE_SET_BOUNDARY,
                    False,
                    1,
                    f"[Stage 1: Write-Set Violation] Unauthorized files {sorted(violations)}. "
                    f"Allowed file scope: {sorted(allowed_files)}",
                )
                return _AttemptResult(False, False, gate_results)

            if task.lease_scope is not None:
                try:
                    scope = WorkScope.from_dict(task.lease_scope)
                    if task.lease_scope_hash != scope.scope_hash:
                        raise PermissionError("lease WorkScope hash mismatch")
                    scope.assert_files_allowed(changed_files)
                except (ValueError, PermissionError) as exc:
                    gate_results[-1] = _gate(
                        GateStage.WRITE_SET_BOUNDARY, False, 1,
                        f"[Stage 1: WorkScope Violation] {exc}",
                    )
                    return _AttemptResult(False, False, gate_results)

            gate_results[-1] = _gate(
                GateStage.WRITE_SET_BOUNDARY,
                True,
                0,
                f"changed={sorted(changed_files)} allowed={sorted(allowed_files)}",
            )

            # Stage 1.5: derive the changed AST scope from the actual base and
            # patched files.  touched_symbols is Runner-supplied and is not
            # trusted here.  Module-level changes require file-level scope.
            scope_started = time.monotonic()
            scope_ok, scope_detail = await self._check_ast_scope(
                repo, worktree, base_sha, task, changed_files
            )
            gate_results.append(
                _gate(
                    GateStage.AST_SCOPE_DIFF,
                    scope_ok,
                    0 if scope_ok else 1,
                    scope_detail,
                    _elapsed_ms(scope_started),
                )
            )
            if not scope_ok:
                return _AttemptResult(False, False, gate_results)

            # Stage 2: frozen contract conservation.
            stage2_started = time.monotonic()
            contract_failure = await self._check_contracts(
                worktree, task, workspace, changed_files
            )
            # End the metadata read transaction before pyright/ruff/pytest.
            await self.session.commit()
            if contract_failure is not None:
                gate_results.append(
                    _gate(
                        GateStage.CONTRACT_HASH_GUARD,
                        False,
                        1,
                        contract_failure,
                        _elapsed_ms(stage2_started),
                    )
                )
                return _AttemptResult(False, False, gate_results)
            gate_results.append(
                _gate(
                    GateStage.CONTRACT_HASH_GUARD,
                    True,
                    0,
                    "frozen signatures conserved",
                    _elapsed_ms(stage2_started),
                )
            )

            # Stage 3: incremental static type check + linter.
            stage3_started = time.monotonic()
            typecheck = await self._static_checks(worktree, changed_files)
            gate_results.append(
                _gate(
                    GateStage.STATIC_TYPECHECK,
                    typecheck.ok,
                    typecheck.returncode,
                    typecheck.output,
                    _elapsed_ms(stage3_started),
                )
            )
            if not typecheck.ok:
                return _AttemptResult(False, False, gate_results)

            # Stage 4: tests / collection.
            stage4_started = time.monotonic()
            test_result = await self._run_tests(worktree, task)
            gate_results.append(
                _gate(
                    GateStage.UNIT_TEST_SUITE,
                    test_result.ok,
                    test_result.returncode,
                    test_result.output,
                    _elapsed_ms(stage4_started),
                )
            )
            if not test_result.ok:
                return _AttemptResult(False, False, gate_results)

            # Commit only the verified changed files; test caches stay out.
            await repo.stage_paths(worktree, changed_files)
            new_sha = await repo.commit(
                worktree,
                f"veynod(task): {task.title} [{task.task_id}]",
            )
            self._register_commit_snapshots(
                repo, worktree, base_sha, new_sha, changed_files
            )
            return _AttemptResult(
                success=True,
                retryable=False,
                gate_results=gate_results,
                new_sha=new_sha,
                changed_files=changed_files,
                pool_wait_ms=pool_wait_ms,
            )
        finally:
            await pool.release(worktree)
            shutil.rmtree(tmp_root, ignore_errors=True)

    def _register_commit_snapshots(
        self,
        repo: GitRepository,
        worktree: Path,
        base_sha: str,
        new_sha: str,
        changed_files: list[str],
    ) -> None:
        """Precompute immutable snapshots outside the merge lock."""
        artifact_snapshot_cache.register_commit(new_sha, base_sha, changed_files)
        for path in changed_files:
            source_path = _safe_path(worktree, path)
            content: str | None = None
            if (
                source_path is not None
                and source_path.is_file()
                and not source_path.is_symlink()
            ):
                try:
                    content = source_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    content = None
            artifact_snapshot_cache.register_worktree_snapshot(
                repo.repo_path, new_sha, path, content, self.slicer
            )

    async def _sensitive_metadata(
        self, task: DagTask
    ) -> tuple[dict[str, list[CodeArtifact]], list[str]]:
        """Load immutable artifact identity data before the critical section."""
        sensitive_ids = [
            *task.read_artifact_ids,
            *task.test_artifact_ids,
            *task.target_artifact_ids,
        ]
        artifacts: list[CodeArtifact] = []
        if sensitive_ids:
            artifacts = list(
                (
                    await self.session.execute(
                        select(CodeArtifact).where(
                            CodeArtifact.artifact_id.in_(sensitive_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
        by_file: dict[str, list[CodeArtifact]] = {}
        for artifact in artifacts:
            by_file.setdefault(artifact.file_path, []).append(artifact)
        write_files = LockAlgebra.file_paths_for_locks(task.write_locks)
        # The merge critical section below is Git/DB work; release the read
        # transaction before entering it.
        await self.session.commit()
        return by_file, sorted(set(by_file) | set(write_files))

    async def _precompute_sensitive_snapshots(
        self, repo: GitRepository, base_sha: str, paths: list[str]
    ) -> None:
        if not paths:
            return
        await artifact_snapshot_cache.ensure_commit_snapshots(
            repo, base_sha, paths, self.slicer
        )
        try:
            current_head = await repo.head_sha()
        except Exception:  # noqa: BLE001 - the lock path can still load it
            return
        if current_head != base_sha:
            await artifact_snapshot_cache.ensure_commit_snapshots(
                repo, current_head, paths, self.slicer
            )

    # ------------------------------------------------------------ ast scope
    async def _check_ast_scope(
        self,
        repo: GitRepository,
        worktree: Path,
        base_sha: str,
        task: DagTask,
        changed_files: Sequence[str],
    ) -> tuple[bool, str]:
        """Independently derive the modified symbols from the actual patch.

        The Runner's ``touched_symbols`` is advisory.  We compare the base and
        patched file snapshots and require:
        * every changed/added/removed symbol to be covered by the task's
          write locks;
        * any module-level change (file skeleton, import, global, signature or
          docstring) to be covered by a *file-level* write lock;
        * non-Python or unparsable changed files to have a file-level lock.
        """
        file_locks = {lock for lock in task.write_locks if "::" not in lock}
        scope = WorkScope.from_dict(task.lease_scope) if task.lease_scope else None

        def file_authorized(path: str) -> bool:
            if path in file_locks:
                return scope is None or scope.allows_symbol(path)
            # A scoped task uses an ancestor lock for scheduling while its
            # frozen path grant supplies the exact file boundary.
            return bool(
                scope is not None
                and scope.allows_symbol(path)
                and any(path.startswith(f"{lock}/") for lock in file_locks)
            )

        violations: list[str] = []
        for file_path in changed_files:
            safe = _safe_path(worktree, file_path)
            if safe is None or not safe.is_file() or safe.is_symlink():
                violations.append(f"{file_path}: not a regular file")
                continue
            patched = safe.read_text(encoding="utf-8", errors="replace")
            base = await repo.read_file(file_path, base_sha)
            if not file_path.endswith(".py"):
                if not file_authorized(file_path):
                    violations.append(
                        f"{file_path}: non-Python change requires a file-level lock"
                    )
                continue
            if base is None:
                if not file_authorized(file_path):
                    violations.append(
                        f"{file_path}: new file requires a file-level lock"
                    )
                continue

            base_snapshot = self.slicer.file_snapshot(base, file_path)
            patched_snapshot = self.slicer.file_snapshot(patched, file_path)
            base_symbols = {
                item["symbol_path"]: item for item in base_snapshot["symbols"]
            }
            patched_symbols = {
                item["symbol_path"]: item for item in patched_snapshot["symbols"]
            }
            changed = {
                symbol
                for symbol in set(base_symbols) | set(patched_symbols)
                if base_symbols.get(symbol, {}).get("source_hash")
                != patched_symbols.get(symbol, {}).get("source_hash")
            }
            module_changed = (
                base_snapshot["signature_hash"]
                != patched_snapshot["signature_hash"]
            )
            if module_changed and not file_authorized(file_path):
                violations.append(
                    f"{file_path}: module-level/signature change requires a "
                    f"file-level lock (have {sorted(task.write_locks)[:8]})"
                )
            if not changed and not module_changed:
                continue
            for symbol in sorted(changed):
                if not LockAlgebra.is_covered_by_write_set(
                    symbol, list(task.write_locks)
                ):
                    violations.append(
                        f"{symbol}: changed AST symbol is outside the write set"
                    )
                if scope is not None and not scope.allows_symbol(symbol):
                    violations.append(
                        f"{symbol}: changed AST symbol is outside the frozen WorkScope"
                    )
            # Added/removed symbols are structural module changes even when
            # the whole-file skeleton hash happens to stay stable.
            for symbol in sorted(set(patched_symbols) - set(base_symbols)):
                if not file_authorized(file_path):
                    violations.append(
                        f"{symbol}: added symbol requires a file-level lock"
                    )
            for symbol in sorted(set(base_symbols) - set(patched_symbols)):
                if not file_authorized(file_path):
                    violations.append(
                        f"{symbol}: removed symbol requires a file-level lock"
                    )
            if not patched_symbols and patched.strip() and not file_authorized(file_path):
                violations.append(
                    f"{file_path}: unparsable/empty AST requires a file-level lock"
                )

        if violations:
            return False, "[AST Scope Diff] " + "; ".join(violations[:20])
        return True, f"derived scope ok for {len(changed_files)} file(s)"

    # -------------------------------------------------------------- contracts
    async def _check_contracts(
        self,
        worktree: Path,
        task: DagTask,
        workspace: Workspace,
        changed_files: list[str],
    ) -> str | None:
        pinned_ids = [*task.target_artifact_ids, *task.test_artifact_ids]
        clauses = [CodeArtifact.file_path.in_(changed_files)]
        if pinned_ids:
            clauses.append(CodeArtifact.artifact_id.in_(pinned_ids))
        artifacts = list(
            (
                await self.session.execute(
                    select(CodeArtifact).where(
                        CodeArtifact.workspace_id == workspace.workspace_id,
                        or_(*clauses),
                    )
                )
            )
            .scalars()
            .all()
        )
        target_ids = set(task.target_artifact_ids)
        allowed_to_change = target_ids if task.type in _ALLOWED_TO_CHANGE else set()

        for artifact in artifacts:
            target = artifact.artifact_id in target_ids
            protected = (
                artifact.status in {ArtifactStatus.FROZEN, ArtifactStatus.COMMITTED}
                and artifact.artifact_id not in allowed_to_change
            )
            if not target and not protected:
                continue

            safe = _safe_path(worktree, artifact.file_path)
            if safe is None:
                return f"Unsafe artifact path: {artifact.file_path!r}"
            if not safe.exists():
                if target:
                    return f"Target artifact file missing after patch: {artifact.file_path}"
                if protected:
                    return f"Protected artifact file deleted: {artifact.file_path}"
                continue

            try:
                content = safe.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return f"Cannot read {artifact.file_path}: {exc}"

            info = self.slicer.extract_artifact_signature(content, artifact.symbol_path)
            if not info.found:
                if target:
                    return (
                        f"Target symbol '{artifact.symbol_path}' is missing from "
                        f"{artifact.file_path} after the patch."
                    )
                if protected:
                    return (
                        f"Protected frozen symbol '{artifact.symbol_path}' was removed "
                        f"from {artifact.file_path}."
                    )
                continue

            if protected and info.signature_hash != artifact.signature_hash:
                return (
                    f"[Contract Conservation Violation] Frozen signature of "
                    f"'{artifact.symbol_path}' changed. If the contract is defective, "
                    f"call escalate_contract_defect instead of mutating the signature."
                )

        return None

    # --------------------------------------------------------------- toolchain
    async def _static_checks(
        self, worktree: Path, changed_files: list[str]
    ) -> CommandResult:
        python_files = [path for path in changed_files if path.endswith(".py")]
        sections: list[str] = []

        if settings.skip_typecheck:
            sections.append("typecheck skipped by VN_SKIP_TYPECHECK")
        elif not python_files:
            sections.append("no Python files changed; typecheck skipped")
        else:
            command = settings.typecheck_cmd.strip()
            if command:
                command = f"{command} {' '.join(_quote(path) for path in python_files)}"
                typecheck = await run_sandboxed_command(command, worktree)
                sections.append(f"[typecheck]\n{typecheck.output}")
                if not typecheck.ok:
                    return CommandResult(typecheck.returncode, "\n".join(sections))
            else:
                sections.append("typecheck command disabled")

        if settings.skip_lint:
            sections.append("lint skipped by VN_SKIP_LINT")
            return CommandResult(0, "\n".join(sections))

        lint = settings.lint_cmd.strip()
        if not lint:
            sections.append("lint command disabled")
            return CommandResult(0, "\n".join(sections))
        if not python_files:
            sections.append("no Python files changed; lint skipped")
            return CommandResult(0, "\n".join(sections))

        first = lint.split()[0] if lint.split() else ""
        if Path(first).name.lower().startswith("ruff"):
            lint = f"{lint} {' '.join(_quote(path) for path in python_files)}"
        lint_result = await run_sandboxed_command(lint, worktree)
        sections.append(f"[lint]\n{lint_result.output}")
        if not lint_result.ok:
            return CommandResult(lint_result.returncode, "\n".join(sections))
        return CommandResult(0, "\n".join(sections))

    async def _test_files(self, task: DagTask) -> list[str]:
        files: list[str] = []
        if task.test_artifact_ids:
            rows = await self.session.execute(
                select(CodeArtifact.file_path).where(
                    CodeArtifact.artifact_id.in_(task.test_artifact_ids)
                )
            )
            files.extend(row[0] for row in rows.fetchall())
        targets = [str(value) for value in task.target_artifact_ids]
        if targets:
            # Affected-test selection: tests whose symbols reach a changed
            # target through the persisted symbol graph (depth <= 3).
            affected = await self.session.execute(
                text(
                    """
                    WITH RECURSIVE reach(artifact_id, depth) AS (
                        SELECT value, 0
                        FROM unnest(CAST(:targets AS UUID[])) AS value
                        UNION
                        SELECT se.caller_artifact_id, r.depth + 1
                        FROM symbol_edges se
                        JOIN reach r ON se.callee_artifact_id = r.artifact_id
                        WHERE r.depth < 3
                    )
                    SELECT DISTINCT ca.file_path
                    FROM code_artifacts ca
                    JOIN reach r ON r.artifact_id = ca.artifact_id
                    WHERE ca.kind::text = 'test'
                    """
                ),
                {"targets": targets},
            )
            files.extend(row[0] for row in affected.fetchall())
        return list(dict.fromkeys(files))

    async def _run_tests(self, worktree: Path, task: DagTask) -> CommandResult:
        test_files = await self._test_files(task)
        # Do not hold a pooled DB transaction while the test subprocess runs.
        await self.session.commit()
        suffix = " ".join(_quote(path) for path in test_files)

        def pytest_command(command: str) -> str:
            if not suffix:
                return command
            executable = command.strip().split()[0] if command.strip() else ""
            if Path(executable).name.lower().startswith("pytest"):
                return f"{command} {suffix}"
            return command

        if task.type in _TESTS_REQUIRED:
            return await run_sandboxed_command(pytest_command(settings.test_cmd), worktree)
        if task.type == TaskType.WRITE_TEST:
            return await run_sandboxed_command(pytest_command(settings.collect_cmd), worktree)
        return CommandResult(0, "test execution not required for this task type")

    # ------------------------------------------------------------------ merge
    async def _merge(
        self,
        repo: GitRepository,
        task: DagTask,
        workspace: Workspace,
        base_sha: str,
        new_sha: str,
        changed_files: list[str],
        gate_results: list[GateCheckResult],
        *,
        sensitive_by_file: dict[str, list[CodeArtifact]] | None = None,
        lock_wait_ms: int = 0,
        gate_pool_wait_ms: int = 0,
        before_merge: Callable[[], Awaitable[None]] | None = None,
    ) -> _MergeOutcome:
        started = time.monotonic()
        timings: dict[str, int] = {
            "lock_wait_ms": lock_wait_ms,
            "gate_pool_wait_ms": gate_pool_wait_ms,
            "dirty_check_ms": 0,
            "git_tree_commit_ms": 0,
            "artifact_db_ms": 0,
            "edge_enqueue_ms": 0,
            "edge_db_ms": 0,
            "kahn_db_ms": 0,
            "core_sync_ms": 0,
            "total_ms": 0,
        }
        # Fresh transaction for the critical section.
        await self.session.commit()
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"veynod:merge:{workspace.workspace_id}"},
        )

        try:
            # The coordinator lease is necessary but not sufficient: a stale
            # intent can outlive the task lease.  Hold the task row lock across
            # the Git CAS and DB finalization.
            await assert_task_mergeable(self.session, task, task.lease_token_hash)
            await assert_workspace_in_current_cell(
                self.session, workspace.workspace_id
            )
            if before_merge is not None:
                await before_merge()
            current_head = await repo.head_sha()
            if current_head != base_sha:
                if settings.fast_forward_merge:
                    outcome = await self._tree_merge(
                        repo=repo,
                        task=task,
                        workspace=workspace,
                        base_sha=base_sha,
                        current_head=current_head,
                        task_commit=new_sha,
                        changed_files=changed_files,
                        gate_results=gate_results,
                        sensitive_by_file=sensitive_by_file,
                        timings=timings,
                        started=started,
                        before_merge=before_merge,
                    )
                    if outcome is not None:
                        return outcome
                await self.session.rollback()
                return _MergeOutcome(retry=True)

            git_started = time.monotonic()
            updated = await repo.update_ref(new_sha, base_sha)
            timings["git_tree_commit_ms"] = _elapsed_ms(git_started)
            if not updated.ok:
                await self.session.rollback()
                if (await repo.head_sha()) != base_sha:
                    return _MergeOutcome(retry=True)
                return _MergeOutcome(
                    retry=False,
                    response=await self._reject(
                        task,
                        gate_results,
                        f"[Atomic Ref Update Retry Needed]\n{updated.output}",
                    ),
                )

            artifact_snapshot_cache.register_commit(
                new_sha, base_sha, changed_files
            )
            return await self._finalize_merge(
                repo=repo,
                task=task,
                workspace=workspace,
                previous_head=base_sha,
                new_sha=new_sha,
                changed_files=changed_files,
                gate_results=gate_results,
                merge_mode="gate_rerun",
                timings=timings,
                started=started,
            )
        except Exception:
            await self.session.rollback()
            raise

    async def _tree_merge(
        self,
        repo: GitRepository,
        task: DagTask,
        workspace: Workspace,
        base_sha: str,
        current_head: str,
        task_commit: str,
        changed_files: list[str],
        gate_results: list[GateCheckResult],
        *,
        sensitive_by_file: dict[str, list[CodeArtifact]] | None = None,
        timings: dict[str, int] | None = None,
        started: float | None = None,
        before_merge: Callable[[], Awaitable[None]] | None = None,
    ) -> _MergeOutcome | None:
        """Opt-in fast merge for write-set-orthogonal patches.

        The task was verified on `base_sha`; if the only commits that landed
        since then touched unrelated symbols, a clean Git tree merge is a valid
        fast-forward of the verified patch. Otherwise return None and let the
        caller re-run the full gate.
        """
        if timings is None:
            timings = {}
        if started is None:
            started = time.monotonic()
        dirty_started = time.monotonic()

        changed = artifact_snapshot_cache.changed_files_in_range(base_sha, current_head)
        if changed is None:
            # External commit (or a chain we did not produce): fall back to Git
            # once; snapshots for the sensitive paths are still reused.
            changed = set(await repo.changed_files_between(base_sha, current_head))

        if sensitive_by_file is None:
            sensitive_ids = [
                *task.read_artifact_ids,
                *task.test_artifact_ids,
                *task.target_artifact_ids,
            ]
            artifacts: list[CodeArtifact] = []
            if sensitive_ids:
                artifacts = list(
                    (
                        await self.session.execute(
                            select(CodeArtifact).where(
                                CodeArtifact.artifact_id.in_(sensitive_ids)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            by_file: dict[str, list[CodeArtifact]] = {}
            for artifact in artifacts:
                by_file.setdefault(artifact.file_path, []).append(artifact)
        else:
            by_file = sensitive_by_file

        write_files = LockAlgebra.file_paths_for_locks(task.write_locks)
        file_level_write = {
            lock for lock in task.write_locks if "::" not in lock
        }
        sensitive_files = set(by_file) | set(write_files)

        for path in changed & sensitive_files:
            if path in file_level_write:
                # A whole-file lock means nobody else should have touched it.
                return None
            base_snapshot = await artifact_snapshot_cache.snapshot_for(
                repo, base_sha, path, self.slicer
            )
            head_snapshot = await artifact_snapshot_cache.snapshot_for(
                repo, current_head, path, self.slicer
            )
            if base_snapshot is None or head_snapshot is None:
                return None
            # Whole-file skeleton: imports, module globals, class attributes,
            # signatures and docstrings.
            if base_snapshot.signature_hash != head_snapshot.signature_hash:
                return None

            file_artifacts = by_file.get(path, [])
            symbol_paths = [artifact.symbol_path for artifact in file_artifacts]
            if not symbol_paths and path in write_files:
                if base_snapshot.content_hash != head_snapshot.content_hash:
                    return None
                continue
            for symbol_path in symbol_paths:
                left = base_snapshot.source_hash_for(symbol_path)
                right = head_snapshot.source_hash_for(symbol_path)
                if left is None or right is None or left != right:
                    return None

        timings["dirty_check_ms"] = _elapsed_ms(dirty_started)
        if before_merge is not None:
            await before_merge()
        git_started = time.monotonic()
        tree = await repo.merge_tree(current_head, task_commit)
        if tree is None:
            return None
        try:
            merged_sha = await repo.commit_tree(
                tree,
                current_head,
                f"veynod(task): {task.title} [{task.task_id}]",
            )
        except GitError:
            return None

        updated = await repo.update_ref(merged_sha, current_head)
        timings["git_tree_commit_ms"] = _elapsed_ms(git_started)
        if not updated.ok:
            await self.session.rollback()
            if (await repo.head_sha()) != current_head:
                return _MergeOutcome(retry=True)
            return None

        # The merged tree has exactly the task commit's content for the files
        # the task touched; register the new commit for subsequent snapshots.
        artifact_snapshot_cache.copy_commit_snapshots(
            task_commit, merged_sha, changed_files
        )
        artifact_snapshot_cache.register_commit(
            merged_sha, current_head, changed_files
        )

        return await self._finalize_merge(
            repo=repo,
            task=task,
            workspace=workspace,
            previous_head=current_head,
            new_sha=merged_sha,
            changed_files=changed_files,
            gate_results=gate_results,
            merge_mode="tree_merge",
            timings=timings,
            started=started,
        )

    async def _finalize_merge(
        self,
        repo: GitRepository,
        task: DagTask,
        workspace: Workspace,
        previous_head: str,
        new_sha: str,
        changed_files: list[str],
        gate_results: list[GateCheckResult],
        merge_mode: str,
        *,
        timings: dict[str, int] | None = None,
        started: float | None = None,
    ) -> _MergeOutcome:
        if timings is None:
            timings = {}
        if started is None:
            started = time.monotonic()
        try:
            artifact_started = time.monotonic()
            stale_ids = await self.synchronizer.sync_after_commit(
                workspace, task, new_sha, changed_files, repo
            )
            timings["artifact_db_ms"] = _elapsed_ms(artifact_started)

            edge_started = time.monotonic()
            if edge_rebuild_service.enabled:
                await edge_rebuild_service.enqueue(
                    EdgeRebuildJob(
                        workspace_id=workspace.workspace_id,
                        repo_path=workspace.repo_path,
                        main_branch=workspace.main_branch,
                        new_sha=new_sha,
                        changed_files=tuple(changed_files),
                    ),
                    session=self.session,
                )
                timings["edge_enqueue_ms"] = _elapsed_ms(edge_started)
            else:
                await self.synchronizer.rebuild_symbol_edges(
                    workspace.workspace_id, changed_files, new_sha, repo
                )
                timings["edge_db_ms"] = _elapsed_ms(edge_started)

            kahn_started = time.monotonic()
            ready_ids = await self.scheduler.on_task_committed(
                workspace.workspace_id, task.task_id, new_sha
            )

            evidence = {
                "commit_sha": new_sha,
                "previous_head": previous_head,
                "changed_files": changed_files,
                "merge_mode": merge_mode,
                "gate_results": [
                    result.model_dump() for result in gate_results
                ],
            }
            evidence_hash = hashlib.sha256(
                json.dumps(
                    evidence, sort_keys=True, default=str, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            await self.session.execute(
                text(
                    """
                    INSERT INTO delivery_records (
                        workspace_id, task_id, submission_id, merge_id,
                        commit_sha, base_sha, merge_mode, snapshot_id,
                        gate_results, evidence_hash
                    )
                    SELECT
                        :workspace_id, :task_id,
                        (
                            SELECT submission_id FROM patch_submissions
                            WHERE task_id = :task_id
                              AND result_commit_sha = :commit_sha
                            ORDER BY completed_at DESC NULLS LAST
                            LIMIT 1
                        ),
                        (
                            SELECT merge_id FROM merge_intents
                            WHERE task_id = :task_id
                              AND merged_commit_sha = :commit_sha
                            ORDER BY completed_at DESC NULLS LAST
                            LIMIT 1
                        ),
                        :commit_sha, :base_sha, :merge_mode, :snapshot_id,
                        CAST(:gate_results AS JSONB), :evidence_hash
                    ON CONFLICT (task_id) DO NOTHING
                    """
                ),
                {
                    "workspace_id": str(workspace.workspace_id),
                    "task_id": str(task.task_id),
                    "commit_sha": new_sha,
                    "base_sha": previous_head,
                    "merge_mode": merge_mode,
                    "snapshot_id": str(task.snapshot_id) if task.snapshot_id else None,
                    "gate_results": json.dumps(
                        [result.model_dump() for result in gate_results],
                        default=str,
                    ),
                    "evidence_hash": evidence_hash,
                },
            )
            await self.session.commit()
            timings["kahn_db_ms"] = _elapsed_ms(kahn_started)

            core_started = time.monotonic()
            await scheduler_core_manager.sync_tasks(
                self.session,
                workspace.workspace_id,
                [task.task_id, *ready_ids],
            )
            timings["core_sync_ms"] = _elapsed_ms(core_started)
        except Exception:
            await self.session.rollback()
            # Best-effort rollback of the git ref if it is still ours.
            if (await repo.head_sha()) == new_sha:
                await repo.update_ref(previous_head, new_sha)
            raise

        # The merge is durable. Event delivery is best-effort and must never
        # turn a committed task back into a rejection.
        try:
            await self.event_bus.publish(
                workspace.workspace_id,
                "GATE_PASSED",
                {
                    "task_id": str(task.task_id),
                    "commit_sha": new_sha,
                    "changed_files": changed_files,
                    "merge_mode": merge_mode,
                },
            )
            if ready_ids:
                await self.event_bus.publish(
                    workspace.workspace_id,
                    "TASK_READY",
                    {"ready_task_ids": [str(value) for value in ready_ids]},
                )
            if stale_ids:
                await self.event_bus.publish(
                    workspace.workspace_id,
                    "CONTRACT_STALE",
                    {"stale_artifact_ids": [str(value) for value in stale_ids]},
                )
            await self.session.commit()
        except Exception:
            await self.session.rollback()

        timings["total_ms"] = _elapsed_ms(started)
        get_runtime_metrics().record_submit_finished(
            "COMMITTED", merge_mode, timings, gate_results
        )
        return _MergeOutcome(
            retry=False,
            response=SubmitPatchResponse(
                task_id=task.task_id,
                new_status=TaskStatus.COMMITTED,
                committed_revision_hash=new_sha,
                unlocked_downstream_task_ids=ready_ids,
                gate_results=gate_results,
                merge_mode=merge_mode,
                merge_timing=timings,
            ),
        )

    # ----------------------------------------------------------------- reject
    async def _reject(
        self,
        task: DagTask,
        gate_results: list[GateCheckResult],
        error_traceback: str,
    ) -> SubmitPatchResponse:
        await self.session.refresh(task)
        task.retry_count += 1
        task.last_error_traceback = _truncate(error_traceback)

        if task.retry_count >= task.max_retries:
            task.status = ModelTaskStatus.FAILED
            task.leased_by_runner = None
            task.lease_token_hash = None
            task.lease_expires_at = None
        else:
            task.status = ModelTaskStatus.LEASED
            task.lease_expires_at = _now() + _delta(settings.verify_lease_seconds)
        # Use the DB clock for the version the Rust scheduler compares against;
        # a host-clock timestamp can be newer/older than Postgres NOW().
        task.updated_at = func.now()  # type: ignore[assignment]
        await self.session.commit()
        await scheduler_core_manager.sync_tasks(
            self.session, task.workspace_id, [task.task_id]
        )

        status_value = (
            task.status.value if hasattr(task.status, "value") else str(task.status)
        )
        get_runtime_metrics().record_submit_finished(
            status_value, None, {}, gate_results
        )
        return SubmitPatchResponse(
            task_id=task.task_id,
            new_status=TaskStatus(status_value),
            gate_results=gate_results,
        )
