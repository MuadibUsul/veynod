"""Task board: claims, TTL leases, L1/L2 handoffs, review and circuit breaker."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from functools import wraps
from typing import Any

from sqlalchemy import false, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    AuthKind,
    Principal,
    RoleScope,
    can,
    capability_role,
    effective_agent,
    ensure_namespace,
    ensure_task_role,
    has_capability,
    require_permission,
)
from app.config import get_settings
from app.database import async_session_factory
from app.engine.blackboard import lease_is_active, validate_handoff_brief
from app.errors import (
    ApiError,
    EscalatedToHuman,
    Forbidden,
    InvalidStateTransition,
    LeaseConflict,
    NotFound,
    TaskNotReviewable,
    VersionConflict,
)
from app.models import (
    CollabTask,
    ContextSlot,
    LinkType,
    TaskStatus,
    WorkspaceLink,
)
from app.schemas import (
    REVIEW_APPROVE_STATUSES,
    REVIEW_NEXT_STATUSES,
    REVIEW_REJECT_STATUSES,
    DocumentSummary,
    SlotOut,
    TaskActionResponse,
    TaskClaimRequest,
    TaskContextResponse,
    TaskCreateRequest,
    TaskEscalateRequest,
    TaskHandoffRequest,
    TaskLeaseRenewRequest,
    TaskListResponse,
    TaskOut,
    TaskReleaseRequest,
    TaskRenewRequest,
    TaskReviewRequest,
    TaskUpdateRequest,
)
from app.services import access_log, agent_onboarding, automation, collab, quality
from app.services import addresses as address_service
from app.services import grants as grant_service
from app.services import leases as lease_service
from app.services.common import (
    append_event,
    db_now,
    document_summary,
    record_audit,
    slot_out,
    task_out,
    task_snapshot,
    task_summary,
    utcnow,
)
from app.services.documents import (
    bind_task_artifact,
    create_artifact,
    verify_artifact_for_review,
)
from app.services.links import add_link
from app.services.policies import enforce_policy
from app.services.taint import refresh_task_taint
from app.tenant import activate_tenant
from app.versioning import LATEST_VERSION, at_least
from app.warnings_context import add_warning

logger = logging.getLogger(__name__)
settings = get_settings()

_CLAIMABLE = {TaskStatus.READY, TaskStatus.HANDOFF}
_ACTIVE_LEASE_STATUSES = {TaskStatus.IN_PROGRESS, TaskStatus.REVIEWING}


async def _record_task_failure(
    principal: Principal,
    task_key: str,
    operation: str,
    exc: ApiError,
) -> None:
    """Write a rejected claim/transition to the timeline in its own transaction.

    The caller's transaction is already failing, so this uses a separate
    session and never replaces the original error if logging itself fails.
    """
    try:
        async with async_session_factory() as log_session:
            await activate_tenant(
                log_session, principal.workspace.id, principal.tenant_scope
            )
            task = await log_session.scalar(
                select(CollabTask).where(
                    CollabTask.workspace_id == principal.workspace.id,
                    CollabTask.task_key == task_key,
                )
            )
            await append_event(
                log_session,
                workspace_id=principal.workspace.id,
                agent=principal.agent_signature,
                event_type=f"TASK_{operation}_FAILED",
                payload={
                    "task_key": task_key,
                    "operation": operation.lower(),
                    "code": exc.code,
                    "http_status": exc.status_code,
                    "message": exc.message,
                    "details": exc.details,
                },
                namespace=task.namespace if task is not None else "default",
            )
            await log_session.commit()
    except Exception:  # noqa: BLE001 - failure telemetry must not mask the API error
        logger.exception("failed to record TASK_%s_FAILED for %s", operation, task_key)


def _task_failure_event(operation: str):
    def decorator(fn):
        @wraps(fn)
        async def wrapper(
            session: AsyncSession,
            principal: Principal,
            task_key: str,
            *args,
            **kwargs,
        ):
            try:
                return await fn(session, principal, task_key, *args, **kwargs)
            except ApiError as exc:
                await _record_task_failure(principal, task_key, operation, exc)
                raise

        return wrapper

    return decorator


async def _get_task(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    task_key: str,
    *,
    for_update: bool = False,
) -> CollabTask:
    # Accepts a canonical id (task_…), the task_key or a redirected key.
    task = await address_service.find_task(
        session, workspace_id, task_key, for_update=for_update
    )
    if task is None:
        raise NotFound("collab_task", f"task '{task_key}' not found")
    return task


def _check_version(task: CollabTask, expected: int | None) -> None:
    if expected is not None and task.version != expected:
        raise VersionConflict("collab_task", task_out(task).model_dump(mode="json"))


def _check_lease(task: CollabTask, agent: str, now: Any | None = None) -> None:
    if (
        task.lease_owner
        and task.lease_owner != agent
        and lease_is_active(task.lease_owner, task.lease_expires_at, now)
    ):
        raise LeaseConflict("collab_task", task_out(task).model_dump(mode="json"))


def _require_active_lease(task: CollabTask, agent: str, now: Any) -> None:
    if task.lease_owner != agent or not lease_is_active(
        task.lease_owner, task.lease_expires_at, now
    ):
        raise LeaseConflict("collab_task", task_out(task).model_dump(mode="json"))


def _check_epoch(task: CollabTask, expected: int | None) -> None:
    if expected is not None and expected != task.lease_epoch:
        raise LeaseConflict("collab_task", task_out(task).model_dump(mode="json"))


def _check_assignee(task: CollabTask, principal: Principal, agent: str) -> None:
    if not task.assignee:
        return
    allowed = {f"agent:{agent}", f"human:{agent}"}
    if principal.user_id is not None:
        allowed.add(f"human:{principal.user_id}")
    if task.assignee not in allowed:
        raise ApiError(
            403,
            "not_assignee",
            "this task is assigned to another principal",
            details={"task_key": task.task_key, "assignee": task.assignee},
        )


def _require_claim_owner(task: CollabTask, principal: Principal, agent: str) -> None:
    if task.claimed_by != agent:
        raise ApiError(
            409,
            "claim_required",
            "only the agent that claimed this task may hand it off",
            details={"task_key": task.task_key, "claimed_by": task.claimed_by},
        )


def validate_review_next_status(decision: str, next_status: str | None) -> None:
    """Single validator for REST and MCP review targets (PRD AG-CO-01 #1)."""
    if next_status is None:
        return
    allowed = REVIEW_APPROVE_STATUSES if decision == "approve" else REVIEW_REJECT_STATUSES
    if next_status not in allowed:
        raise ApiError(
            422,
            "validation_error",
            f"next_status '{next_status}' is not valid for decision '{decision}'",
            details={
                "field": "next_status",
                "allowed": list(allowed),
                "all": list(REVIEW_NEXT_STATUSES),
            },
        )


def _resolve_review_history(history: list[Any] | None) -> list[Any]:
    resolved = []
    now = utcnow().isoformat()
    for entry in history or []:
        if isinstance(entry, dict) and entry.get("resolved_at") is None:
            entry = {**entry, "resolved_at": now}
        resolved.append(entry)
    return resolved


def _clear_lease(task: CollabTask) -> None:
    lease_service.clear_task_lease(task)


async def _upstream_briefs(
    session: AsyncSession, task: CollabTask, principal: Principal
) -> list[dict[str, Any]]:
    target_ids = (
        await session.execute(
            select(WorkspaceLink.target_id).where(
                WorkspaceLink.workspace_id == task.workspace_id,
                WorkspaceLink.source_id == task.id,
                WorkspaceLink.relation_type == LinkType.DEPENDS_ON,
            )
        )
    ).scalars().all()
    if not target_ids:
        return []
    rows = (
        await session.execute(
            select(CollabTask).where(
                CollabTask.workspace_id == task.workspace_id,
                CollabTask.id.in_(target_ids),
            )
        )
    ).scalars().all()
    if not principal.is_commander and "*" not in principal.allowed_namespaces:
        rows = [row for row in rows if row.namespace in principal.allowed_namespaces]
    return [
        {
            "task_key": row.task_key,
            "title": row.title,
            "status": row.status.value,
            "handoff_brief": row.handoff_brief,
            "artifact_uri": row.artifact_uri,
        }
        for row in rows
    ]


async def _context_slots(
    session: AsyncSession, principal: Principal, task: CollabTask
) -> list[SlotOut]:
    if not can(principal, "state:read"):
        return []
    raw = task.input_context or {}
    keys: list[str] = []
    single = raw.get("slot_key")
    if isinstance(single, str):
        keys.append(single)
    many = raw.get("slot_keys")
    if isinstance(many, list):
        keys.extend(str(item) for item in many if isinstance(item, str))
    if not keys:
        return []
    query = select(ContextSlot).where(
        ContextSlot.workspace_id == task.workspace_id,
        ContextSlot.slot_key.in_(keys),
    )
    if not principal.is_commander and "*" not in principal.allowed_namespaces:
        query = query.where(
            ContextSlot.namespace.in_(principal.allowed_namespaces)
        )
    slots = (await session.execute(query)).scalars().all()
    return [slot_out(slot) for slot in slots]


async def _context_documents(
    session: AsyncSession, principal: Principal, task: CollabTask
) -> list[DocumentSummary]:
    if not can(principal, "doc:read"):
        return []
    raw = task.input_context or {}
    keys: list[str] = []
    single = raw.get("doc_key")
    if isinstance(single, str):
        keys.append(single)
    many = raw.get("doc_keys")
    if isinstance(many, list):
        keys.extend(str(item) for item in many if isinstance(item, str))
    if not keys:
        return []
    # Resolve each pointer by id, alias or redirect so renames never break
    # a task's context (PRD AG-ST-17 acceptance 1).
    docs = []
    for key in dict.fromkeys(keys):
        try:
            document = await address_service.find_document(session, task.workspace_id, key)
        except ApiError:
            document = None
        if document is not None:
            docs.append(document)
    allowed = (
        None
        if principal.is_commander or "*" in principal.allowed_namespaces
        else set(principal.allowed_namespaces)
    )
    if allowed is not None:
        docs = [doc for doc in docs if doc.namespace in allowed]
    return [document_summary(doc) for doc in docs]


async def _create_task_core(
    session: AsyncSession,
    principal: Principal,
    request: TaskCreateRequest,
    *,
    agent: str,
    batch: uuid.UUID,
    commit: bool,
    parent: CollabTask | None = None,
    by_graph: bool = False,
) -> CollabTask:
    # A diagram chunk released by its upstream merge is the graph's own work
    # (M-14): the merger, often a reviewer, does not need task:create for it.
    if not by_graph:
        require_permission(principal, "task:create")
    namespace, source = await collab.infer_namespace(session, principal, request, parent)
    ensure_namespace(principal, namespace, write=True)
    if source == "default":
        add_warning("task_namespace_defaulted",
                    "no namespace given or inferable; the task went to 'default'. "
                    "Pass namespace so the people who should see it can.",
                    namespace=namespace)
    elif source != "explicit":
        add_warning("task_namespace_inferred",
                    f"namespace '{namespace}' was taken from the {source.replace('_', ' ')}",
                    namespace=namespace, source=source)
    await collab.ensure_assignee_reaches(session, principal,
                                         getattr(request, "assignee", None), namespace)
    await address_service.ensure_namespace_registered(session, principal, namespace)
    task_key = request.task_key or f"TASK-{uuid.uuid4().hex[:8].upper()}"
    await enforce_policy(
        session,
        principal,
        resource_type="task",
        key=task_key,
        action="create",
        payload=request.input_context,
    )
    existing = await session.scalar(
        select(CollabTask.id).where(
            CollabTask.workspace_id == principal.workspace.id,
            CollabTask.task_key == task_key,
        )
    )
    if existing is not None:
        raise ApiError(409, "task_exists", f"task '{task_key}' already exists")
    await collab.validate_new_task(session, principal, request)

    dependencies: list[CollabTask] = []
    for dep_key in request.depends_on:
        if dep_key == task_key:
            raise ApiError(422, "invalid_dependency", "a task cannot depend on itself")
        dependency = await _get_task(session, principal.workspace.id, dep_key)
        ensure_namespace(principal, dependency.namespace)
        dependencies.append(dependency)

    if dependencies:
        all_done = all(dep.status is TaskStatus.DONE for dep in dependencies)
        status = TaskStatus.READY if all_done else TaskStatus.TODO
    else:
        status = TaskStatus(request.status)

    assigned_role = request.assigned_role
    if assigned_role is None and not principal.is_commander:
        assigned_role = principal.role.value

    task = CollabTask(
        workspace_id=principal.workspace.id,
        task_key=task_key,
        title=request.title,
        project=request.project,
        repo=request.repo,
        description=request.description,
        status=status,
        priority=request.priority,
        assigned_role=assigned_role,
        namespace=namespace,
        input_context=request.input_context,
        max_retries=request.max_retries,
        assignee=getattr(request, "assignee", None),
        version=1,
        created_by=agent,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    collab.apply_new_fields(task, request)
    task.acceptance = [item.model_dump() for item in getattr(request, "acceptance", None) or []]
    if parent is not None:
        task.parent_task_id = parent.id
        task.parent_task_key = parent.task_key
        task.root_task_id = parent.root_task_id or parent.id
        task.root_task_key = parent.root_task_key or parent.task_key
        task.tree_depth = (parent.tree_depth or 0) + 1
    mentions = collab.parse_mentions(request.mentions, request.description)
    task.mentions = mentions
    session.add(task)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise ApiError(409, "task_exists", f"task '{task_key}' already exists") from None

    for dependency in dependencies:
        await add_link(
            session,
            workspace_id=principal.workspace.id,
            source_id=task.id,
            target_id=dependency.id,
            relation_type=LinkType.DEPENDS_ON,
        )
    await refresh_task_taint(session, task)
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="INSERT",
        before={},
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="TASK_CREATED",
        payload={
            "task_key": task_key,
            "title": task.title,
            "status": status.value,
            "assigned_role": assigned_role,
            "priority": task.priority,
            "parent_task_key": task.parent_task_key,
        },
        namespace=namespace,
    )
    await collab.deliver_mentions(
        session, principal, mentions,
        refs={"task_key": task_key, "id": task.canonical_id, "title": task.title},
        namespace=namespace,
    )
    if commit:
        await session.commit()
    return task


@agent_onboarding.records("task_create")
async def create_task(
    session: AsyncSession, principal: Principal, request: TaskCreateRequest
) -> TaskOut:
    agent = effective_agent(principal, request.agent)
    batch = request.session_batch_id or uuid.uuid4()
    task = await _create_task_core(
        session, principal, request, agent=agent, batch=batch, commit=True
    )
    return task_out(task)


async def list_tasks(
    session: AsyncSession,
    principal: Principal,
    *,
    statuses: list[str] | None = None,
    assigned_role: str | None = None,
    namespace: str | None = None,
    limit: int = 100,
    assignee: str | None = None,
    required_role: str | None = None,
    parent: str | None = None,
    kind: str | None = None,
    claimable_by_me: bool = False,
    task_keys: list[str] | None = None,
    diagram_key: str | None = None,
) -> TaskListResponse:
    require_permission(principal, "task:read")
    query = select(CollabTask).where(
        CollabTask.workspace_id == principal.workspace.id
    )
    if task_keys:
        # 2026-10-24 #11: ?task_key= used to be ignored and return the whole board.
        query = query.where(CollabTask.task_key.in_(task_keys)
                            | CollabTask.canonical_id.in_(task_keys))
    if assignee == "me":
        mine = await collab.my_refs(session, principal)
        query = query.where(CollabTask.assignee.in_(mine))
    elif assignee:
        query = query.where(CollabTask.assignee == assignee)
    if required_role:
        query = query.where(CollabTask.required_role == required_role)
    if parent:
        parent_task = await _get_task(session, principal.workspace.id, parent)
        query = query.where(CollabTask.parent_task_id == parent_task.id)
    if kind:
        query = query.where(CollabTask.kind == kind)
    if diagram_key:
        # A diagram's chunk tasks, however many times a chunk was dispatched.
        query = query.where(CollabTask.input_context["diagram"]["key"].astext == diagram_key)
    if claimable_by_me:
        query = query.where(*await claimable_conditions(session, principal))
    if statuses:
        query = query.where(CollabTask.status.in_(statuses))
    if assigned_role:
        query = query.where(CollabTask.assigned_role == assigned_role)
    if namespace:
        ensure_namespace(principal, namespace)
        query = query.where(CollabTask.namespace == namespace)
    elif not principal.is_commander and "*" not in principal.allowed_namespaces:
        query = query.where(CollabTask.namespace.in_(principal.allowed_namespaces))
        if assignee == "me" or claimable_by_me:
            await _warn_out_of_reach(session, principal)
    tasks = (
        await session.execute(
            query.order_by(
                CollabTask.priority.desc(), CollabTask.updated_at.desc()
            ).limit(min(limit, settings.max_page_size))
        )
    ).scalars().all()
    return TaskListResponse(items=[task_summary(task) for task in tasks])


async def claimable_conditions(session: AsyncSession, principal: Principal) -> list[Any]:
    """Exactly what claim_task checks, as filters (2026-10-24 #12: orientation and
    list_tasks(claimable_by_me) used to disagree): the task:claim permission,
    status, assignee, required role and capabilities (not for people, as in
    check_claimable), assigned role, and a namespace the token may write."""
    if not can(principal, "task:claim"):
        return [false()]  # claim_task would refuse every one (a SCOUT, say)
    mine = await collab.my_refs(session, principal)
    conditions: list[Any] = [
        CollabTask.workspace_id == principal.workspace.id,
        CollabTask.status.in_(list(_CLAIMABLE)),
        (CollabTask.assignee.is_(None)) | (CollabTask.assignee.in_(mine)),
    ]
    if not principal.is_human:
        entry = await collab.my_directory_entry(session, principal)
        conditions += [
            (CollabTask.required_role.is_(None))
            | (CollabTask.required_role == ((entry or {}).get("role") or "")),
            CollabTask.required_capabilities.contained_by(
                list((entry or {}).get("capabilities") or [])
            ),
        ]
    if not principal.is_commander:
        conditions.append(CollabTask.assigned_role.is_(None)
                          | (CollabTask.assigned_role == principal.role.value))
    if principal.auth_kind is AuthKind.AGENT:
        writes = (principal.write_namespaces if principal.write_namespaces is not None
                  else principal.allowed_namespaces)
        if "*" not in writes:
            conditions.append(CollabTask.namespace.in_(list(writes)))
    return conditions


async def _warn_out_of_reach(session: AsyncSession, principal: Principal) -> None:
    """Tasks assigned to the caller in namespaces its token cannot read: say so."""
    mine = await collab.my_refs(session, principal)
    rows = (await session.execute(
        select(CollabTask.namespace, func.count(CollabTask.id))
        .where(CollabTask.workspace_id == principal.workspace.id,
               CollabTask.assignee.in_(mine),
               CollabTask.status.not_in([TaskStatus.DONE, TaskStatus.FAILED,
                                         TaskStatus.CANCELLED]),
               CollabTask.namespace.not_in(list(principal.allowed_namespaces)))
        .group_by(CollabTask.namespace)
    )).all()
    if rows:
        add_warning(
            "assigned_tasks_out_of_reach",
            f"{sum(count for _, count in rows)} open task(s) assigned to you are in "
            "namespaces your token cannot read; ask an owner to move them or widen "
            "your token",
            namespaces={namespace: count for namespace, count in rows})


@access_log.reads("task")
async def get_task(
    session: AsyncSession, principal: Principal, task_key: str
) -> TaskOut:
    require_permission(principal, "task:read")
    task = await _get_task(session, principal.workspace.id, task_key)
    ensure_namespace(principal, task.namespace)
    out = task_out(task)
    out.acceptance_state = await acceptance_state(session, task)
    out.children_summary = await collab.children_summary(session, task)
    return out


async def acceptance_state(session: AsyncSession, task: CollabTask) -> list[dict[str, Any]]:
    """Per acceptance item status: the latest audit result, else the task status.

    The console shows these next to the items so a person sees what still needs
    judgement before reading the description.
    """
    latest: dict[str, str] = {}
    rows = (await session.execute(
        text("SELECT items FROM audits WHERE workspace_id = :ws AND task_id = :t "
             "ORDER BY created_at, id"),
        {"ws": task.workspace_id, "t": task.id})).scalars().all()
    for items in rows:
        for item in items or []:
            if isinstance(item, dict) and item.get("item_id"):
                latest[str(item["item_id"])] = str(item.get("result"))
    done = task.status is TaskStatus.DONE
    out: list[dict[str, Any]] = []
    for item in task.acceptance or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or item.get("item_id") or "")
        out.append({"id": item_id,
                    "status": latest.get(item_id) or ("PASS" if done else "PENDING")})
    return out


