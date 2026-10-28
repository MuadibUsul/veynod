"""The owner's automation mode: manual, semi-automatic or full automatic (Loop v1).

The mode is stored per workspace, returned to humans and to agents through
orientation, and every change is audited and announced. It does not itself
merge or deploy anything: it tells the agents which decisions they may take
without asking, and whether the deploy gate may ship without a person.

It also changes what reaches the owner (spec agentgraph/spec-auto-loop-mode v2):

* semi and full: an agent's technical question is refused with
  ``decide_with_safe_default``; only money, identity, legal matters, deleting
  data and external commitments may go to the owner;
* full: those questions never block the agent (it keeps the safe default and
  the owner answers at acceptance), and a task rejected ``max_retries`` times
  passes to the next executor (HANDOFF) instead of escalating to a person.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_capability
from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError
from app.services.common import append_event, record_audit

settings = get_settings()

MODES = ("manual", "semi", "full")
DEFAULT_MODE = "manual"
OWNER_TOPICS = ("money", "identity", "legal", "data_deletion", "external_commitment")


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
    # This workspace's switch also governs the server's deploy gate (Loop R-02).
    controls = str(settings.deploy_gate_board_workspace_id or "") == str(workspace_id)
    if row is None:
        return {
            "mode": DEFAULT_MODE,
            "auto_deploy_allowed": False,
            "updated_by": None,
            "updated_at": None,
            "version": 0,
            "controls_deploy_gate": controls,
        }
    return {
        "mode": row["mode"],
        "auto_deploy_allowed": bool(row["auto_deploy_allowed"]),
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
        "version": int(row["version"]),
        "controls_deploy_gate": controls,
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
    controls = before.pop("controls_deploy_gate")
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
    if controls:
        from app.services import deploy_gate

        await deploy_gate.record_owner_switch(principal.agent_signature, before, after, note)
    return {**after, "controls_deploy_gate": controls}


def agent_rules(mode: str) -> dict[str, Any]:
    """What the mode means for an agent, returned with orientation."""
    if mode == "manual":
        return {"technical_questions": "ask the owner",
                "owner_topics": "ask the owner",
                "after_max_rejections": "escalate to a person"}
    return {
        "technical_questions": "decide with the safest default, note it on the task",
        "owner_topics": list(OWNER_TOPICS),
        "owner_questions_block": mode != "full",
        "after_max_rejections": "handoff to the next executor" if mode == "full"
        else "escalate to a person",
        "owner_report": "put owner_report {what_changed, how_to_try, not_yet_done} in plain "
        "words in your handoff brief; technical detail stays in the log",
    }


async def current_mode(session: AsyncSession, workspace_id: uuid.UUID) -> str:
    return (await get_mode(session, workspace_id))["mode"]


async def screen_escalation(
    session: AsyncSession,
    principal: Principal,
    *,
    topic: str,
    task_key: str | None,
    question: str,
) -> bool:
    """May this agent ask the owner? Returns True when the question must not block.

    People escalate freely, and manual mode keeps every checkpoint. Under semi
    and full automation a technical question is refused; the refusal is
    recorded as an event so the log shows where an agent chose a default.
    """
    if principal.is_human:
        return False
    mode = await current_mode(session, principal.workspace.id)
    if mode == "manual":
        return False
    if topic not in OWNER_TOPICS:
        await _record_declined(principal, mode, task_key, question)
        raise ApiError(
            409,
            "decide_with_safe_default",
            f"automation is {mode}: decide technical questions yourself with the safest "
            "default, note the choice on the task, and continue",
            details={
                "mode": mode,
                "topic": topic,
                "owner_topics": list(OWNER_TOPICS),
                "safe_default": "keep existing behaviour, deny by default, back up before "
                "anything irreversible, spend nothing new",
                "record_with": "write_continuation or the handoff brief",
            },
        )
    return mode == "full"


async def _record_declined(
    principal: Principal, mode: str, task_key: str | None, question: str
) -> None:
    """Committed on its own: the refusal rolls the caller's transaction back."""
    async with async_session_factory() as session:
        await append_event(
            session,
            workspace_id=principal.workspace.id,
            agent=principal.agent_signature,
            event_type="AUTOMATION_QUESTION_DECLINED",
            payload={"mode": mode, "task_key": task_key, "question": question[:500]},
        )
        await session.commit()
