"""Post-commit artifact refresh and heuristic symbol call-graph rebuild."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ArtifactKind, ArtifactStatus, CodeArtifact, DagTask, TaskType, Workspace
from app.services.artifact_snapshot import artifact_snapshot_cache
from app.services.ast_slicer import Slicer
from app.services.git_ops import GitRepository

_FREEZING_TYPES = {TaskType.DESIGN_CONTRACT, TaskType.REVISE_CONTRACT, TaskType.WRITE_TEST}
_COMPLETING_TYPES = {TaskType.IMPLEMENT_BODY, TaskType.FIX_INTEGRATION}


class ArtifactSynchronizer:
    def __init__(self, session: AsyncSession, slicer: Slicer) -> None:
        self.session = session
        self.slicer = slicer

    async def sync_after_commit(
        self,
        workspace: Workspace,
        task: DagTask,
        new_sha: str,
        changed_files: list[str],
        repo: GitRepository,
    ) -> list[UUID]:
        """Refresh contract metadata and return artifacts that went STALE."""
        if not changed_files:
            return []

        rows = await self.session.execute(
            select(CodeArtifact).where(
                CodeArtifact.workspace_id == workspace.workspace_id,
                CodeArtifact.file_path.in_(changed_files),
            )
        )
        artifacts = list(rows.scalars().all())
        target_ids = set(task.target_artifact_ids)
        stale_ids: list[UUID] = []

        for artifact in artifacts:
            snapshot = await artifact_snapshot_cache.snapshot_for(
                repo, new_sha, artifact.file_path, self.slicer
            )
            if snapshot is None:
                if artifact.artifact_id in target_ids:
                    artifact.status = ArtifactStatus.STALE
                    stale_ids.append(artifact.artifact_id)
                continue

            info = snapshot.artifact_info(artifact.symbol_path)
            if info is None:
                if artifact.artifact_id in target_ids:
                    artifact.status = ArtifactStatus.STALE
                    stale_ids.append(artifact.artifact_id)
                continue

            previous_signature_hash = artifact.signature_hash
            is_target = artifact.artifact_id in target_ids

            if is_target:
                if task.type in _FREEZING_TYPES:
                    artifact.status = ArtifactStatus.FROZEN
                    if task.type == TaskType.WRITE_TEST:
                        artifact.kind = ArtifactKind.TEST
                elif task.type in _COMPLETING_TYPES:
                    artifact.status = ArtifactStatus.COMMITTED
                    if artifact.kind != ArtifactKind.TEST:
                        artifact.kind = ArtifactKind.IMPLEMENTATION
                artifact.ast_signature = info.skeleton
                artifact.signature_hash = info.signature_hash
                artifact.docstring = info.docstring or ""
            else:
                if (
                    artifact.status in {ArtifactStatus.FROZEN, ArtifactStatus.COMMITTED}
                    and previous_signature_hash
                    and info.signature_hash != previous_signature_hash
                ):
                    artifact.status = ArtifactStatus.STALE
                    stale_ids.append(artifact.artifact_id)

            artifact.content_hash = snapshot.content_hash
            artifact.version += 1
            artifact.updated_at = datetime.now(timezone.utc)

        await self.session.flush()
        return stale_ids

    async def rebuild_symbol_edges(
        self,
        workspace_id: UUID,
        changed_files: list[str],
        new_sha: str,
        repo: GitRepository,
    ) -> None:
        """Recompute edges for changed callers (and callers of changed callees)."""
        if not changed_files:
            return

        artifacts = list(
            (
                await self.session.execute(
                    select(CodeArtifact).where(CodeArtifact.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
        if not artifacts:
            return

        by_id = {artifact.artifact_id: artifact for artifact in artifacts}
        changed_ids = {
            artifact.artifact_id
            for artifact in artifacts
            if artifact.file_path in changed_files
        }
        if not changed_ids:
            return

        old_caller_rows = await self.session.execute(
            text(
                """
                SELECT DISTINCT caller_artifact_id
                FROM symbol_edges
                WHERE workspace_id = :workspace_id
                  AND callee_artifact_id = ANY(CAST(:ids AS UUID[]))
                """
            ),
            {
                "workspace_id": workspace_id,
                "ids": [str(value) for value in changed_ids],
            },
        )
        affected_callers = set(changed_ids) | {
            row[0] for row in old_caller_rows.fetchall()
        }

        await self.session.execute(
            text(
                """
                DELETE FROM symbol_edges
                WHERE workspace_id = :workspace_id
                  AND (
                        caller_artifact_id = ANY(CAST(:ids AS UUID[]))
                     OR callee_artifact_id = ANY(CAST(:ids AS UUID[]))
                  )
                """
            ),
            {
                "workspace_id": workspace_id,
                "ids": [str(value) for value in changed_ids],
            },
        )

        leaf_index: dict[str, list[CodeArtifact]] = {}
        for artifact in artifacts:
            leaf = artifact.symbol_path.split("::")[-1]
            leaf_index.setdefault(leaf, []).append(artifact)

        edges: list[dict[str, object]] = []
        for caller_id in affected_callers:
            caller = by_id.get(caller_id)
            if caller is None:
                continue
            snapshot = await artifact_snapshot_cache.snapshot_for(
                repo, new_sha, caller.file_path, self.slicer
            )
            if snapshot is not None:
                referenced = set(
                    snapshot.referenced_names_for(caller.symbol_path) or ()
                )
            else:
                content = await repo.read_file(caller.file_path, new_sha)
                if content is None:
                    continue
                referenced = self.slicer.extract_referenced_names(
                    content, caller.symbol_path
                )
            if not referenced:
                continue
            seen_callees: set[UUID] = set()
            for name in referenced:
                candidates = leaf_index.get(name, [])
                if len(candidates) > 1:
                    # Leaf names are ambiguous across modules (e.g. every file
                    # has ``f0``). Without import resolution, only same-file
                    # candidates are safe; cross-file edges are left to the
                    # explicit read/test artifact sets.
                    candidates = [
                        candidate
                        for candidate in candidates
                        if candidate.file_path == caller.file_path
                    ]
                for callee in candidates:
                    if callee.artifact_id == caller_id or callee.kind == ArtifactKind.TEST:
                        continue
                    if callee.artifact_id in seen_callees:
                        continue
                    seen_callees.add(callee.artifact_id)
                    edges.append(
                        {
                            "workspace_id": workspace_id,
                            "caller_id": caller_id,
                            "callee_id": callee.artifact_id,
                            "fields": [name],
                        }
                    )

        if edges:
            await self.session.execute(
                text(
                    """
                    INSERT INTO symbol_edges (
                        workspace_id, caller_artifact_id, callee_artifact_id, referenced_fields
                    ) VALUES (
                        :workspace_id, :caller_id, :callee_id, CAST(:fields AS TEXT[])
                    )
                    ON CONFLICT DO NOTHING
                    """
                ),
                edges,
            )
        await self.session.flush()


__all__ = ["ArtifactSynchronizer"]