@access_log.reads("task")
@agent_onboarding.records("task_claim")
@_task_failure_event("CLAIM")
async def claim_task(
    session: AsyncSession,
    principal: Principal,
    task_key: str,
    request: TaskClaimRequest,
) -> TaskContextResponse:
    require_permission(principal, "task:claim")
    agent = effective_agent(principal, request.agent)
    batch = request.session_batch_id or uuid.uuid4()
    task = await _get_task(session, principal.workspace.id, task_key, for_update=True)
    ensure_namespace(principal, task.namespace, write=True)
    ensure_task_role(principal, task.assigned_role)
    if task.status is TaskStatus.ESCALATED_TO_HUMAN:
        raise EscalatedToHuman(task_out(task).model_dump(mode="json"))
    if task.status not in _CLAIMABLE and task.status is not TaskStatus.IN_PROGRESS:
        raise InvalidStateTransition(task.status.value, "IN_PROGRESS")
    if not await collab.assignee_matches(session, principal, task, agent):
        _check_assignee(task, principal, agent)
    await collab.check_claimable(session, principal, task)
    now = await db_now(session)
    _check_lease(task, agent, now)
    before = task_snapshot(task)
    if task.claimed_by != agent or task.status is not TaskStatus.IN_PROGRESS:
        task.claimed_at = now
    _clear_lease(task)
    ttl = lease_service.ttl_for(request.lease_ttl_seconds, request.lease_seconds)
    task.status = TaskStatus.IN_PROGRESS
    task.claimed_by = agent
    task.acceptance_locked = True  # quality control 1.0 §3.1
    task.lease_owner = agent
    task.lease_expires_at = now + timedelta(seconds=ttl)
    task.version += 1
    task.updated_at = now
    await session.flush()
    lease = await lease_service.grant_task_lease(session, principal, task, agent, ttl)
    context = (
        await collab.load_context(session, principal, task, max_bytes=request.context_max_bytes)
        if request.include_context
        else []
    )
    await refresh_task_taint(session, task)
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="UPDATE",
        before=before,
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="TASK_CLAIMED",
        payload={
            "task_key": task.task_key,
            "version": task.version,
            "lease_epoch": task.lease_epoch,
            "lease_id": lease.lease_id,
            "lease_expires_at": task.lease_expires_at.isoformat()
            if task.lease_expires_at
            else None,
        },
        namespace=task.namespace,
    )
    await session.commit()
    return TaskContextResponse(
        task=task_out(task),
        lease=lease_service.lease_out(lease),
        upstream_briefs=await _upstream_briefs(session, task, principal),
        slots=await _context_slots(session, principal, task),
        documents=await _context_documents(session, principal, task),
        context=context,
        session_batch_id=batch,
    )


