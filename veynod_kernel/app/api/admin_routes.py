"""Bootstrap/admin plane: workspaces, runner tokens, artifacts, task seeding.

This is not part of the 6-syscall agent surface. It is protected by
``X-Admin-Key`` and is intended for the single human architect (or CI) that
creates the seed topology and the cold-start task graph.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    clear_token_cache,
    derive_worker_hub_id,
    hash_token,
    issue_token,
    new_token,
)
from app.config import settings
from app.database import get_db_session, get_engine
from app.engine import rust_bridge
from app.engine.lock_algebra import LockAlgebra
from app.engine.scheduler_core import scheduler_core_manager
from app.engine.task_commands import (
    TaskCancelConflict,
    TaskNotFound,
    cancel_task as cancel_task_command,
)
from app.mcp_server.syscalls import ArtifactKind as ApiArtifactKind
from app.mcp_server.syscalls import ArtifactStatus as ApiArtifactStatus
from app.mcp_server.syscalls import TaskType as ApiTaskType
from app.models import (
    AgentToken,
    ArtifactKind,
    ArtifactStatus,
    CodeArtifact,
    DagTask,
    TaskStatus,
    Workspace,
)
from app.services.artifact_store import ensure_artifacts, infer_kind
from app.services.artifact_sync import ArtifactSynchronizer
from app.services.ast_slicer import SignatureInfo, get_slicer, hash_signature_text
from app.services.event_bus import EventBus
from app.services.git_ops import GitError, GitRepository, RepositoryNotUsable
from app.services.runtime_metrics import get_runtime_metrics
from app.services.task_evidence import build_task_evidence
from app.services.task_factory import insert_task

_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "build",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".idea",
    ".vscode",
}


async def require_admin(
    x_admin_key: Annotated[str | None, Header(alias="X-Admin-Key")] = None,
) -> None:
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="VN_ADMIN_API_KEY is not configured; the admin API is disabled.",
        )
    if not x_admin_key or not hmac.compare_digest(
        x_admin_key.encode("utf-8"), settings.admin_api_key.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid X-Admin-Key.",
        )


class TaskCancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=8000)


require_admin_dependency = Depends(require_admin)
router = APIRouter(
    prefix="/v1/admin",
    tags=["Admin / Bootstrap"],
    dependencies=[require_admin_dependency],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    repo_path: str = Field(..., min_length=1)
    main_branch: str = Field(default="main", min_length=1, max_length=64)
    language_toolchain: str = Field(default="python", max_length=32)


class TokenCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="runner", min_length=1, max_length=128)
    workspace_id: UUID | None = None


class ArtifactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    symbol_path: str = Field(..., min_length=1)
    kind: ApiArtifactKind | None = None
    status: ApiArtifactStatus | None = None
    ast_signature: str | None = Field(default=None, max_length=200_000)
    docstring: str | None = Field(default=None, max_length=20_000)


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    title: str = Field(..., min_length=3, max_length=256)
    spec_prompt: str = Field(..., min_length=10, max_length=100_000)
    type: ApiTaskType
    read_symbols: list[str] = Field(default_factory=list)
    target_symbols: list[str] = Field(default_factory=list)
    test_symbols: list[str] = Field(default_factory=list)
    write_set_symbols: list[str] | None = None
    depends_on_task_ids: list[UUID] = Field(default_factory=list)
    priority: int = 100
    max_retries: int = Field(default=3, ge=1, le=20)


class ScanStubsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = False
    limit: int = Field(default=5000, ge=1, le=20000)
    include_tests: bool = False
    write_test_priority: int = 100
    implement_priority: int = 100
    rebuild_symbol_edges: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _workspace(session: AsyncSession, workspace_id: UUID) -> Workspace:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {workspace_id} not found.",
        )
    return workspace


def _repo(workspace: Workspace) -> GitRepository:
    return GitRepository(workspace.repo_path, workspace.main_branch)


def _task_json(task: DagTask) -> dict:
    return {
        "task_id": str(task.task_id),
        "title": task.title,
        "type": task.type.value,
        "status": task.status.value,
        "priority": task.priority,
        "in_degree": task.in_degree,
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "leased_by_runner": task.leased_by_runner,
        "lease_expires_at": task.lease_expires_at,
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
        "write_locks": list(task.write_locks),
        "read_artifact_ids": [str(value) for value in task.read_artifact_ids],
        "target_artifact_ids": [str(value) for value in task.target_artifact_ids],
        "test_artifact_ids": [str(value) for value in task.test_artifact_ids],
        "base_commit_sha": task.base_commit_sha,
        "result_commit_sha": task.result_commit_sha,
        "last_error_traceback": task.last_error_traceback,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _json_value(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _artifact_json(artifact: CodeArtifact) -> dict:    return {
        "artifact_id": str(artifact.artifact_id),
        "symbol_path": artifact.symbol_path,
        "file_path": artifact.file_path,
        "kind": artifact.kind.value,
        "status": artifact.status.value,
        "signature_hash": artifact.signature_hash,
        "content_hash": artifact.content_hash,
        "version": artifact.version,
        "docstring": artifact.docstring,
        "ast_signature": artifact.ast_signature,
        "updated_at": artifact.updated_at,
    }


def _test_symbol(file_path: str, symbol_path: str) -> tuple[str, str]:
    chain = symbol_path.split("::")[1:]
    leaf = "_".join(chain[-2:]) if len(chain) >= 2 else chain[-1]
    stem = file_path[:-3] if file_path.endswith(".py") else file_path
    stem = re.sub(r"[^A-Za-z0-9_]+", "_", stem).strip("_")[:120]
    leaf_safe = re.sub(r"[^A-Za-z0-9_]+", "_", leaf).strip("_")[:60] or "symbol"
    digest = hashlib.sha1(symbol_path.encode("utf-8")).hexdigest()[:8]
    test_file = f"tests/test_{stem}__{leaf_safe}__{digest}.py"
    return f"{test_file}::test_{leaf_safe}", test_file


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------
@router.post("/workspaces", status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    repo_path = Path(body.repo_path).expanduser().resolve()
    repo = GitRepository(repo_path, body.main_branch)
    try:
        await repo.validate_usable()
        branch_sha = await repo.resolve_ref(f"refs/heads/{body.main_branch}")
    except (RepositoryNotUsable, GitError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    if not branch_sha:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Branch '{body.main_branch}' does not exist in {repo_path}.",
        )

    # High-frequency worktrees: disable background gc and preload the index.
    await repo.configure_for_swarm()

    workspace = Workspace(
        name=body.name,
        repo_path=str(repo_path),
        main_branch=body.main_branch,
        head_commit_sha=branch_sha,
        language_toolchain=body.language_toolchain,
    )
    session.add(workspace)
    await session.commit()
    await session.refresh(workspace)
    await session.execute(
        text(
            """
            INSERT INTO repository_placements (workspace_id, cell_id)
            SELECT :workspace_id, cell_id FROM cells WHERE name = 'default'
            ON CONFLICT (workspace_id) DO NOTHING
            """
        ),
        {"workspace_id": str(workspace.workspace_id)},
    )
    await session.commit()
    return {
        "workspace_id": str(workspace.workspace_id),
        "name": workspace.name,
        "repo_path": workspace.repo_path,
        "main_branch": workspace.main_branch,
        "head_commit_sha": workspace.head_commit_sha,
    }


@router.get("/workspaces")
async def list_workspaces(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[dict]:
    workspaces = (
        await session.execute(select(Workspace).order_by(Workspace.created_at.asc()))
    ).scalars().all()
    return [
        {
            "workspace_id": str(workspace.workspace_id),
            "name": workspace.name,
            "repo_path": workspace.repo_path,
            "main_branch": workspace.main_branch,
            "head_commit_sha": workspace.head_commit_sha,
            "language_toolchain": workspace.language_toolchain,
        }
        for workspace in workspaces
    ]


# ---------------------------------------------------------------------------
# Runner tokens
# ---------------------------------------------------------------------------
@router.post("/tokens", status_code=status.HTTP_201_CREATED)
async def create_token(
    body: TokenCreate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    workspace_id = body.workspace_id
    if workspace_id is not None:
        await _workspace(session, workspace_id)
    raw_token, record = await issue_token(
        session, body.label, workspace_id=workspace_id
    )
    return {
        "token": raw_token,
        "worker_hub_id": record.worker_hub_id,
        "token_id": str(record.token_id),
        "workspace_id": str(record.workspace_id) if record.workspace_id else None,
        "note": "Store this token now; only its SHA-256 hash is persisted.",
    }


def _token_json(token: AgentToken) -> dict:
    return {
        "token_id": str(token.token_id),
        "worker_hub_id": token.worker_hub_id,
        "label": token.label,
        "workspace_id": str(token.workspace_id) if token.workspace_id else None,
        "active": token.active,
        "created_at": token.created_at,
        "last_seen_at": token.last_seen_at,
    }


async def _release_runner_leases(
    session: AsyncSession, worker_id: str
) -> list[dict]:
    """Return a revoked/rotated runner's tasks to the queue (or fail them)."""
    rows = (
        await session.execute(
            text(
                """
                UPDATE dag_tasks
                SET status = CASE
                        WHEN status = 'VERIFYING'
                        THEN 'FAILED'::task_status
                        WHEN retry_count + 1 >= max_retries
                        THEN 'FAILED'::task_status
                        ELSE 'READY'::task_status
                    END,
                    retry_count = retry_count + 1,
                    leased_by_runner = NULL,
                    lease_token_hash = NULL,
                    lease_expires_at = NULL,
                    last_error_traceback = 'Runner token revoked or rotated',
                    updated_at = NOW()
                WHERE leased_by_runner = :worker_id
                  AND status IN ('LEASED', 'VERIFYING')
                RETURNING task_id, workspace_id, status::text AS status
                """
            ),
            {"worker_id": worker_id},
        )
    ).mappings().all()
    released = [dict(row) for row in rows]
    if released:
        task_ids = [row["task_id"] for row in released]
        # A gate that is already RUNNING must not later report a success for a
        # revoked runner.  The worker watcher will also cancel it.
        await session.execute(
            text(
                """
                UPDATE patch_submissions
                SET status = 'FAILED',
                    task_status = 'FAILED',
                    error_code = 'runner_token_revoked',
                    error_detail = 'Runner token was revoked or rotated during the gate.',
                    work_lease_id = NULL,
                    work_lease_expires_at = NULL,
                    completed_at = COALESCE(completed_at, NOW()),
                    updated_at = NOW()
                WHERE task_id = ANY(CAST(:task_ids AS UUID[]))
                  AND status IN ('QUEUED', 'RUNNING')
                """
            ),
            {"task_ids": task_ids},
        )
    for row in released:
        await EventBus(session).publish(
            row["workspace_id"],
            "TASK_RELEASED" if row["status"] == "READY" else "TASK_FAILED",
            {
                "task_id": str(row["task_id"]),
                "runner_id": worker_id,
                "new_status": row["status"],
                "reason": "runner token revoked or rotated",
            },
        )
    return released


