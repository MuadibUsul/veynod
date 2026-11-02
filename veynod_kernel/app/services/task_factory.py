"""Shared task insertion used by the admin API and the cold-start scanner."""

from __future__ import annotations

import hashlib
import json
from typing import Iterable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def insert_task(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    title: str,
    spec_prompt: str,
    task_type: str,
    read_artifact_ids: Iterable[UUID] = (),
    target_artifact_ids: Iterable[UUID] = (),
    test_artifact_ids: Iterable[UUID] = (),
    write_locks: Iterable[str] = (),
    ancestor_locks: Iterable[str] = (),
    depends_on_task_ids: Iterable[UUID] = (),
    priority: int = 100,
    max_retries: int = 3,
    parent_task_id: UUID | None = None,
    work_scope: dict | None = None,
    scope_hash: str | None = None,
    runtime_request_key: str | None = None,
) -> UUID:
    read_ids = list(read_artifact_ids)
    target_ids = list(target_artifact_ids)
    test_ids = list(test_artifact_ids)
    dependencies = list(dict.fromkeys(depends_on_task_ids))
    in_degree = len(dependencies)
    task_status = "READY" if in_degree == 0 else "BLOCKED"

    task_id = (
        await session.execute(
            text(
                """
                INSERT INTO dag_tasks (
                    workspace_id, parent_task_id, title, spec_prompt, type, status,
                    priority, in_degree, read_artifact_ids, target_artifact_ids,
                    test_artifact_ids, write_locks, ancestor_locks, max_retries,
                    work_scope, scope_hash, runtime_request_key
                ) VALUES (
                    :workspace_id, :parent_task_id, :title, :spec,
                    CAST(:task_type AS task_type), CAST(:status AS task_status),
                    :priority, :in_degree, CAST(:read_ids AS UUID[]),
                    CAST(:target_ids AS UUID[]), CAST(:test_ids AS UUID[]),
                    CAST(:write_locks AS TEXT[]), CAST(:ancestor_locks AS TEXT[]),
                    :max_retries, CAST(:work_scope AS JSONB), :scope_hash,
                    :runtime_request_key
                )
                RETURNING task_id
                """
            ),
            {
                "workspace_id": workspace_id,
                "parent_task_id": parent_task_id,
                "title": title[:256],
                "spec": spec_prompt,
                "task_type": task_type,
                "status": task_status,
                "priority": priority,
                "in_degree": in_degree,
                "read_ids": [str(value) for value in read_ids],
                "target_ids": [str(value) for value in target_ids],
                "test_ids": [str(value) for value in test_ids],
                "write_locks": list(write_locks),
                "ancestor_locks": list(ancestor_locks),
                "max_retries": max_retries,
                "work_scope": json.dumps(work_scope) if work_scope is not None else None,
                "scope_hash": scope_hash,
                "runtime_request_key": runtime_request_key,
            },
        )
    ).scalar_one()

    for upstream in dependencies:
        await session.execute(
            text(
                "INSERT INTO task_dependencies (upstream_task_id, downstream_task_id) "
                "VALUES (:upstream, :downstream) ON CONFLICT DO NOTHING"
            ),
            {"upstream": upstream, "downstream": task_id},
        )

    # Bind the task to the exact artifact signatures it was planned against.
    # The manifest is deliberately small and content-addressed; it is not a
    # copy of the repository.
    artifact_ids = sorted(
        {
            str(value)
            for value in (*read_ids, *target_ids, *test_ids)
        }
    )
    artifacts: list[dict] = []
    if artifact_ids:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT artifact_id, symbol_path, file_path, kind::text AS kind,
                           signature_hash, content_hash, version
                    FROM code_artifacts
                    WHERE artifact_id = ANY(CAST(:ids AS UUID[]))
                    ORDER BY symbol_path
                    """
                ),
                {"ids": artifact_ids},
            )
        ).mappings().all()
        artifacts = [dict(row) for row in rows]
    base_sha = await session.scalar(
        text("SELECT head_commit_sha FROM workspaces WHERE workspace_id = :workspace"),
        {"workspace": workspace_id},
    )
    graph_version = await session.scalar(
        text(
            "SELECT COALESCE(version, 0) FROM symbol_graph_versions "
            "WHERE workspace_id = :workspace"
        ),
        {"workspace": workspace_id},
    )
    manifest = {
        "artifacts": [
            {key: (str(value) if key == "artifact_id" else value)
             for key, value in row.items()}
            for row in artifacts
        ],
        "write_locks": sorted(write_locks),
        "ancestor_locks": sorted(ancestor_locks),
        "base_sha": base_sha,
        "graph_version": int(graph_version or 0),
    }
    manifest_hash = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    snapshot_id = (
        await session.execute(
            text(
                """
                INSERT INTO task_snapshots
                    (workspace_id, base_sha, graph_version, manifest, manifest_hash)
                VALUES (
                    :workspace_id, :base_sha, :graph_version,
                    CAST(:manifest AS JSONB), :manifest_hash
                )
                RETURNING snapshot_id
                """
            ),
            {
                "workspace_id": workspace_id,
                "base_sha": base_sha or "",
                "graph_version": int(graph_version or 0),
                "manifest": json.dumps(manifest, default=str),
                "manifest_hash": manifest_hash,
            },
        )
    ).scalar_one()
    await session.execute(
        text("UPDATE dag_tasks SET snapshot_id = :snapshot WHERE task_id = :task"),
        {"snapshot": snapshot_id, "task": task_id},
    )
    return task_id


__all__ = ["insert_task"]