@_task_failure_event("RENEW")
async def renew_task(
    session: AsyncSession,
    principal: Principal,
    task_key: str,
    request: TaskRenewRequest,
) -> TaskOut:
    require_permission(principal, "task:claim")
    agent = effective_agent(principal, request.agent)
    task = await _get_task(session, principal.workspace.id, task_key, for_update=True)
    if task.status not in _ACTIVE_LEASE_STATUSES:
        raise InvalidStateTransition(task.status.value, "renew")
    now = await db_now(session)
    _check_epoch(task, request.lease_epoch)
    lease_service.check_fencing(task, request.fencing_token)
    await lease_service.require_holder(session, task, agent, now)
    lease = await lease_service.active_task_lease(session, task)
    task.lease_owner = agent
    task.lease_expires_at = now + timedelta(
        seconds=request.lease_seconds
        or (lease.ttl_seconds if lease else settings.default_task_lease_seconds)
    )
    await lease_service.touch(session, task, now)
    task.claimed_by = agent
    task.version += 1
    task.updated_at = now
    await session.commit()
    return task_out(task)


async def renew_lease_explicit(
    session: AsyncSession,
    principal: Principal,
    task_key: str,
    request: TaskLeaseRenewRequest,
) -> TaskOut:
    """Renew only in the last 300 s of a lease, at most 12 times per claim (§4.2).

    The liveness test runs as one SQL statement against the database clock, and
    the fence (``lease_epoch``) does not move, so a stale holder still fails.
    """
    require_permission(principal, "task:claim")
    agent = effective_agent(principal, request.agent)
    task = await _get_task(session, principal.workspace.id, task_key, for_update=True)
    ensure_namespace(principal, task.namespace, write=True)
    if task.status is not TaskStatus.IN_PROGRESS:
        raise InvalidStateTransition(task.status.value, "renew")
    now = await db_now(session)
    lease_service.check_fencing(task, request.fencing_token)
    await lease_service.require_holder(session, task, agent, now)
    remaining = (task.lease_expires_at - now).total_seconds()
    window = settings.lease_renew_window_seconds
    if remaining >= window:
        raise ApiError(
            409,
            "lease_renew_too_early",
            f"renew only when less than {window} seconds remain",
            details={"remaining_seconds": int(remaining), "window_seconds": window},
        )
    limit = settings.max_lease_renews
    renewals = int(task.lease_renew_count or 0)
    if renewals >= limit:
        raise ApiError(
            409,
            "lease_renew_limit",
            "this claim reached its renewal limit; claim the task again",
            details={"renewals": renewals, "limit": limit},
        )
    before = task_snapshot(task)
    task.lease_renew_count = renewals + 1
    task.lease_expires_at = now + timedelta(seconds=settings.default_task_lease_seconds)
    task.version += 1
    task.updated_at = now
    await lease_service.touch(session, task, now)
    await session.flush()
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=uuid.uuid4(),
        agent_signature=agent,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="UPDATE",
        before=before,
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="TASK_LEASE_RENEWED",
        payload={"task_key": task.task_key, "renewals": task.lease_renew_count,
                 "lease_expires_at": task.lease_expires_at.isoformat()},
        namespace=task.namespace,
    )
    await session.commit()
    return task_out(task)


