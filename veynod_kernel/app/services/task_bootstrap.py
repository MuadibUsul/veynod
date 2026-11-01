"""Create a DAG task (with its artifacts) for a workspace.

Shared by the admin plane (a person onboarding a repository) and the runtime
service plane (an Agent Run that needs a Kernel node for its product task).
Keeping one implementation means the write-set/artifact rules cannot drift
between the two callers.
"""

from __future__ import annotations

from typing import Any, Sequence
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.lock_algebra import LockAlgebra
from app.engine.scheduler_core import scheduler_core_manager
from app.models import ArtifactKind, CodeArtifact, DagTask, Workspace
from app.services.artifact_store import ensure_artifacts, infer_kind
from app.services.ast_slicer import get_slicer
from app.services.git_ops import GitError, GitRepository
from app.services.task_factory import insert_task


async def create_dag_task(
    session: AsyncSession,
    *,
    workspace: Workspace,
    title: str,
    spec_prompt: str,
    task_type: str,
    read_symbols: Sequence[str] = (),
    target_symbols: Sequence[str] = (),
    test_symbols: Sequence[str] = (),
    write_set_symbols: Sequence[str] = (),
    depends_on_task_ids: Sequence[UUID] = (),
    priority: int = 0,
    max_retries: int | None = None,
) -> DagTask:
    repo = GitRepository(workspace.repo_path, workspace.main_branch)
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
        list(read_symbols),
        default_kind=ArtifactKind.CONTRACT,
        repo=repo,
        slicer=slicer,
    )
    target_artifacts: list[CodeArtifact] = []
    for symbol in target_symbols:
        target_artifacts.extend(
            await ensure_artifacts(
                session,
                workspace,
                [symbol],
                default_kind=infer_kind(symbol, task_type),
                repo=repo,
                slicer=slicer,
            )
        )
    test_artifacts = await ensure_artifacts(
        session,
        workspace,
        list(test_symbols),
        default_kind=ArtifactKind.TEST,
        repo=repo,
        slicer=slicer,
    )
    for artifact in test_artifacts:
        if artifact.kind != ArtifactKind.TEST:
            artifact.kind = ArtifactKind.TEST

    write_symbols = list(write_set_symbols) or list(target_symbols)
    if not write_symbols:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A task needs at least one target or write-set symbol.",
        )
    write_paths = LockAlgebra.promote_new_files(write_symbols, existing_files)
    write_locks, ancestor_locks = LockAlgebra.expand_write_set(write_paths)

    dependency_ids = list(dict.fromkeys(depends_on_task_ids))
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
                detail=(
                    "One or more depends_on_task_ids do not belong to this "
                    "workspace."
                ),
            )

    task_id = await insert_task(
        session,
        workspace_id=workspace.workspace_id,
        title=title,
        spec_prompt=spec_prompt,
        task_type=task_type,
        read_artifact_ids=list(
            dict.fromkeys(artifact.artifact_id for artifact in read_artifacts)
        ),
        target_artifact_ids=list(
            dict.fromkeys(artifact.artifact_id for artifact in target_artifacts)
        ),
        test_artifact_ids=list(
            dict.fromkeys(artifact.artifact_id for artifact in test_artifacts)
        ),
        write_locks=write_locks,
        ancestor_locks=ancestor_locks,
        depends_on_task_ids=dependency_ids,
        priority=priority,
        max_retries=max_retries if max_retries is not None else 3,
    )
    await session.commit()
    await scheduler_core_manager.sync_tasks(session, workspace.workspace_id, [task_id])
    task = await session.get(DagTask, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Created task disappeared.",
        )
    return task


def task_json(task: DagTask) -> dict[str, Any]:
    return {
        "task_id": str(task.task_id),
        "workspace_id": str(task.workspace_id),
        "title": task.title,
        "type": task.type.value,
        "status": task.status.value,
        "priority": task.priority,
        "write_locks": list(task.write_locks or []),
        "base_commit_sha": task.base_commit_sha,
    }


__all__ = ["create_dag_task", "task_json"]
