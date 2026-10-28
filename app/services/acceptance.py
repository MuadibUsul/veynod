"""Owner acceptance: the owner tries delivered work and answers in plain words (Loop v1).

Under full automation the owner's only job is acceptance (spec
agentgraph/spec-auto-loop-mode v2, A-05). The console lists recently
delivered tasks with the agent's plain-words owner_report; the owner answers
"works", "does not work" or "change this" with a sentence. Anything but
"works" becomes a follow-up task carrying the owner's words, so nobody asks
the owner for commits, tables or test names.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_capability
from app.errors import ApiError, NotFound
from app.models import CollabTask, TaskStatus
from app.schemas import DocRef, TaskCreateRequest
from app.services import addresses as address_service
from app.services.common import append_event, record_audit, utcnow

VERDICTS = ("works", "broken", "change")
DELIVERED = (TaskStatus.DONE, TaskStatus.HANDOFF)
WINDOW = timedelta(days=30)


def _owner_report(task: CollabTask) -> dict[str, str] | None:
    report = (task.handoff_brief or {}).get("owner_report")
    return report if isinstance(report, dict) else None


def _verdict_out(row: Any) -> dict[str, Any]:
    return {"verdict": row["verdict"], "note": row["note"],
            "follow_up_task_key": row["follow_up_task_key"],
            "decided_by": row["decided_by"], "decided_at": row["decided_at"].isoformat()}


async def _latest_verdicts(
    session: AsyncSession, workspace_id: uuid.UUID, task_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    if not task_ids:
        return {}
    rows = (await session.execute(text(
        "SELECT DISTINCT ON (task_id) task_id, verdict, note, follow_up_task_key, decided_by, "
        "decided_at FROM owner_acceptance WHERE workspace_id = :ws AND task_id = ANY(:ids) "
        "ORDER BY task_id, decided_at DESC, id DESC"),
        {"ws": workspace_id, "ids": task_ids})).mappings().all()
    return {row["task_id"]: _verdict_out(row) for row in rows}


async def list_delivered(
    session: AsyncSession, principal: Principal, *, limit: int = 50
) -> dict[str, Any]:
    """Work delivered in the last 30 days, newest first, with its latest verdict."""
    tasks = (await session.execute(
        select(CollabTask).where(
            CollabTask.workspace_id == principal.workspace.id,
            CollabTask.kind == "work",
            CollabTask.status.in_(DELIVERED),
            CollabTask.updated_at >= utcnow() - WINDOW,
        ).order_by(CollabTask.updated_at.desc()).limit(min(max(limit, 1), 200))
    )).scalars().all()
    verdicts = await _latest_verdicts(session, principal.workspace.id,
                                      [task.id for task in tasks])
    items = [{
        "task_key": task.task_key,
        "title": task.title,
        "status": task.status.value,
        "delivered_at": task.updated_at.isoformat(),
        "owner_report": _owner_report(task),
        "acceptance": verdicts.get(task.id),
    } for task in tasks]
    return {"items": items,
            "awaiting": sum(1 for item in items if item["acceptance"] is None)}


async def _follow_up(
    session: AsyncSession, principal: Principal, task: CollabTask, verdict: str, note: str
) -> CollabTask:
    from app.services.tasks import _create_task_core

    said = "does not work" if verdict == "broken" else "needs a change"
    description = (
        f"The owner tried this in the console and says it {said}:\n\n> "
        + note.replace("\n", "\n> ")
        + f"\n\nOriginal task: {task.task_key} ({task.title}). Turn the owner's words into "
        "the change, deliver it the same way, and report back with owner_report in plain "
        "words. Do not ask the owner for technical details."
    )
    request = TaskCreateRequest(
        title=f"Owner feedback on {task.task_key}: {note.splitlines()[0]}"[:256],
        description=description,
        project=task.project,
        repo=task.repo,
        namespace=task.namespace,
        assigned_role=task.assigned_role,
        priority=task.priority,
        input_context={"acceptance_of": task.task_key, "verdict": verdict},
        context_docs=[DocRef(**ref) for ref in (task.context_docs or [])],
        allow_missing_context=True,
    )
    return await _create_task_core(session, principal, request,
                                   agent=principal.agent_signature, batch=uuid.uuid4(),
                                   commit=False)


async def record(
    session: AsyncSession, principal: Principal, task_key: str, *, verdict: str,
    note: str | None,
) -> dict[str, Any]:
    require_capability(principal, "task:approve_done")
    if verdict not in VERDICTS:
        raise ApiError(422, "validation_error", "verdict is works, broken or change",
                       details={"field": "verdict", "allowed": list(VERDICTS)})
    note = (note or "").strip()
    if verdict != "works" and not note:
        raise ApiError(422, "acceptance_note_required",
                       "say in your own words what does not work or what to change",
                       details={"field": "note"})
    task = await address_service.find_task(session, principal.workspace.id, task_key)
    if task is None:
        raise NotFound("task", f"task '{task_key}' not found")
    follow_up = None
    if verdict != "works":
        follow_up = await _follow_up(session, principal, task, verdict, note)
    row = (await session.execute(text(
        "INSERT INTO owner_acceptance (workspace_id, tenant_id, task_id, task_key, verdict, "
        "note, follow_up_task_key, decided_by) VALUES (:ws, :tenant, :task, :key, :verdict, "
        ":note, :follow, :by) RETURNING id, verdict, note, follow_up_task_key, decided_by, "
        "decided_at"),
        {"ws": principal.workspace.id, "tenant": principal.tenant_id, "task": task.id,
         "key": task.task_key, "verdict": verdict, "note": note or None,
         "follow": follow_up.task_key if follow_up else None,
         "by": principal.agent_signature})).mappings().one()
    batch = uuid.uuid4()
    after = {"task_key": task.task_key, **_verdict_out(row)}
    await record_audit(
        session, workspace_id=principal.workspace.id, session_batch_id=batch,
        agent_signature=principal.agent_signature, target_table="owner_acceptance",
        target_record_id=task.id, operation="INSERT", before={}, after=after,
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type="TASK_ACCEPTANCE_RECORDED",
        payload={"task_key": task.task_key, "verdict": verdict,
                 "follow_up_task_key": after["follow_up_task_key"],
                 "targets": [item for item in (task.handed_off_by, task.created_by) if item]},
        namespace=task.namespace,
    )
    await session.commit()
    return after
