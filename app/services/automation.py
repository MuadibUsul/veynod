"""The owner's automation mode: manual, semi-automatic or full automatic (Loop v1).

The mode is stored per workspace, returned to humans and to agents through
orientation, and every change is audited and announced. It does not itself
merge or deploy anything: it tells the agents which decisions they may take
without asking, and whether the deploy gate may ship without a person.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_capability
from app.errors import ApiError
from app.services.common import append_event, record_audit

MODES = ("manual", "semi", "full")
DEFAULT_MODE = "manual"


def _jsonable(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


async def get_mode(session: AsyncSession, workspace_id: uuid.UUID) -> dict[str, Any]:
    row = (await session.execute(
        text(
            "SELECT mode, auto_deploy_allowed, updated_by, updated_at, version "
            "FROM automation_modes WHERE workspace_id = :ws"
        ),
        {"ws": workspace_id},
    )).mappings().one_or_none()
    if row is None:
        return {
            "mode": DEFAULT_MODE,
            "auto_deploy_allowed": False,
            "updated_by": None,
            "updated_at": None,
            "version": 0,
        }
    return {
        "mode": row["mode"],
        "auto_deploy_allowed": bool(row["auto_deploy_allowed"]),
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
        "version": int(row["version"]),
    }


async def set_mode(
    session: AsyncSession,
    principal: Principal,
    *,
    mode: str,
    auto_deploy_allowed: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    require_capability(principal, "automation:set")
    if mode not in MODES:
        raise ApiError(422, "validation_error", "mode must be manual, semi or full",
                       details={"field": "mode", "allowed": list(MODES)})
    before = await get_mode(session, principal.workspace.id)
    note = (reason or "").strip() or "set from the console"
    row = (await session.execute(
        text(
            "INSERT INTO automation_modes "
            "(workspace_id, tenant_id, mode, auto_deploy_allowed, updated_by, updated_at, version) "
            "VALUES (:ws, :tenant, :mode, :auto, :by, NOW(), 1) "
            "ON CONFLICT (workspace_id) DO UPDATE SET "
            "mode = EXCLUDED.mode, auto_deploy_allowed = EXCLUDED.auto_deploy_allowed, "
            "updated_by = EXCLUDED.updated_by, updated_at = NOW(), "
            "version = automation_modes.version + 1 "
            "RETURNING mode, auto_deploy_allowed, updated_by, updated_at, version"
        ),
        {"ws": principal.workspace.id, "tenant": principal.tenant_id, "mode": mode,
         "auto": auto_deploy_allowed, "by": principal.agent_signature},
    )).mappings().one()
    after = {
        "mode": row["mode"],
        "auto_deploy_allowed": bool(row["auto_deploy_allowed"]),
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
        "version": int(row["version"]),
    }
    batch = uuid.uuid4()
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=principal.agent_signature,
        target_table="automation_modes",
        target_record_id=principal.workspace.id,
        operation="UPSERT",
        before={key: _jsonable(value) for key, value in before.items()},
        after={key: _jsonable(value) for key, value in after.items()},
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type="AUTOMATION_MODE_CHANGED",
        payload={
            "mode": after["mode"],
            "auto_deploy_allowed": after["auto_deploy_allowed"],
            "previous_mode": before["mode"],
            "previous_auto_deploy_allowed": before["auto_deploy_allowed"],
            "reason": note,
            "session_batch_id": str(batch),
        },
    )
    await session.commit()
    return after
