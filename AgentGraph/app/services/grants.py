"""CommanderGrant: owner-issued, scoped, expiring authority (role rules v0.2).

A COMMANDER agent never writes on the strength of its role: the request names
a grant in ``X-Commander-Grant-Id`` and every dimension of the grant (grantee,
action, namespace, task keys, kind, expiry, revocation) is checked before the
write proceeds. Anything else is a 403.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthKind, Principal, RoleScope, require_capability
from app.errors import ApiError
from app.models import CollabTask, CommanderGrant, utcnow
from app.schemas import CommanderGrantCreateRequest
from app.services.common import record_audit

COMMANDER_ACTIONS = frozenset({"task:cancel", "task:review", "task:archive"})


def _out(grant: CommanderGrant) -> dict[str, Any]:
    return {
        "grant_id": grant.grant_id,
        "issued_by": grant.issued_by,
        "grantee": grant.grantee,
        "allowed_actions": list(grant.allowed_actions or []),
        "scope_namespace": grant.scope_namespace,
        "scope_task_keys": list(grant.scope_task_keys) if grant.scope_task_keys else None,
        "scope_task_kind": grant.scope_task_kind,
        "reason": grant.reason,
        "expires_at": grant.expires_at,
        "revoked_at": grant.revoked_at,
        "revoked_by": grant.revoked_by,
        "created_at": grant.created_at,
    }


async def _audit(session: AsyncSession, principal: Principal, grant: CommanderGrant,
                 operation: str, after: dict[str, Any]) -> None:
    def plain(value: Any) -> Any:
        if isinstance(value, uuid.UUID):
            return str(value)
        return value.isoformat() if hasattr(value, "isoformat") else value

    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=uuid.uuid4(),
        agent_signature=principal.agent_signature,
        target_table="commander_grants",
        target_record_id=grant.grant_id,
        operation=operation,
        before={},
        after={key: plain(value) for key, value in after.items()},
    )


async def issue_grant(
    session: AsyncSession, principal: Principal, request: CommanderGrantCreateRequest
) -> dict[str, Any]:
    """Issue a grant. Only the human OWNER (or the operator console) may."""
    require_capability(principal, "grant:issue")
    actions = sorted(set(request.allowed_actions))
    unknown = set(actions) - COMMANDER_ACTIONS
    if unknown:
        raise ApiError(422, "validation_error",
                       f"unsupported commander action(s): {', '.join(sorted(unknown))}",
                       details={"field": "allowed_actions"})
    grant = CommanderGrant(
        workspace_id=principal.workspace.id,
        tenant_id=principal.tenant_id,
        issued_by=f"user:{principal.user_id or principal.agent_signature}",
        grantee=request.grantee,
        allowed_actions=actions,
        scope_namespace=request.scope_namespace,
        scope_task_keys=request.scope_task_keys,
        scope_task_kind=request.scope_task_kind,
        reason=request.reason,
        expires_at=utcnow() + timedelta(seconds=request.expires_in_seconds),
    )
    session.add(grant)
    await session.flush()
    await _audit(session, principal, grant, "COMMANDER_GRANT_ISSUED", _out(grant))
    await session.commit()
    return _out(grant)


async def list_grants(session: AsyncSession, principal: Principal) -> list[dict[str, Any]]:
    if not principal.is_human:
        raise ApiError(403, "human_endpoint", "commander grants are read by people")
    rows = (await session.execute(
        select(CommanderGrant)
        .where(CommanderGrant.workspace_id == principal.workspace.id)
        .order_by(CommanderGrant.created_at.desc())
        .limit(200)
    )).scalars().all()
    return [_out(row) for row in rows]


async def revoke_grant(
    session: AsyncSession, principal: Principal, grant_id: str
) -> dict[str, Any]:
    require_capability(principal, "grant:issue")
    grant = await _load(session, principal, grant_id)
    if grant.revoked_at is None:
        grant.revoked_at = utcnow()
        grant.revoked_by = f"user:{principal.user_id or principal.agent_signature}"
        await session.flush()
        await _audit(session, principal, grant, "COMMANDER_GRANT_REVOKED", _out(grant))
    await session.commit()
    return _out(grant)


async def _load(session: AsyncSession, principal: Principal, grant_id: str) -> CommanderGrant:
    try:
        parsed = uuid.UUID(str(grant_id))
    except (ValueError, AttributeError, TypeError):
        raise ApiError(403, "commander_grant_invalid",
                       "X-Commander-Grant-Id is not a grant id") from None
    grant = await session.get(CommanderGrant, parsed)
    if grant is None or grant.workspace_id != principal.workspace.id:
        raise ApiError(403, "commander_grant_invalid", "no such grant in this workspace")
    return grant


def requires_grant(principal: Principal) -> bool:
    """Agent COMMANDERs only; people and the workspace key are not proxies."""
    return principal.auth_kind is AuthKind.AGENT and principal.role is RoleScope.COMMANDER


async def require_grant(
    session: AsyncSession,
    principal: Principal,
    grant_id: str | None,
    action: str,
    task: CollabTask,
) -> CommanderGrant:
    """Validate the named grant against the action and the target task."""
    if not (grant_id or "").strip():
        raise ApiError(403, "commander_grant_required",
                       "a COMMANDER write must name X-Commander-Grant-Id",
                       details={"action": action, "task_key": task.task_key})
    grant = await _load(session, principal, grant_id.strip())
    if grant.revoked_at is not None:
        raise ApiError(403, "commander_grant_invalid", "the grant was revoked",
                       details={"grant_id": str(grant.grant_id)})
    if grant.expires_at <= utcnow():
        raise ApiError(403, "commander_grant_expired", "the grant expired",
                       details={"grant_id": str(grant.grant_id)})
    identities = {f"agent:{principal.agent_signature}"}
    if principal.agent_id:
        identities.add(f"agent:{principal.agent_id}")
    if grant.grantee not in identities:
        raise ApiError(403, "commander_grant_invalid", "the grant belongs to another agent",
                       details={"grant_id": str(grant.grant_id)})
    if action not in (grant.allowed_actions or []):
        raise ApiError(403, "commander_grant_out_of_scope",
                       f"the grant does not allow {action}",
                       details={"grant_id": str(grant.grant_id), "action": action})
    if grant.scope_namespace not in ("*", task.namespace):
        raise ApiError(403, "commander_grant_out_of_scope",
                       "the task is outside the grant's namespace",
                       details={"grant_id": str(grant.grant_id),
                                "namespace": task.namespace})
    if grant.scope_task_keys and task.task_key not in grant.scope_task_keys:
        raise ApiError(403, "commander_grant_out_of_scope",
                       "the task is outside the grant's task keys",
                       details={"grant_id": str(grant.grant_id),
                                "task_key": task.task_key})
    if grant.scope_task_kind and task.kind != grant.scope_task_kind:
        raise ApiError(403, "commander_grant_out_of_scope",
                       "the task kind is outside the grant",
                       details={"grant_id": str(grant.grant_id), "kind": task.kind})
    return grant