@_task_failure_event("RELEASE")
async def release_task(
    session: AsyncSession,
    principal: Principal,
    task_key: str,
    request: TaskReleaseRequest,
) -> TaskOut:
    require_permission(principal, "task:transition")
    agent = effective_agent(principal, request.agent)
    batch = request.session_batch_id or uuid.uuid4()
    task = await _get_task(session, principal.workspace.id, task_key, for_update=True)
    now = await db_now(session)
    _check_epoch(task, request.lease_epoch)
    if request.force:
        if principal.auth_kind is not AuthKind.HUMAN_ADMIN:
            raise Forbidden("only the admin console may force-release a task")
    else:
        lease_service.check_fencing(task, request.fencing_token)
        await lease_service.require_holder(session, task, agent, now)
    before = task_snapshot(task)
    task.status = TaskStatus.READY
    _clear_lease(task)
    task.version += 1
    task.updated_at = now
    await session.flush()
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="UPDATE",
        before=before,
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="TASK_RELEASED",
        payload={"task_key": task_key, "reason": request.reason},
        namespace=task.namespace,
    )
    await session.commit()
    return task_out(task)


async def _release_dependents(
    session: AsyncSession, completed: CollabTask, principal: Principal
) -> None:
    await collab.on_done(session, completed)
    dependent_ids = (
        await session.execute(
            select(WorkspaceLink.source_id).where(
                WorkspaceLink.workspace_id == completed.workspace_id,
                WorkspaceLink.target_id == completed.id,
                WorkspaceLink.relation_type == LinkType.DEPENDS_ON,
            )
        )
    ).scalars().all()
    for dependent_id in dependent_ids:
        dependent = (
            await session.execute(
                select(CollabTask)
                .where(
                    CollabTask.workspace_id == completed.workspace_id,
                    CollabTask.id == dependent_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if dependent is None or dependent.status is not TaskStatus.TODO:
            continue
        if (
            not principal.is_commander
            and "*" not in principal.allowed_namespaces
            and dependent.namespace not in principal.allowed_namespaces
        ):
            continue
        dependency_ids = (
            await session.execute(
                select(WorkspaceLink.target_id).where(
                    WorkspaceLink.workspace_id == completed.workspace_id,
                    WorkspaceLink.source_id == dependent.id,
                    WorkspaceLink.relation_type == LinkType.DEPENDS_ON,
                )
            )
        ).scalars().all()
        if not dependency_ids:
            continue
        dependencies = (
            await session.execute(
                select(CollabTask).where(
                    CollabTask.workspace_id == completed.workspace_id,
                    CollabTask.id.in_(dependency_ids),
                )
            )
        ).scalars().all()
        if not all(dep.status is TaskStatus.DONE for dep in dependencies):
            continue
        before = task_snapshot(dependent)
        dependent.status = TaskStatus.READY
        dependent.version += 1
        dependent.updated_at = utcnow()
        await session.flush()
        await record_audit(
            session,
            workspace_id=completed.workspace_id,
            session_batch_id=uuid.uuid4(),
            agent_signature="dependency_scheduler",
            target_table="collab_tasks",
            target_record_id=dependent.id,
            operation="UPDATE",
            before=before,
            after=task_snapshot(dependent),
        )
        await append_event(
            session,
            workspace_id=completed.workspace_id,
            agent="dependency_scheduler",
            event_type="TASK_READY",
            payload={
                "task_key": dependent.task_key,
                "unblocked_by": completed.task_key,
            },
            namespace=dependent.namespace,
        )


@_task_failure_event("HANDOFF")
async def handoff_task(
    session: AsyncSession,
    principal: Principal,
    task_key: str,
    request: TaskHandoffRequest,
) -> TaskActionResponse:
    require_permission(principal, "task:transition")
    agent = effective_agent(principal, request.agent)
    batch = request.session_batch_id or uuid.uuid4()
    task = await _get_task(session, principal.workspace.id, task_key, for_update=True)
    ensure_namespace(principal, task.namespace, write=True)
    ensure_task_role(principal, task.assigned_role)
    now = await db_now(session)
    # A holder whose lease ended learns that first, whatever the task became.
    lease_service.check_fencing(task, request.fencing_token)
    if at_least(LATEST_VERSION):
        await lease_service.require_holder(session, task, agent, now)
    if task.status is TaskStatus.ESCALATED_TO_HUMAN:
        raise EscalatedToHuman(task_out(task).model_dump(mode="json"))
    if task.status not in (
        TaskStatus.IN_PROGRESS,
        TaskStatus.REVIEWING,
        TaskStatus.HANDOFF,
    ):
        raise InvalidStateTransition(task.status.value, request.next_status)
    _check_version(task, request.expected_version)
    _check_lease(task, agent, now)
    _check_epoch(task, request.lease_epoch)
    _require_claim_owner(task, principal, agent)
    _require_active_lease(task, agent, now)
    brief = (
        validate_handoff_brief(request.brief.model_dump())
        if request.brief is not None
        else None
    )
    if (brief is not None and "owner_report" not in brief and not principal.is_human
            and request.next_status in ("REVIEWING", "HANDOFF")
            and await automation.mode_for_task(session, principal.workspace.id, task.task_key)
            == "full"):
        add_warning("owner_report_missing",
                    "full automation: add owner_report {what_changed, how_to_try, "
                    "not_yet_done} in plain words; it is what the owner reads",
                    field="brief.owner_report")
    await collab.handoff_checks(session, principal, task, agent, request.next_status, brief)

    artifact_uri = request.artifact_uri
    if request.artifact_text is not None and request.artifact_uri is not None:
        raise ApiError(
            422,
            "ambiguous_artifact",
            "provide artifact_text or artifact_uri, not both",
        )
    artifact_version: int | None = None
    artifact_hash: str | None = None
    if request.artifact_text:
        # Handoff artifacts are new documents: the same create permission and
        # path policy as a direct document write apply.
        require_permission(principal, "doc:create")
        artifact_doc, artifact_uri = await create_artifact(
            session,
            workspace=principal.workspace,
            principal=principal,
            task=task,
            agent=agent,
            artifact_text=request.artifact_text,
            risk_flag=bool(brief.get("risk_flag")) if brief else False,
            batch_id=batch,
        )
        artifact_version = artifact_doc.version
        artifact_hash = artifact_doc.content_hash
    elif artifact_uri:
        artifact_version, artifact_hash = await bind_task_artifact(
            session, principal, task, artifact_uri.removeprefix("doc://")
        )

    if request.next_status == "REVIEWING":
        # Resubmission resolves the outstanding feedback (PRD AG-CO-01 #3).
        task.review_history = _resolve_review_history(task.review_history)
        task.handoff_lease_expires_at = task.lease_expires_at
    if request.next_status == "REVIEWING" and (
        task.review_feedback is not None or task.reviewed_by is not None
    ):
        history = list(task.reject_history or [])
        history.append(
            {
                "kind": "previous_review",
                "feedback": task.review_feedback,
                "reviewed_by": task.reviewed_by,
                "at": utcnow().isoformat(),
                "version": task.version,
            }
        )
        task.reject_history = history[-settings.max_reject_history :]
        task.review_feedback = None
        task.reviewed_by = None

    auto_release: dict[str, Any] | None = None
    if request.next_status == "DONE" and principal.is_super_admin:
        raise Forbidden("a SUPER_ADMIN cannot approve DONE; a human owner must")
    if request.next_status == "DONE" and not principal.is_human:
        if not quality.enforced():
            raise Forbidden(
                "agents must hand off to REVIEWING; only a human can mark a task DONE"
            )
        # Quality control tier 2: gates passed, acceptance from a trusted runner.
        auto_release = await quality.check_auto_release(session, principal, task)
    if request.human_feedback is not None and not principal.is_human:
        raise Forbidden("human_feedback is reserved for human reviewers")
    before = task_snapshot(task)
    task.status = TaskStatus(request.next_status)
    task.handoff_brief = brief
    task.artifact_uri = artifact_uri
    task.artifact_version = artifact_version
    task.artifact_hash = artifact_hash
    task.handed_off_by = agent
    task.output_artifact = request.output_artifact
    task.human_feedback = request.human_feedback if principal.is_human else None
    _clear_lease(task)
    task.version += 1
    task.updated_at = utcnow()
    await session.flush()
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="UPDATE",
        before=before,
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="TASK_HANDOFF",
        payload={
            "task_key": task.task_key,
            "status": task.status.value,
            "version": task.version,
            "summary": brief.get("summary") if brief else None,
            "artifact_uri": artifact_uri,
        },
        namespace=task.namespace,
    )
    if task.status is TaskStatus.DONE:
        await _release_dependents(session, task, principal)
        await quality.released(session, principal.workspace.id, task,
                               path="AUTO" if auto_release else "HUMAN", info=auto_release)

    next_task: CollabTask | None = None
    if request.create_next is not None:
        require_permission(principal, "task:create")
        next_request = TaskCreateRequest(
            task_key=request.create_next.task_key,
            title=request.create_next.title,
            project=request.create_next.project,
            repo=request.create_next.repo,
            assigned_role=request.create_next.assigned_role,
            priority=request.create_next.priority,
            namespace=request.create_next.namespace or task.namespace,
            input_context=request.create_next.input_context,
            agent=agent,
            session_batch_id=batch,
        )
        next_task = await _create_task_core(
            session, principal, next_request, agent=agent, batch=batch, commit=False
        )
        await add_link(
            session,
            workspace_id=principal.workspace.id,
            source_id=next_task.id,
            target_id=task.id,
            relation_type=LinkType.REFERENCES,
        )

    await session.commit()
    return TaskActionResponse(
        task=task_out(task),
        artifact_uri=artifact_uri,
        session_batch_id=batch,
        status=task.status,
        version=task.version,
        task_key=task.task_key,
    )


def _require_approve_done(principal: Principal) -> None:
    """Role rules v0.2 §2.6: only the human OWNER approves DONE.

    Under QUALITY_CONTROL an AUDITOR releases through the tier gates; that is
    an explicitly configured, gated delegation, not an owner decision.
    Enforced only with OWNER_ONLY_DONE=true.
    """
    if not settings.owner_only_done:
        return
    if has_capability(principal, "task:approve_done"):
        return
    if settings.quality_control and principal.role is RoleScope.AUDITOR:
        return
    raise ApiError(
        403,
        "role_permission_denied",
        "only the human OWNER approves DONE",
        details={"action": "task:approve_done", "role": capability_role(principal)},
    )


async def _deploy_evidence_for_done(
    session: AsyncSession, principal: Principal, task: CollabTask
) -> dict[str, Any] | bool | None:
    """Does an agent's DONE need a verified deploy, and is there one?

    Loop A-03 held such a DONE until a deploy that carried the task passed its
    smoke test. It is off by default since 2026-10-29 (DEPLOY_EVIDENCE_REQUIRED):
    the deploy gate is off the task path and a merged pull request finishes a
    task. Returns None when no deploy is needed, the evidence when it exists,
    and False when it is still missing.
    """
    from app.services import deploy_gate

    if not settings.deploy_evidence_required:
        return None
    if not task.repo or not deploy_gate.governs(principal.workspace.id):
        return None
    if await automation.mode_for_task(session, principal.workspace.id, task.task_key) != "full":
        return None
    return await deploy_gate.deploy_evidence(task.task_key) or False


@agent_onboarding.records("review")
@_task_failure_event("REVIEW")
async def review_task(
    session: AsyncSession,
    principal: Principal,
    task_key: str,
    request: TaskReviewRequest,
    *,
    grant_id: str | None = None,
) -> TaskActionResponse:
    if not (principal.is_commander or can(principal, "task:review")):
        require_permission(principal, "task:review")
    validate_review_next_status(request.decision, request.next_status)
    if request.decision == "reject" and not (request.feedback or "").strip():
        # A verdict without a reason is not a verdict (delivery protocol §7).
        raise ApiError(422, "review_feedback_required",
                       "a rejection must say why: pass feedback",
                       details={"field": "feedback"})
    agent = effective_agent(principal, request.agent)
    batch = request.session_batch_id or uuid.uuid4()
    task = await _get_task(session, principal.workspace.id, task_key, for_update=True)
    ensure_namespace(principal, task.namespace, write=True)
    if grant_service.requires_grant(principal):
        await grant_service.require_grant(
            session, principal, grant_id, "task:review", task)
    if task.status is not TaskStatus.REVIEWING and task.status not in (
        TaskStatus.AWAITING_HUMAN,
        TaskStatus.ESCALATED_TO_HUMAN,
    ):
        raise TaskNotReviewable(task.status.value, request.decision)
    if task.status in (TaskStatus.AWAITING_HUMAN, TaskStatus.ESCALATED_TO_HUMAN):
        if not principal.is_human:
            raise Forbidden(
                "a human account is required to resolve a task awaiting human review"
            )
    if (
        request.decision == "approve"
        and not principal.is_human
        and task.handed_off_by in (agent, principal.agent_signature)
    ):
        # Rejecting one's own work is harmless; approving it is self-certification.
        raise Forbidden(
            "an agent cannot approve a task it handed off; another reviewer "
            "or a human must decide"
        )
    _check_version(task, request.expected_version)
    _check_lease(task, agent)
    audit = await quality.before_review(session, principal, task, request, agent)
    before = task_snapshot(task)
    human_decision = principal.is_human and principal.is_commander
    escalated = False

    task.reviewed_by = principal.agent_signature
    artifact_verified = False
    awaiting_deploy = False
    deploy_evidence: dict[str, Any] | bool | None = None
    effective_status: TaskStatus | None = None
    reassigned: bool | None = None
    if request.decision == "approve":
        # review_feedback means "not yet addressed"; an approval leaves none.
        task.review_feedback = None
        task.review_history = _resolve_review_history(task.review_history)
        next_status = audit["next_status"] or request.next_status or "DONE"
        if next_status == "DONE":
            _require_approve_done(principal)
            if not principal.is_human:
                deploy_evidence = await _deploy_evidence_for_done(session, principal, task)
                if deploy_evidence is False:
                    # Loop A-03 / R-04: no verified deploy yet, so not DONE. The
                    # approval stands (the gate's G-03 sees it) and the task waits
                    # in HANDOFF; DEPLOY_VERIFIED tells its people when it shipped.
                    next_status = "HANDOFF"
                    awaiting_deploy = True
                    # Shown on the task itself, so approved work waiting for its deploy
                    # never reads as work sent back.
                    task.handoff_brief = {**(task.handoff_brief or {}), "approval": {
                        "by": principal.agent_signature, "at": utcnow().isoformat(),
                        "awaiting_deploy": True}}
        task.status = TaskStatus(next_status)
        if human_decision:
            task.human_feedback = request.feedback
        if human_decision:
            task.ping_pong_count = 0
        _clear_lease(task)
        task.version += 1
        task.updated_at = utcnow()
        event_type = "HUMAN_APPROVED" if human_decision else "TASK_APPROVED"
        if task.status is TaskStatus.DONE:
            await _release_dependents(session, task, principal)
        if task.artifact_uri and task.artifact_uri.startswith("doc://"):
            try:
                artifact_verified = await verify_artifact_for_review(
                    session,
                    principal,
                    task.artifact_uri.removeprefix("doc://"),
                    actor=agent,
                    batch=batch,
                    task_key=task.task_key,
                    note=request.feedback,
                    expected_version=task.artifact_version,
                    expected_hash=task.artifact_hash,
                )
            except ApiError:
                artifact_verified = False
    else:
        task.review_feedback = request.feedback
        task.review_round += 1
        history = list(task.review_history or [])
        history.append(
            {
                "round": task.review_round,
                "reviewer": principal.agent_signature,
                "decision": request.decision,
                "next_status": request.next_status or "READY",
                "review_feedback": request.feedback,
                "created_at": utcnow().isoformat(),
                "resolved_at": None,
            }
        )
        task.review_history = history[-settings.max_reject_history :]
        task.ping_pong_count += 1
        entry = {
            "by": agent,
            "feedback": request.feedback,
            "at": utcnow().isoformat(),
            "version": task.version,
            "kind": "reject",
        }
        history = list(task.reject_history or [])
        history.append(entry)
        task.reject_history = history[-settings.max_reject_history :]
        rework = request.next_status == "IN_PROGRESS"
        relayed = False
        if human_decision:
            task.human_feedback = request.feedback
            task.status = TaskStatus.READY
            event_type = "HUMAN_REJECTED"
        elif task.ping_pong_count >= task.max_retries and await automation.mode_for_task(
                session, principal.workspace.id, task.task_key) == "full":
            # Full automation (Loop A-04): the next executor takes over; nobody
            # is asked a technical question.
            task.status = TaskStatus.HANDOFF
            relayed = True
            event_type = "TASK_RELAYED"
        elif task.ping_pong_count >= task.max_retries:
            task.status = TaskStatus.ESCALATED_TO_HUMAN
            escalated = True
            event_type = "TASK_ESCALATED"
        else:
            task.status = TaskStatus.READY
            event_type = "TASK_REJECTED"
        _clear_lease(task)
        if rework and not escalated and not relayed:
            now = await db_now(session)
            expiry = task.handoff_lease_expires_at
            if task.handed_off_by and expiry is not None and expiry > now:
                # Back to the same executor under the lease it still had.
                task.status = TaskStatus.IN_PROGRESS
                task.claimed_by = task.handed_off_by
                task.lease_owner = task.handed_off_by
                task.lease_expires_at = expiry
            reassigned = False
            effective_status = task.status
            event_type = "TASK_RETURNED_FOR_REWORK"
        task.version += 1
        task.updated_at = utcnow()

    await session.flush()
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="UPDATE",
        before=before,
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type=event_type,
        payload={
            "task_key": task.task_key,
            "status": task.status.value,
            "version": task.version,
            "feedback": request.feedback,
            "ping_pong_count": task.ping_pong_count,
            "max_retries": task.max_retries,
            "artifact_verified": artifact_verified,
            **({"awaiting_deploy": True} if awaiting_deploy else {}),
            **({"deploy_evidence": deploy_evidence} if isinstance(deploy_evidence, dict)
               else {}),
        },
        namespace=task.namespace,
    )
    await quality.after_review(session, principal, task, request, audit)
    await session.commit()
    return TaskActionResponse(
        task=task_out(task),
        escalated=escalated,
        effective_status=effective_status,
        reassigned=reassigned,
        awaiting_deploy=awaiting_deploy or None,
        deploy_evidence=deploy_evidence if isinstance(deploy_evidence, dict) else None,
        session_batch_id=batch,
        status=task.status,
        version=task.version,
        task_key=task.task_key,
    )


@_task_failure_event("ESCALATE")
async def escalate_task(
    session: AsyncSession,
    principal: Principal,
    task_key: str,
    request: TaskEscalateRequest,
) -> TaskActionResponse:
    require_permission(principal, "task:transition")
    agent = effective_agent(principal, request.agent)
    batch = request.session_batch_id or uuid.uuid4()
    task = await _get_task(session, principal.workspace.id, task_key, for_update=True)
    ensure_namespace(principal, task.namespace, write=True)
    ensure_task_role(principal, task.assigned_role)
    _check_version(task, request.expected_version)
    lease_service.check_fencing(task, request.fencing_token)
    if task.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED):
        raise InvalidStateTransition(task.status.value, "ESCALATED_TO_HUMAN")
    non_blocking = await automation.screen_escalation(
        session, principal, topic=request.topic, task_key=task.task_key,
        question=request.question or request.reason)
    if request.question:
        if non_blocking:
            # Full automation: the agent keeps its safe default and goes on.
            request = request.model_copy(update={"blocking": False})
        # AG-CO-05: a question with options; the holder keeps the task.
        if request.blocking and not principal.is_human:
            await lease_service.require_holder(session, task, agent, await db_now(session))
        escalation = await collab.escalate(session, principal, task, agent, request)
        await session.commit()
        return TaskActionResponse(
            task=task_out(task),
            escalated=True,
            escalation=escalation,
            session_batch_id=batch,
            status=task.status,
            version=task.version,
            task_key=task.task_key,
        )
    before = task_snapshot(task)
    task.status = TaskStatus.ESCALATED_TO_HUMAN
    _clear_lease(task)
    task.version += 1
    task.updated_at = utcnow()
    await session.flush()
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=agent,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="UPDATE",
        before=before,
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="TASK_ESCALATED",
        payload={
            "task_key": task.task_key,
            "reason": request.reason,
            "version": task.version,
        },
        namespace=task.namespace,
    )
    await session.commit()
    return TaskActionResponse(
        task=task_out(task),
        escalated=True,
        session_batch_id=batch,
        status=task.status,
        version=task.version,
        task_key=task.task_key,
    )


