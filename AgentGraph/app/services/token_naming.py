"""Stable agent identity, credential generations and rename references.

Display names are free-form; ``agent_directory.agent_id`` is the machine
identity; each token row is one credential generation. This module keeps those
three layers coherent without changing the token tree or permissions model.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthKind, Principal, generate_agent_key, hash_api_key
from app.errors import ApiError, Forbidden
from app.models import AgentToken, Workspace
from app.services.common import agent_token_out, append_event
from app.services.directory import _owner, _resolve, default_agent_id
from app.services.lineage import snapshot_token


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def attach_token(session: AsyncSession, workspace: Workspace, token: AgentToken, *,
                       display_name: str | None = None,
                       registered_by: str = "admin", source: str = "registered") -> None:
    """Bind a token to its stable directory identity."""
    agent_id = token.agent_id or default_agent_id(token.agent_signature)
    if token.agent_id is None:
        token.agent_id = agent_id
    row = (await session.execute(text(
        "SELECT principal_id FROM agent_directory WHERE tenant_id = :t AND agent_id = :a"),
        {"t": workspace.tenant_id, "a": agent_id})).scalar_one_or_none()
    if row is not None:
        await session.execute(text(
            "UPDATE agent_directory SET "
            "token_ids = CASE WHEN :token = ANY(token_ids) THEN token_ids "
            "ELSE array_append(token_ids, :token) END, "
            "signatures = CASE WHEN :sig = ANY(signatures) THEN signatures "
            "ELSE array_append(signatures, :sig) END, "
            "display_name = COALESCE(:name, display_name), "
            "version = version + 1, updated_at = NOW() WHERE principal_id = :p"),
            {"token": token.canonical_id, "sig": token.agent_signature,
             "name": (display_name or None) and display_name[:64], "p": row})
        return
    owner = await _owner(session, workspace.id)
    await session.execute(text(
        "INSERT INTO agent_directory (workspace_id, agent_id, display_name, role, "
        "token_ids, signatures, owner_human, registered_by, source, last_seen_at) "
        "VALUES (:ws, :agent, :name, :role, ARRAY[:token], ARRAY[:sig], :owner, :by, "
        ":source, NOW())"),
        {"ws": workspace.id, "agent": agent_id,
         "name": (display_name or token.agent_signature)[:64],
         "role": token.role_scope.value, "token": token.canonical_id,
         "sig": token.agent_signature, "owner": owner,
         "by": registered_by[:256], "source": source})


async def next_generation(session: AsyncSession, principal: Principal, ref: str,
                          display_name: str | None = None) -> dict[str, Any]:
    """Issue the next credential generation for an existing stable identity.

    The old active credentials are revoked, the agent_id does not move, and all
    old signatures stay in the directory so existing assignee/mention refs keep
    resolving.
    """
    entry = await _resolve(session, principal.workspace, ref)
    if principal.is_human:
        if principal.auth_kind is AuthKind.HUMAN and principal.human_role not in ("OWNER", "ADMIN"):
            raise Forbidden("only an owner or admin can issue a new generation")
    elif principal.token_cid not in (entry.get("token_ids") or []):
        raise Forbidden("an agent may only issue a new generation of itself")

    tokens = (await session.execute(select(AgentToken).where(
        AgentToken.workspace_id == principal.workspace.id,
        AgentToken.canonical_id.in_(entry.get("token_ids") or []),
    ))).scalars().all()
    if not tokens:
        raise ApiError(409, "no_active_generation", "this identity has no token to rotate",
                       details={"agent_id": entry["agent_id"]})
    active = [token for token in tokens if token.is_active and token.revoked_at is None]
    current = max(active or tokens, key=lambda token: (
        int(token.generation or 1), token.created_at))
    generation = max(int(token.generation or 1) for token in tokens) + 1
    signature = f"{entry['agent_id']}-v{generation}"
    while await session.scalar(select(AgentToken.id).where(
            AgentToken.workspace_id == principal.workspace.id,
            AgentToken.agent_signature == signature)):
        generation += 1
        signature = f"{entry['agent_id']}-v{generation}"

    raw_key = generate_agent_key()
    token = AgentToken(
        workspace_id=principal.workspace.id,
        agent_signature=signature,
        agent_id=entry["agent_id"],
        generation=generation,
        api_key_hash=hash_api_key(raw_key),
        role_scope=current.role_scope,
        allowed_namespaces=list(current.allowed_namespaces or ["*"]),
        read_namespaces=(list(current.read_namespaces)
                         if current.read_namespaces is not None else None),
        write_namespaces=(list(current.write_namespaces)
                          if current.write_namespaces is not None else None),
        scopes=(list(current.scopes) if current.scopes is not None else None),
        can_issue=bool(current.can_issue),
        max_descendants=int(current.max_descendants or 0),
        expires_at=current.expires_at,
        parent_token_id=current.parent_token_id,
        root_token_id=current.root_token_id or principal.workspace.root_token_id,
        depth=current.depth or 1,
        purpose=current.purpose,
        issued_by_agent_id=(principal.agent_signature if not principal.is_human else "human"),
        issued_by_token_id=(None if principal.is_human else principal.token_cid),
        issued_by_human=principal.user_id,
        per_minute_limit=current.per_minute_limit,
        monthly_token_budget=current.monthly_token_budget,
        scope_add=list(current.scope_add or []),
        scope_remove=list(current.scope_remove or []),
        agent_profile={**(current.agent_profile or {}),
                       "agent_id": entry["agent_id"],
                       "display_name": (display_name or entry["display_name"])[:64]},
    )
    session.add(token)
    await session.flush()
    lineage = list(current.lineage or [principal.workspace.root_token_id])
    token.lineage = [*lineage[:-1], token.canonical_id]
    now = _now()
    for old in active:
        old.is_active = False
        old.revoked_at = now
        old.revoke_reason = f"superseded by generation v{generation}"

    await snapshot_token(session, principal.workspace, token)
    await attach_token(session, principal.workspace, token, display_name=display_name,
                       registered_by=(principal.token_cid or "human"), source="registered")
    await append_event(
        session, workspace_id=principal.workspace.id, agent="directory",
        event_type="AGENT_GENERATION",
        payload={"agent_id": entry["agent_id"], "generation": generation,
                 "token_id": token.canonical_id,
                 "previous_token_ids": [old.canonical_id for old in active],
                 "targets": [f"agent:{entry['agent_id']}"]},
    )
    await session.commit()
    return {"token": agent_token_out(token), "api_key": raw_key,
            "agent_id": entry["agent_id"], "generation": generation,
            "previous_token_ids": [old.canonical_id for old in active]}


async def generations(session: AsyncSession, principal: Principal, ref: str) -> dict[str, Any]:
    """The credential-generation ledger of one stable identity (R-05).

    Every token row is one generation; the directory entry keeps them together,
    so a rotation or a revocation is auditable without reading the token list.
    """
    entry = await _resolve(session, principal.workspace, ref)
    tokens = (await session.execute(select(AgentToken).where(
        AgentToken.workspace_id == principal.workspace.id,
        AgentToken.canonical_id.in_(entry.get("token_ids") or []),
    ).order_by(AgentToken.generation, AgentToken.created_at))).scalars().all()
    now = _now()

    def status(token: AgentToken) -> str:
        if token.revoked_at is not None:
            return "revoked"
        if not token.is_active:
            return "inactive"
        if token.expires_at is not None and token.expires_at <= now:
            return "expired"
        return "active"

    active = [token for token in tokens if status(token) == "active"]
    current = max(active, key=lambda token: int(token.generation or 1), default=None)
    items = [{
        "generation": int(token.generation or 1),
        "signature": token.agent_signature,
        "token_id": token.canonical_id,
        "status": status(token),
        "is_current": current is not None and token.id == current.id,
        "created_at": token.created_at.isoformat() if token.created_at else None,
        "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
        "revoke_reason": token.revoke_reason,
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
    } for token in tokens]
    return {
        "agent_id": entry["agent_id"],
        "principal_id": str(entry["principal_id"]),
        "display_name": entry.get("display_name"),
        "status": "revoked" if not active and tokens else "active",
        "items": items,
    }


async def references(session: AsyncSession, principal: Principal, ref: str) -> dict[str, Any]:
    """Every machine reference to an identity; used before a rename (T-3)."""
    entry = await _resolve(session, principal.workspace, ref)
    if not principal.is_human and principal.token_cid not in (entry.get("token_ids") or []):
        raise Forbidden("an agent may only inspect its own references")
    labels = list(dict.fromkeys([
        entry["agent_id"], entry["principal_id"],
        *(entry.get("signatures") or []), *(entry.get("legacy_names") or []),
    ]))
    refs = [f"agent:{label}" for label in labels]
    tasks = (await session.execute(text(
        "SELECT task_key, status, assignee, lead, mentions FROM collab_tasks "
        "WHERE workspace_id = :ws AND (assignee = ANY(CAST(:refs AS text[])) "
        "OR lead = ANY(CAST(:refs AS text[])) "
        "OR jsonb_exists_any(mentions, CAST(:refs AS text[]))) "
        "ORDER BY updated_at DESC LIMIT 200"),
        {"ws": principal.workspace.id, "refs": refs})).mappings().all()
    patterns = list(dict.fromkeys(
        pattern for label in labels
        for pattern in (f"%agent:{label.lower()}%", f"%@{label.lower()}%")
    ))
    documents = (await session.execute(text(
        "SELECT doc_key, title FROM shared_documents WHERE workspace_id = :ws "
        "AND lower(content) LIKE ANY(CAST(:patterns AS text[])) "
        "ORDER BY doc_key LIMIT 200"),
        {"ws": principal.workspace.id, "patterns": patterns})).mappings().all()
    return {
        "agent_id": entry["agent_id"],
        "refs": refs,
        "tasks": [dict(row) for row in tasks],
        "documents": [dict(row) for row in documents],
        "counts": {"tasks": len(tasks), "documents": len(documents),
                   "mentions": sum(len(row["mentions"] or []) for row in tasks)},
    }


async def migrate_rename(session: AsyncSession, workspace: Workspace,
                         row: dict[str, Any], updated: dict[str, Any]) -> dict[str, Any]:
    """Move task/mention refs to the new agent_id and report document hits."""
    labels = list(dict.fromkeys([
        row["agent_id"], row["principal_id"],
        *(row.get("signatures") or []), *(row.get("legacy_names") or []),
    ]))
    old_refs = [f"agent:{label}" for label in labels]
    new_ref = f"agent:{updated['agent_id']}"
    assignee = await session.execute(text(
        "UPDATE collab_tasks SET assignee = :new, version = version + 1, "
        "updated_at = NOW() WHERE workspace_id = :ws "
        "AND assignee = ANY(CAST(:old AS text[]))"),
        {"new": new_ref, "ws": workspace.id, "old": old_refs})
    lead = await session.execute(text(
        "UPDATE collab_tasks SET lead = :new, version = version + 1, "
        "updated_at = NOW() WHERE workspace_id = :ws "
        "AND lead = ANY(CAST(:old AS text[]))"),
        {"new": new_ref, "ws": workspace.id, "old": old_refs})
    mention_rows = (await session.execute(text(
        "SELECT id, mentions FROM collab_tasks WHERE workspace_id = :ws "
        "AND jsonb_exists_any(mentions, CAST(:old AS text[]))"),
        {"ws": workspace.id, "old": old_refs})).mappings().all()
    mention_count = 0
    for task in mention_rows:
        replaced = [new_ref if item in old_refs else item for item in (task["mentions"] or [])]
        if replaced != (task["mentions"] or []):
            await session.execute(text(
                "UPDATE collab_tasks SET mentions = CAST(:m AS jsonb), "
                "version = version + 1, updated_at = NOW() WHERE id = :id"),
                {"m": json.dumps(replaced), "id": task["id"]})
            mention_count += 1
    patterns = list(dict.fromkeys(
        pattern for label in labels
        for pattern in (f"%agent:{label.lower()}%", f"%@{label.lower()}%")
    ))
    documents = (await session.execute(text(
        "SELECT doc_key, title FROM shared_documents WHERE workspace_id = :ws "
        "AND lower(content) LIKE ANY(CAST(:patterns AS text[])) "
        "ORDER BY doc_key LIMIT 200"),
        {"ws": workspace.id, "patterns": patterns})).mappings().all()
    return {"assignee_updated": assignee.rowcount or 0,
            "lead_updated": lead.rowcount or 0,
            "mentions_updated": mention_count,
            "documents": [dict(row) for row in documents],
            "document_count": len(documents)}