async def _sync_released(rows: list[dict]) -> None:
    if not rows:
        return
    async with AsyncSession(get_engine()) as session:
        by_workspace: dict[UUID, list[UUID]] = {}
        for row in rows:
            by_workspace.setdefault(row["workspace_id"], []).append(row["task_id"])
        for workspace_id, task_ids in by_workspace.items():
            await scheduler_core_manager.sync_tasks(session, workspace_id, task_ids)
        await session.commit()


async def _token_audit(
    session: AsyncSession,
    token: AgentToken,
    event_type: str,
    payload: dict,
) -> None:
    if token.workspace_id is None:
        return
    await EventBus(session).publish(
        token.workspace_id,
        event_type,
        {
            "token_id": str(token.token_id),
            "worker_hub_id": token.worker_hub_id,
            "label": token.label,
            **payload,
        },
    )


@router.get("/tokens")
async def list_tokens(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[dict]:
    tokens = (
        await session.execute(select(AgentToken).order_by(AgentToken.created_at.asc()))
    ).scalars().all()
    return [_token_json(token) for token in tokens]


@router.post("/tokens/{token_id}/revoke")
async def revoke_token(
    token_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    token = await session.get(AgentToken, token_id)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Token not found."
        )
    token.active = False
    released = await _release_runner_leases(session, token.worker_hub_id)
    await _token_audit(
        session,
        token,
        "TOKEN_REVOKED",
        {"released_tasks": [str(row["task_id"]) for row in released]},
    )
    await session.commit()
    clear_token_cache()
    await _sync_released(released)
    return {
        "token_id": str(token.token_id),
        "active": False,
        "released_tasks": [str(row["task_id"]) for row in released],
    }


@router.post("/tokens/{token_id}/rotate")
async def rotate_token(
    token_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    token = await session.get(AgentToken, token_id)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Token not found."
        )
    old_worker_id = token.worker_hub_id
    released = await _release_runner_leases(session, old_worker_id)
    raw_token = new_token()
    token.token_hash = hash_token(raw_token)
    token.worker_hub_id = derive_worker_hub_id(raw_token)
    token.active = True
    await _token_audit(
        session,
        token,
        "TOKEN_ROTATED",
        {
            "old_worker_hub_id": old_worker_id,
            "released_tasks": [str(row["task_id"]) for row in released],
        },
    )
    await session.commit()
    clear_token_cache()
    await _sync_released(released)
    return {
        "token": raw_token,
        "token_id": str(token.token_id),
        "worker_hub_id": token.worker_hub_id,
        "workspace_id": str(token.workspace_id) if token.workspace_id else None,
        "released_tasks": [str(row["task_id"]) for row in released],
        "note": "The previous secret is invalid immediately.",
    }


# ---------------------------------------------------------------------------
# Durable event outbox: operator visibility for retries and dead letters
# ---------------------------------------------------------------------------
@router.get("/event-outbox/stats")
async def event_outbox_stats(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    row = (
        await session.execute(
            text(
                """
                SELECT
                    COUNT(*) FILTER (WHERE published_at IS NOT NULL) AS published,
                    COUNT(*) FILTER (
                        WHERE published_at IS NULL AND dead_lettered_at IS NULL
                    ) AS pending,
                    COUNT(*) FILTER (WHERE dead_lettered_at IS NOT NULL)
                        AS dead_lettered,
                    COALESCE(MAX(attempts), 0) AS max_attempts
                FROM event_outbox
                """
            )
        )
    ).mappings().one()
    return {
        "published": int(row["published"] or 0),
        "pending": int(row["pending"] or 0),
        "dead_lettered": int(row["dead_lettered"] or 0),
        "max_attempts": int(row["max_attempts"] or 0),
    }


@router.get("/event-outbox/dead-letter")
async def event_outbox_dead_letter(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    rows = (
        await session.execute(
            text(
                """
                SELECT outbox_id, event_seq, workspace_id, event_type,
                       attempts, last_error, dead_lettered_at, created_at
                FROM event_outbox
                WHERE dead_lettered_at IS NOT NULL
                ORDER BY outbox_id DESC LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    ).mappings().all()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Runtime metrics (additive; consumed by scripts/bench_swarm.py)
# ---------------------------------------------------------------------------
@router.get("/metrics")
async def runtime_metrics() -> dict:
    """Cumulative in-process claim/submit/merge/SSE and DB-pool metrics."""
    return get_runtime_metrics().snapshot(get_engine().sync_engine.pool)


@router.post("/metrics/reset")
async def reset_runtime_metrics() -> dict:
    metrics = get_runtime_metrics()
    metrics.reset()
    return {"reset": True, "reset_at": metrics.snapshot()["reset_at"]}


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------
@router.post("/artifacts", status_code=status.HTTP_201_CREATED)
async def create_artifact(
    body: ArtifactCreate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    workspace = await _workspace(session, body.workspace_id)
    symbol_path = LockAlgebra.normalize(body.symbol_path)
    default_kind = (
        ArtifactKind(body.kind.value) if body.kind is not None else infer_kind(symbol_path)
    )
    repo = _repo(workspace)
    artifacts = await ensure_artifacts(
        session,
        workspace,
        [symbol_path],
        default_kind=default_kind,
        repo=repo,
        slicer=get_slicer(),
    )
    artifact = artifacts[0]
    if body.ast_signature is not None:
        artifact.ast_signature = body.ast_signature
        artifact.signature_hash = hash_signature_text(body.ast_signature)
        artifact.status = ArtifactStatus.DRAFT
    if body.status is not None:
        artifact.status = ArtifactStatus(body.status.value)
    if body.docstring is not None:
        artifact.docstring = body.docstring
    await session.commit()
    await session.refresh(artifact)
    return _artifact_json(artifact)


@router.get("/artifacts")
async def list_artifacts(
    workspace_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    symbol_prefix: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
) -> list[dict]:
    stmt = select(CodeArtifact).where(CodeArtifact.workspace_id == workspace_id)
    if symbol_prefix:
        prefix = LockAlgebra.normalize(symbol_prefix)
        stmt = stmt.where(CodeArtifact.symbol_path.like(f"{prefix}%"))
    rows = (
        await session.execute(
            stmt.order_by(CodeArtifact.symbol_path.asc()).limit(limit)
        )
    ).scalars().all()
    return [_artifact_json(artifact) for artifact in rows]


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
@router.post("/tasks", status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    workspace = await _workspace(session, body.workspace_id)
    repo = _repo(workspace)
    slicer = get_slicer()
    try:
        existing_files = await repo.list_files()
    except GitError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    read_artifacts = await ensure_artifacts(
        session,
        workspace,
        body.read_symbols,
        default_kind=ArtifactKind.CONTRACT,
        repo=repo,
        slicer=slicer,
    )

    target_artifacts: list[CodeArtifact] = []
    for symbol in body.target_symbols:
        target_artifacts.extend(
            await ensure_artifacts(
                session,
                workspace,
                [symbol],
                default_kind=infer_kind(symbol, body.type.value),
                repo=repo,
                slicer=slicer,
            )
        )

    test_artifacts = await ensure_artifacts(
        session,
        workspace,
        body.test_symbols,
        default_kind=ArtifactKind.TEST,
        repo=repo,
        slicer=slicer,
    )
    for artifact in test_artifacts:
        if artifact.kind != ArtifactKind.TEST:
            artifact.kind = ArtifactKind.TEST

    write_symbols = body.write_set_symbols or body.target_symbols
    if not write_symbols:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A task needs at least one target or write-set symbol.",
        )
    write_paths = LockAlgebra.promote_new_files(write_symbols, existing_files)
    write_locks, ancestor_locks = LockAlgebra.expand_write_set(write_paths)

    dependency_ids = list(dict.fromkeys(body.depends_on_task_ids))
    if dependency_ids:
        found = (
            await session.execute(
                select(DagTask.task_id).where(
                    DagTask.workspace_id == workspace.workspace_id,
                    DagTask.task_id.in_(dependency_ids),
                )
            )
        ).scalars().all()
        if len(found) != len(dependency_ids):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="One or more depends_on_task_ids do not belong to this workspace.",
            )

    read_ids = list(dict.fromkeys(artifact.artifact_id for artifact in read_artifacts))
    target_ids = list(dict.fromkeys(artifact.artifact_id for artifact in target_artifacts))
    test_ids = list(dict.fromkeys(artifact.artifact_id for artifact in test_artifacts))

    task_id = await insert_task(
        session,
        workspace_id=workspace.workspace_id,
        title=body.title,
        spec_prompt=body.spec_prompt,
        task_type=body.type.value,
        read_artifact_ids=read_ids,
        target_artifact_ids=target_ids,
        test_artifact_ids=test_ids,
        write_locks=write_locks,
        ancestor_locks=ancestor_locks,
        depends_on_task_ids=dependency_ids,
        priority=body.priority,
        max_retries=body.max_retries,
    )
    await session.commit()
    await scheduler_core_manager.sync_tasks(
        session, workspace.workspace_id, [task_id]
    )
    task = await session.get(DagTask, task_id)
    assert task is not None
    return _task_json(task)


@router.get("/tasks")
async def list_tasks(
    workspace_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    task_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, ge=1, le=2000),
) -> list[dict]:
    stmt = select(DagTask).where(DagTask.workspace_id == workspace_id)
    if task_status:
        try:
            stmt = stmt.where(DagTask.status == TaskStatus(task_status))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown task status {task_status!r}.",
            ) from exc
    tasks = (
        await session.execute(
            stmt.order_by(DagTask.priority.desc(), DagTask.created_at.asc()).limit(limit)
        )
    ).scalars().all()
    return [_task_json(task) for task in tasks]


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    task = await session.get(DagTask, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Task {task_id} not found."
        )
    return _task_json(task)


@router.get("/tasks/{task_id}/evidence")
async def get_task_evidence(
    task_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    """Return the *durable* gate/merge evidence for a task.

    AgentGraph must project these results, never infer PASS from a status.
    """
    payload = await build_task_evidence(session, task_id)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Task {task_id} not found."
        )
    return payload


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: UUID,
    body: TaskCancelRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    """Cancel a task, its dependent subtree, open submissions and intents."""
    try:
        return await cancel_task_command(
            session, task_id, reason=body.reason, actor="admin"
        )
    except TaskNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except TaskCancelConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc


# ---------------------------------------------------------------------------
# Cold start: scan ``...`` stubs into WRITE_TEST -> IMPLEMENT_BODY pairs
# ---------------------------------------------------------------------------
@router.post("/workspaces/{workspace_id}/scan-stubs")
async def scan_stubs(
    workspace_id: UUID,
    body: ScanStubsRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    workspace = await _workspace(session, workspace_id)
    repo = _repo(workspace)
    slicer = get_slicer()
    existing_files = await repo.list_files()

    discovered: list[dict] = []
    scanned_files = 0
    rust_scanner = (
        getattr(rust_bridge.core(), "scan_workspace_repo", None)
        if rust_bridge.available()
        else None
    )

    if rust_scanner is not None:
        # Whole-repo Rust scanner: ignore rules + rayon + GIL release. The
        # workspace repo is bare, so scan a detached worktree at HEAD.
        temp_root = Path(tempfile.mkdtemp(prefix="veynod-scan-"))
        worktree = temp_root / "wt"
        head = await repo.head_sha()
        try:
            await repo.add_worktree(worktree, head)
            scans = rust_bridge.scan_workspace_repo(
                str(worktree), body.include_tests
            )
        finally:
            await repo.remove_worktree(worktree)
            shutil.rmtree(temp_root, ignore_errors=True)

        scanned_files = len(scans)
        for file_path, stub_list, _symbols, _references in scans:
            if any(part in _EXCLUDED_DIRS for part in file_path.split("/")):
                continue
            for (
                symbol_path,
                skeleton,
                signature_hash,
                docstring,
                found,
            ) in stub_list:
                discovered.append(
                    {
                        "file": file_path,
                        "symbol_path": symbol_path,
                        "signature": SignatureInfo(
                            skeleton=skeleton,
                            signature_hash=signature_hash,
                            docstring=docstring,
                            found=bool(found),
                        ),
                    }
                )
                if len(discovered) >= body.limit:
                    break
            if len(discovered) >= body.limit:
                break
    else:
        for path in sorted(existing_files):
            if not path.endswith(".py") or len(discovered) >= body.limit:
                continue
            if any(part in _EXCLUDED_DIRS for part in path.split("/")):
                continue
            if not body.include_tests and (
                path.startswith("tests/")
                or "/tests/" in path
                or path.split("/")[-1].startswith("test_")
            ):
                continue
            content = await repo.read_file(path)
            if content is None:
                continue
            scanned_files += 1
            for stub in slicer.discover_stubs(content, path):
                discovered.append({"file": path, "content": content, **stub})
                if len(discovered) >= body.limit:
                    break

    stub_symbols = [stub["symbol_path"] for stub in discovered]
    test_symbols = [
        _test_symbol(stub["file"], stub["symbol_path"])[0] for stub in discovered
    ]
    titles: list[str] = []
    for symbol in stub_symbols:
        titles.append(f"Write test: {symbol}")
        titles.append(f"Implement: {symbol}")
    existing_titles: dict[str, UUID] = {}
    for offset in range(0, len(titles), 5000):
        chunk = titles[offset : offset + 5000]
        rows = await session.execute(
            select(DagTask.title, DagTask.task_id).where(
                DagTask.workspace_id == workspace_id,
                DagTask.title.in_(chunk),
            )
        )
        existing_titles.update({row[0]: row[1] for row in rows.fetchall()})

    if body.dry_run:
        would_create = sum(
            1
            for symbol in stub_symbols
            if f"Implement: {symbol}" not in existing_titles
        )
        return {
            "dry_run": True,
            "scanned_files": scanned_files,
            "stubs_found": len(discovered),
            "would_create_implementations": would_create,
            "sample": stub_symbols[:20],
        }

    stub_artifacts = await ensure_artifacts(
        session,
        workspace,
        stub_symbols,
        default_kind=ArtifactKind.STUB,
    )
    stub_by_symbol = {artifact.symbol_path: artifact for artifact in stub_artifacts}
    test_artifacts = await ensure_artifacts(
        session,
        workspace,
        test_symbols,
        default_kind=ArtifactKind.TEST,
    )
    test_by_symbol = {artifact.symbol_path: artifact for artifact in test_artifacts}

    created_write_tests = 0
    created_implementations = 0
    created_task_ids: list[UUID] = []
    skipped = 0

    for stub in discovered:
        symbol = stub["symbol_path"]
        impl_title = f"Implement: {symbol}"
        if impl_title in existing_titles:
            skipped += 1
            continue

        stub_artifact = stub_by_symbol.get(symbol)
        test_symbol, _ = _test_symbol(stub["file"], symbol)
        test_artifact = test_by_symbol.get(test_symbol)
        if stub_artifact is None or test_artifact is None:
            skipped += 1
            continue

        info = stub["signature"]
        if info.found:
            if not stub_artifact.signature_hash:
                stub_artifact.ast_signature = info.skeleton
                stub_artifact.signature_hash = info.signature_hash
                stub_artifact.docstring = info.docstring or ""
            stub_artifact.status = ArtifactStatus.FROZEN
        test_artifact.kind = ArtifactKind.TEST

        write_test_title = f"Write test: {symbol}"
        write_test_id = existing_titles.get(write_test_title)
        if write_test_id is None:
            test_write_paths = LockAlgebra.promote_new_files(
                [test_symbol], existing_files
            )
            test_locks, test_ancestors = LockAlgebra.expand_write_set(test_write_paths)
            write_test_id = await insert_task(
                session,
                workspace_id=workspace_id,
                title=write_test_title,
                spec_prompt=(
                    f"Write a failing, deterministic unit test for `{symbol}` in "
                    f"`{test_symbol}`. Do not modify the implementation. The test must "
                    "be collected by pytest and must fail against the current stub."
                ),
                task_type="WRITE_TEST",
                read_artifact_ids=[stub_artifact.artifact_id],
                target_artifact_ids=[test_artifact.artifact_id],
                write_locks=test_locks,
                ancestor_locks=test_ancestors,
                priority=body.write_test_priority,
            )
            existing_titles[write_test_title] = write_test_id
            created_task_ids.append(write_test_id)
            created_write_tests += 1

        impl_write_paths = LockAlgebra.promote_new_files([symbol], existing_files)
        impl_locks, impl_ancestors = LockAlgebra.expand_write_set(impl_write_paths)
        impl_task_id = await insert_task(
            session,
            workspace_id=workspace_id,
            title=impl_title,
            spec_prompt=(
                f"Implement `{symbol}` so the frozen test set passes. Keep the "
                "public signature and docstring unchanged. If the contract is "
                "defective, call escalate_contract_defect instead of editing it."
            ),
            task_type="IMPLEMENT_BODY",
            target_artifact_ids=[stub_artifact.artifact_id],
            test_artifact_ids=[test_artifact.artifact_id],
            write_locks=impl_locks,
            ancestor_locks=impl_ancestors,
            depends_on_task_ids=[write_test_id],
            priority=body.implement_priority,
        )
        existing_titles[impl_title] = UUID(int=0)
        created_task_ids.append(impl_task_id)
        created_implementations += 1

    if body.rebuild_symbol_edges and discovered:
        head = await repo.head_sha()
        synchronizer = ArtifactSynchronizer(session, slicer)
        await synchronizer.rebuild_symbol_edges(
            workspace_id,
            sorted(existing_files),
            head,
            repo,
        )

    await session.commit()
    await scheduler_core_manager.sync_tasks(
        session, workspace_id, created_task_ids
    )
    return {
        "dry_run": False,
        "scanned_files": scanned_files,
        "stubs_found": len(discovered),
        "created_write_tests": created_write_tests,
        "created_implementations": created_implementations,
        "skipped": skipped,
        "sample": stub_symbols[:20],
    }


# ---------------------------------------------------------------------------
# Symbol graph maintenance
# ---------------------------------------------------------------------------
@router.post("/workspaces/{workspace_id}/edges/rebuild")
async def rebuild_edges(
    workspace_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    workspace = await _workspace(session, workspace_id)
    repo = _repo(workspace)
    head = await repo.head_sha()
    files = sorted(
        path
        for path in await repo.list_files()
        if path.endswith(".py")
        and not any(part in _EXCLUDED_DIRS for part in path.split("/"))
    )
    synchronizer = ArtifactSynchronizer(session, get_slicer())
    await synchronizer.rebuild_symbol_edges(workspace_id, files, head, repo)
    await session.commit()
    return {"workspace_id": str(workspace_id), "files_scanned": len(files)}