async def reap_expired_task_leases(
    session: AsyncSession, limit: int = 50
) -> int:
    """Expire overdue leases: the task returns to READY (or escalates) as an orphan."""
    now = await db_now(session)
    tasks = (
        await session.execute(
            select(CollabTask)
            .where(
                CollabTask.status.in_(
                    [TaskStatus.IN_PROGRESS, TaskStatus.REVIEWING]
                ),
                CollabTask.lease_expires_at.is_not(None),
                CollabTask.lease_expires_at < now,
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
    ).scalars().all()
    for task in tasks:
        await lease_service.orphan_task(
            session, task, state="expired", reason="timeout", actor="lease_reaper"
        )
    await lease_service.flag_missed_heartbeats(session)
    await session.commit()
    return len(tasks)


async def reap_task_timeouts(
    session: AsyncSession, threshold_seconds: int | None = None, limit: int = 100
) -> int:
    """M-14: a READY task assigned to a specific agent but left unclaimed for
    the timeout window is degraded and its assignment is opened for reassignment."""
    from app.config import get_settings

    threshold = (
        threshold_seconds
        if threshold_seconds is not None
        else get_settings().task_unclaimed_timeout_seconds
    )
    cutoff = await db_now(session) - timedelta(seconds=threshold)
    tasks = (
        await session.execute(
            select(CollabTask)
            .where(
                CollabTask.status == TaskStatus.READY,
                CollabTask.assignee.is_not(None),
                CollabTask.claimed_by.is_(None),
                CollabTask.updated_at < cutoff,
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
    ).scalars().all()
    for task in tasks:
        assignee = task.assignee
        before = task_snapshot(task)
        task.assignee = None
        task.version += 1
        task.updated_at = utcnow()
        await session.flush()
        await record_audit(
            session,
            workspace_id=task.workspace_id,
            session_batch_id=uuid.uuid4(),
            agent_signature="timeout_reaper",
            target_table="collab_tasks",
            target_record_id=task.id,
            operation="UPDATE",
            before=before,
            after=task_snapshot(task),
        )
        await append_event(
            session,
            workspace_id=task.workspace_id,
            agent="timeout_reaper",
            event_type="TASK_TIMEOUT_DEGRADED",
            payload={
                "task_key": task.task_key,
                "targets": [assignee, "human"],
                "details": {
                    "delivery_kind": "task.timeout_degraded",
                    "priority": 0,
                    "note": f"unclaimed for {threshold}s; reassignment opened",
                },
            },
            namespace=task.namespace,
        )
    await session.commit()
    return len(tasks)


async def reassign_task(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    task_key: str,
    *,
    assigned_role: str,
    reason: str | None,
    expected_version: int | None,
    reset_to_ready: bool,
    actor: str = "human_commander",
) -> TaskOut:
    task = await _get_task(session, workspace_id, task_key, for_update=True)
    _check_version(task, expected_version)
    before = task_snapshot(task)
    task.assigned_role = assigned_role
    if reset_to_ready:
        task.status = TaskStatus.READY
        _clear_lease(task)
    task.version += 1
    task.updated_at = utcnow()
    await session.flush()
    await record_audit(
        session,
        workspace_id=workspace_id,
        session_batch_id=uuid.uuid4(),
        agent_signature=actor,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="UPDATE",
        before=before,
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=workspace_id,
        agent=actor,
        event_type="TASK_REASSIGNED",
        payload={
            "task_key": task.task_key,
            "assigned_role": assigned_role,
            "status": task.status.value,
            "reason": reason,
        },
        namespace=task.namespace,
    )
    await session.commit()
    return task_out(task)


async def cancel_task(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    task_key: str,
    *,
    reason: str,
    expected_version: int | None,
    actor: str = "human_commander",
) -> TaskOut:
    task = await _get_task(session, workspace_id, task_key, for_update=True)
    _check_version(task, expected_version)
    if task.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED):
        raise InvalidStateTransition(task.status.value, "FAILED")
    lease = await lease_service.active_task_lease(session, task)
    if lease is not None:
        lease.state = "released"
        lease.ended_at = utcnow()
        lease.end_reason = "cancelled"
    cancelled_children = await collab.cancel_children(session, task, reason)
    before = task_snapshot(task)
    task.status = TaskStatus.FAILED
    task.review_feedback = reason
    task.reviewed_by = actor
    _clear_lease(task)
    task.version += 1
    task.updated_at = utcnow()
    await session.flush()
    await record_audit(
        session,
        workspace_id=workspace_id,
        session_batch_id=uuid.uuid4(),
        agent_signature=actor,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="UPDATE",
        before=before,
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=workspace_id,
        agent=actor,
        event_type="TASK_CANCELLED",
        payload={"task_key": task.task_key, "reason": reason,
                 "cancelled_children": cancelled_children},
        namespace=task.namespace,
    )
    await session.commit()
    return task_out(task)


async def cancel_own_task(session: AsyncSession, principal: Principal, task_key: str, *,
                          reason: str, expected_version: int | None,
                          grant_id: str | None = None) -> TaskOut:
    """Withdraw a task (2026-10-24: there was no cancel outside the admin API).
    Its creator may, while nobody else holds it; people and commanders always."""
    task = await _get_task(session, principal.workspace.id, task_key)
    ensure_namespace(principal, task.namespace, write=True)
    if grant_service.requires_grant(principal):
        await grant_service.require_grant(
            session, principal, grant_id, "task:cancel", task)
    if not (principal.is_human or principal.is_commander or principal.is_super_admin):
        if not await collab.created_by_matches(session, principal, task.created_by):
            raise Forbidden("only the task's creator, or a person, cancels it")
        if task.claimed_by and not await collab.created_by_matches(
                session, principal, task.claimed_by):
            raise ApiError(409, "task_claimed",
                           f"{task.claimed_by} holds this task; ask them or a person",
                           details={"claimed_by": task.claimed_by})
    if not reason.strip():
        raise ApiError(422, "validation_error", "a cancel needs a reason",
                       details={"field": "reason"})
    return await cancel_task(session, principal.workspace.id, task.task_key, reason=reason,
                             expected_version=expected_version,
                             actor=principal.agent_signature)


# ------------------------------------------------ orphaned tasks (R-07)


_ORPHAN_SQL = (
    "SELECT t.id FROM collab_tasks t WHERE t.workspace_id = :ws "
    "AND t.status NOT IN ('DONE', 'FAILED', 'CANCELLED') "
    "AND EXISTS (SELECT 1 FROM agent_directory d WHERE d.tenant_id = :tenant "
    "AND t.created_by = ANY(d.signatures) "
    "AND NOT EXISTS (SELECT 1 FROM agent_tokens tok "
    "WHERE tok.canonical_id = ANY(d.token_ids) AND tok.is_active "
    "AND tok.revoked_at IS NULL "
    "AND (tok.expires_at IS NULL OR tok.expires_at > NOW())))"
)


async def list_orphan_tasks(session: AsyncSession, principal: Principal) -> list[dict[str, Any]]:
    """Tasks whose creator identity has no active credential (R-07)."""
    if not (principal.is_human or principal.is_commander):
        raise Forbidden("people and commanders read orphaned tasks")
    ids = (await session.execute(text(_ORPHAN_SQL + " ORDER BY t.created_at"), {
        "ws": principal.workspace.id, "tenant": principal.tenant_id})).scalars().all()
    out: list[dict[str, Any]] = []
    for task_id in ids:
        task = await session.get(CollabTask, task_id)
        if task is not None:
            out.append(task_summary(task).model_dump(mode="json"))
    return out


async def claim_orphan_tasks(session: AsyncSession, principal: Principal,
                             task_keys: list[str], assignee: str | None = None,
                             reason: str | None = None) -> dict[str, Any]:
    """Give orphaned tasks an owner again: reassign, audit, return the result."""
    if not (principal.is_human or principal.is_commander):
        raise Forbidden("people and commanders claim orphaned tasks")
    target = (assignee or "").strip() or (
        f"human:{principal.user_id}" if principal.user_id is not None
        else f"agent:{principal.agent_signature}")
    if not target.startswith(("agent:", "human:")) or any(ch.isspace() for ch in target):
        raise ApiError(422, "validation_error", "assignee must be agent:<id> or human:<id>",
                       details={"field": "assignee"})
    orphan_ids = set((await session.execute(text(_ORPHAN_SQL), {
        "ws": principal.workspace.id, "tenant": principal.tenant_id})).scalars().all())
    batch = uuid.uuid4()
    claimed: list[str] = []
    skipped: list[str] = []
    for key in task_keys:
        task = await _get_task(session, principal.workspace.id, key, for_update=True)
        if task.id not in orphan_ids:
            skipped.append(task.task_key)
            continue
        before = task_snapshot(task)
        task.assignee = target
        task.version += 1
        task.updated_at = utcnow()
        await session.flush()
        await record_audit(
            session,
            workspace_id=principal.workspace.id,
            session_batch_id=batch,
            agent_signature=principal.agent_signature,
            target_table="collab_tasks",
            target_record_id=task.id,
            operation="UPDATE",
            before=before,
            after=task_snapshot(task),
        )
        await append_event(
            session,
            workspace_id=principal.workspace.id,
            agent=principal.agent_signature,
            event_type="TASK_REASSIGNED",
            payload={"task_key": task.task_key, "assignee": target,
                     "reason": (reason or "orphaned creator identity")[:500]},
            namespace=task.namespace,
        )
        claimed.append(task.task_key)
    await session.commit()
    return {"assignee": target, "claimed": claimed, "skipped": skipped}


# ------------------------------------------------ collaboration (PRD R2.1)


async def _append_erratum(session: AsyncSession, principal: Principal, task: CollabTask,
                          agent: str, text_: str) -> dict[str, Any]:
    """The author of the last handoff corrects it while it is reviewed: appended to
    the brief, never rewriting it, so the reviewer sees both."""
    if task.status not in (TaskStatus.REVIEWING, TaskStatus.HANDOFF) or (
            not principal.is_human and task.handed_off_by != agent):
        raise Forbidden("only the author of the last handoff adds an erratum, while the "
                        "task is in review")
    brief = dict(task.handoff_brief or {})
    errata = list(brief.get("errata") or [])
    if len(errata) >= 20:
        raise ApiError(422, "validation_error", "at most 20 errata per handoff")
    entry = {"by": agent, "at": utcnow().isoformat(), "text": text_}
    brief["errata"] = [*errata, entry]
    task.handoff_brief = brief
    task.version += 1
    task.updated_at = utcnow()
    await append_event(
        session, workspace_id=principal.workspace.id, agent=agent, event_type="TASK_ERRATUM",
        payload={"task_key": task.task_key, "id": task.canonical_id, "erratum": entry,
                 "targets": [f"agent:{task.reviewed_by}"] if task.reviewed_by else []},
        namespace=task.namespace)
    await session.commit()
    return {"erratum": entry, "errata": brief["errata"],
            "task": task_out(task).model_dump(mode="json")}


async def write_continuation(
    session: AsyncSession, principal: Principal, task_key: str, request: Any
) -> dict[str, Any]:
    """AG-CO-04: the holder writes a continuation pack; ``handoff`` hands off too."""
    require_permission(principal, "task:transition")
    agent = effective_agent(principal, None)
    task = await _get_task(session, principal.workspace.id, task_key, for_update=True)
    ensure_namespace(principal, task.namespace, write=True)
    if request.erratum:
        return await _append_erratum(session, principal, task, agent, request.erratum)
    data = request.model_dump(mode="json")
    pointer = await collab.write_continuation(session, principal, task, agent, data)
    task.version += 1
    task.updated_at = utcnow()
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="TASK_CONTINUATION",
        payload={"task_key": task.task_key, "id": task.canonical_id,
                 "continuation": pointer,
                 "targets": [request.handoff_to] if request.handoff_to else []},
        namespace=task.namespace,
    )
    await session.commit()
    result: dict[str, Any] = {"continuation": pointer, "task": task_out(task).model_dump(
        mode="json")}
    if request.handoff:
        handoff = await handoff_task(
            session, principal, task.task_key,
            TaskHandoffRequest(next_status="HANDOFF", brief={
                "summary": (data.get("done") or ["continuation written"])[-1][:500],
                "key_metrics": {}, "unverified_hypotheses": [], "risk_flag": False,
                **(request.brief or {})}),
        )
        result["task"] = handoff.task.model_dump(mode="json")
    return result


async def super_admin_update_task(
    session: AsyncSession,
    principal: Principal,
    task_key: str,
    request: TaskUpdateRequest,
) -> TaskOut:
    """SUPER_ADMIN edits task fields (never DONE, never locked acceptance)."""
    if not principal.is_super_admin:
        raise Forbidden("only a SUPER_ADMIN token may edit task fields directly")
    reason = (request.reason or "").strip()
    if not reason:
        raise ApiError(422, "reason_required", "an update needs a reason",
                       details={"field": "reason"})
    task = await _get_task(session, principal.workspace.id, task_key, for_update=True)
    ensure_namespace(principal, task.namespace, write=True)
    _check_version(task, request.expected_version)
    before = task_snapshot(task)
    changed: list[str] = []
    if request.title is not None and request.title != task.title:
        task.title = request.title
        changed.append("title")
    if request.description is not None and request.description != task.description:
        task.description = request.description
        changed.append("description")
    if request.priority is not None and request.priority != task.priority:
        task.priority = request.priority
        changed.append("priority")
    if request.assignee is not None and (request.assignee or None) != task.assignee:
        task.assignee = request.assignee or None
        changed.append("assignee")
    if request.status is not None:
        if request.status == "DONE":
            raise Forbidden("a SUPER_ADMIN cannot approve DONE; a human owner must")
        if request.status == "IN_PROGRESS":
            raise ApiError(409, "claim_required",
                           "IN_PROGRESS needs a lease; claim the task instead")
        if TaskStatus(request.status) is not task.status:
            task.status = TaskStatus(request.status)
            changed.append("status")
    if not changed:
        raise ApiError(422, "validation_error", "nothing to update",
                       details={"field": "changes"})
    task.version += 1
    task.updated_at = utcnow()
    await session.flush()
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=uuid.uuid4(),
        agent_signature=principal.agent_signature,
        target_table="collab_tasks",
        target_record_id=task.id,
        operation="UPDATE",
        before=before,
        after=task_snapshot(task),
    )
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type="TASK_UPDATED",
        payload={"task_key": task.task_key, "fields": changed, "reason": reason,
                 "status": task.status.value},
        namespace=task.namespace,
    )
    await session.commit()
    return task_out(task)


