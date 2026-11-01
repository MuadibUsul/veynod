"""Cell and repository placement registry (Phase 4).

One Kernel workspace equals one repository.  A cell groups workspaces so a
worker/gate/merge process can be bound to a subset of repositories.  Moving a
repository increments ``placement_epoch``; a cell-bound process refuses to
enter the Git critical section once the placement no longer points at its cell.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


async def list_cells(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT c.cell_id, c.name, c.endpoint, c.status, c.capacity,
                       c.created_at, c.updated_at,
                       COUNT(rp.workspace_id) FILTER (
                           WHERE rp.status = 'ACTIVE'
                       ) AS repositories
                FROM cells c
                LEFT JOIN repository_placements rp ON rp.cell_id = c.cell_id
                GROUP BY c.cell_id
                ORDER BY c.name
                """
            )
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def create_cell(
    session: AsyncSession,
    *,
    name: str,
    endpoint: str | None = None,
    capacity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = (
        await session.execute(
            text(
                """
                INSERT INTO cells (name, endpoint, capacity)
                VALUES (:name, :endpoint, CAST(:capacity AS JSONB))
                RETURNING cell_id, name, endpoint, status, capacity, created_at
                """
            ),
            {
                "name": name,
                "endpoint": endpoint,
                "capacity": _json(capacity or {}),
            },
        )
    ).mappings().one()
    await session.commit()
    return dict(row)


async def list_repositories(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT rp.workspace_id, rp.repository_id, rp.cell_id,
                       c.name AS cell_name, rp.placement_epoch, rp.status,
                       w.name AS workspace_name, w.repo_path, w.main_branch,
                       w.head_commit_sha
                FROM repository_placements rp
                JOIN workspaces w ON w.workspace_id = rp.workspace_id
                JOIN cells c ON c.cell_id = rp.cell_id
                ORDER BY c.name, w.name
                """
            )
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def move_repository(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    cell_id: UUID,
    expected_epoch: int | None = None,
) -> dict[str, Any] | None:
    current = (
        await session.execute(
            text(
                """
                SELECT rp.placement_epoch, rp.cell_id
                FROM repository_placements rp
                WHERE rp.workspace_id = :workspace_id
                FOR UPDATE
                """
            ),
            {"workspace_id": str(workspace_id)},
        )
    ).mappings().first()
    if current is None:
        return None
    if expected_epoch is not None and int(current["placement_epoch"]) != expected_epoch:
        raise ValueError(
            f"placement epoch changed: expected {expected_epoch}, "
            f"current {current['placement_epoch']}"
        )
    exists = await session.scalar(
        text("SELECT 1 FROM cells WHERE cell_id = :cell_id AND status = 'ACTIVE'"),
        {"cell_id": str(cell_id)},
    )
    if not exists:
        raise ValueError("target cell does not exist or is not active")
    row = (
        await session.execute(
            text(
                """
                UPDATE repository_placements
                SET cell_id = :cell_id,
                    placement_epoch = placement_epoch + 1,
                    updated_at = NOW()
                WHERE workspace_id = :workspace_id
                RETURNING workspace_id, repository_id, cell_id,
                          placement_epoch, status
                """
            ),
            {"workspace_id": str(workspace_id), "cell_id": str(cell_id)},
        )
    ).mappings().one()
    await session.commit()
    return dict(row)


async def cell_workspace_ids(session: AsyncSession, cell_id: UUID) -> list[UUID]:
    rows = (
        await session.execute(
            text(
                """
                SELECT workspace_id FROM repository_placements
                WHERE cell_id = :cell_id AND status = 'ACTIVE'
                """
            ),
            {"cell_id": str(cell_id)},
        )
    ).all()
    return [row[0] for row in rows]


async def assert_workspace_in_current_cell(
    session: AsyncSession, workspace_id: UUID
) -> None:
    """Raise when this process is cell-bound and the repository moved away."""
    if settings.cell_id is None:
        return
    current = await session.scalar(
        text(
            """
            SELECT cell_id FROM repository_placements
            WHERE workspace_id = :workspace_id AND status = 'ACTIVE'
            """
        ),
        {"workspace_id": str(workspace_id)},
    )
    if current is None or str(current) != str(settings.cell_id):
        from app.services.verification_gate import MergeFenceLost

        raise MergeFenceLost(
            f"repository {workspace_id} is no longer placed in cell "
            f"{settings.cell_id}"
        )


def cell_filter_sql(alias: str = "p") -> str:
    """SQL fragment for cell-bound workers; empty for unbound processes."""
    if settings.cell_id is None:
        return ""
    return (
        f" AND {alias}.workspace_id IN ("
        "SELECT workspace_id FROM repository_placements "
        f"WHERE cell_id = '{settings.cell_id}' AND status = 'ACTIVE')"
    )


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str)


__all__ = [
    "assert_workspace_in_current_cell",
    "cell_filter_sql",
    "cell_workspace_ids",
    "create_cell",
    "list_cells",
    "list_repositories",
    "move_repository",
]
