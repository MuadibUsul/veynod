"""Artifact upsert/refresh helper shared by the admin API and the engine."""

from __future__ import annotations

import hashlib
from typing import Iterable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.lock_algebra import LockAlgebra
from app.models import ArtifactKind, ArtifactStatus, CodeArtifact, Workspace
from app.services.ast_slicer import Slicer
from app.services.git_ops import GitRepository


def infer_kind(symbol_path: str, task_type: str | None = None) -> ArtifactKind:
    file_part = LockAlgebra.normalize(symbol_path).split("::")[0]
    leaf = LockAlgebra.normalize(symbol_path).split("::")[-1]
    if "test" in file_part.lower() or leaf.startswith("test_"):
        return ArtifactKind.TEST
    if task_type == "DESIGN_CONTRACT" or task_type == "REVISE_CONTRACT":
        return ArtifactKind.CONTRACT
    if task_type == "IMPLEMENT_BODY" or task_type == "FIX_INTEGRATION":
        return ArtifactKind.STUB
    return ArtifactKind.CONTRACT


async def ensure_artifacts(
    session: AsyncSession,
    workspace: Workspace,
    symbol_paths: Iterable[str],
    *,
    default_kind: ArtifactKind = ArtifactKind.CONTRACT,
    repo: GitRepository | None = None,
    slicer: Slicer | None = None,
) -> list[CodeArtifact]:
    normalized = sorted({LockAlgebra.normalize(path) for path in symbol_paths})
    if not normalized:
        return []

    for symbol_path in normalized:
        file_path = symbol_path.split("::")[0]
        await session.execute(
            text(
                """
                INSERT INTO code_artifacts (
                    workspace_id, file_path, symbol_path, kind, status
                ) VALUES (
                    :workspace_id, :file_path, :symbol_path,
                    CAST(:kind AS artifact_kind), 'DRAFT'
                )
                ON CONFLICT (workspace_id, symbol_path) DO NOTHING
                """
            ),
            {
                "workspace_id": workspace.workspace_id,
                "file_path": file_path,
                "symbol_path": symbol_path,
                "kind": default_kind.value,
            },
        )

    rows = (
        await session.execute(
            select(CodeArtifact).where(
                CodeArtifact.workspace_id == workspace.workspace_id,
                CodeArtifact.symbol_path.in_(normalized),
            )
        )
    ).scalars().all()
    by_symbol = {artifact.symbol_path: artifact for artifact in rows}

    if repo is not None and slicer is not None:
        for symbol_path, artifact in by_symbol.items():
            if artifact.signature_hash:
                continue
            content = await repo.read_file(artifact.file_path)
            if content is None:
                continue
            info = slicer.extract_artifact_signature(content, artifact.symbol_path)
            if not info.found:
                continue
            artifact.ast_signature = info.skeleton
            artifact.signature_hash = info.signature_hash
            artifact.docstring = info.docstring or ""
            artifact.content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    await session.flush()
    return [by_symbol[symbol] for symbol in normalized if symbol in by_symbol]


def artifact_status(value: str) -> ArtifactStatus:
    return ArtifactStatus(value)


__all__ = ["ensure_artifacts", "infer_kind", "artifact_status"]