async def create_subtasks(
    session: AsyncSession, principal: Principal, task_key: str, request: Any
) -> dict[str, Any]:
    """AG-CO-08: up to 20 children, atomically, by the holder or the lead."""
    require_permission(principal, "task:create")
    agent = effective_agent(principal, None)
    parent = await _get_task(session, principal.workspace.id, task_key, for_update=True)
    ensure_namespace(principal, parent.namespace, write=True)
    if not collab.may_lead(principal, parent, agent):
        raise Forbidden("only the task's holder or lead creates its subtasks")
    if (parent.tree_depth or 0) + 1 > collab.MAX_TREE_DEPTH:
        raise ApiError(422, "task_tree_too_deep", "task trees are at most 4 levels deep",
                       details={"max_depth": collab.MAX_TREE_DEPTH})
    if parent.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED):
        raise InvalidStateTransition(parent.status.value, "subtasks")
    batch = uuid.uuid4()
    children = []
    for child in request.tasks:
        if child.namespace is None:
            child = child.model_copy(update={"namespace": parent.namespace})
        children.append(await _create_task_core(
            session, principal, child, agent=agent, batch=batch, commit=False, parent=parent))
    parent.lead = request.lead or parent.lead or f"agent:{agent}"
    parent.children_done = False
    parent.version += 1
    parent.updated_at = utcnow()
    await session.commit()
    return {"parent": task_out(parent).model_dump(mode="json"),
            "children": [task_summary(child).model_dump(mode="json") for child in children]}


async def get_task_tree(
    session: AsyncSession, principal: Principal, task_key: str, *, depth: int,
    include: list[str],
) -> dict[str, Any]:
    require_permission(principal, "task:read")
    task = await _get_task(session, principal.workspace.id, task_key)
    ensure_namespace(principal, task.namespace)
    return await collab.task_tree(session, principal, task, depth=min(max(depth, 1), 4),
                                  include=include)


async def create_escalation(
    session: AsyncSession, principal: Principal, request: Any
) -> dict[str, Any]:
    """A question to a human that blocks no task (AG-CO-05)."""
    require_permission(principal, "task:create")
    agent = effective_agent(principal, None)
    await automation.screen_escalation(session, principal, topic=request.topic,
                                       task_key=None, question=request.question)
    result = await collab.escalate(session, principal, None, agent, request)
    await session.commit()
    return result


async def list_escalations(
    session: AsyncSession, principal: Principal, *, status: str | None
) -> dict[str, Any]:
    require_permission(principal, "task:read")
    rows = (
        await session.execute(
            text("SELECT e.*, t.task_key AS task_key FROM escalations e LEFT JOIN "
                 "collab_tasks t ON t.id = e.task_id WHERE e.workspace_id = :ws AND "
                 "(CAST(:status AS text) IS NULL OR e.status = :status) "
                 "ORDER BY e.created_at DESC LIMIT 200"),
            {"ws": principal.workspace.id, "status": status},
        )
    ).mappings().all()
    items = []
    for row in rows:
        item = {key: value for key, value in row.items()
                if key not in ("workspace_id", "tenant_id", "task_id", "escalation_task_id",
                               "raised_by_token")}
        for key in ("due_at", "resolved_at", "last_reminded_at", "created_at"):
            if item.get(key) is not None:
                item[key] = item[key].isoformat()
        items.append(item)
    return {"items": items}


async def resolve_escalation(
    session: AsyncSession, principal: Principal, escalation_id: str, request: Any
) -> dict[str, Any]:
    return await collab.resolve(session, principal, escalation_id, option_id=request.option_id,
                                answer=request.answer, note=request.note)
