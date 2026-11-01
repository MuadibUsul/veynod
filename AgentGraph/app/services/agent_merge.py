"""Merge duplicate directory identities into one stable agent_id.

Operational cleanup: source identities become aliases, their tokens move to the
target, task/mention references are rewritten, and all but one active credential
are revoked. Documents are reported, never rewritten silently.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthKind, principal_for_admin
from app.errors import Forbidden
from app.models import AgentToken, Workspace
from app.services.common import append_event
from app.services.directory import _resolve
from app.services.token_tree import revoke_token


def _labels(entry: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys([
        entry["agent_id"], entry["principal_id"],
        *(entry.get("signatures") or []), *(entry.get("legacy_names") or []),
    ]))


async def _migrate_refs(session: AsyncSession, workspace: Workspace,
                        old_refs: list[str], new_ref: str) -> dict[str, Any]:
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
    labels = [ref.removeprefix("agent:") for ref in old_refs]
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


async def merge_identities(session: AsyncSession, workspace: Workspace,
                           target_ref: str, source_refs: list[str], *,
                           dry_run: bool = True,
                           principal: Any | None = None) -> dict[str, Any]:
    target = await _resolve(session, workspace, target_ref)
    if principal is not None:
        if not principal.is_human:
            raise Forbidden("only an owner or admin can merge agent identities")
        if (principal.auth_kind is AuthKind.HUMAN
                and principal.human_role not in ("OWNER", "ADMIN")):
            raise Forbidden("only an owner or admin can merge agent identities")
    sources: list[dict[str, Any]] = []
    for ref in source_refs:
        entry = await _resolve(session, workspace, ref)
        if entry["principal_id"] != target["principal_id"]:
            sources.append(entry)
    if not sources:
        raise ValueError("no distinct source identities to merge")

    entries = [target, *sources]
    token_ids = list(dict.fromkeys(
        token for entry in entries for token in (entry.get("token_ids") or [])
    ))
    tokens = (await session.execute(select(AgentToken).where(
        AgentToken.workspace_id == workspace.id,
        AgentToken.canonical_id.in_(token_ids),
    ))).scalars().all() if token_ids else []
    active = [token for token in tokens if token.is_active and token.revoked_at is None]
    keep = max(active or tokens, key=lambda token: (
        int(token.generation or 1), token.created_at)) if tokens else None
    old_refs = list(dict.fromkeys(
        f"agent:{label}" for entry in entries for label in _labels(entry)
    ))
    new_ref = f"agent:{target['agent_id']}"
    report: dict[str, Any] = {
        "target": target["agent_id"],
        "sources": [entry["agent_id"] for entry in sources],
        "tokens": len(tokens),
        "active_tokens": [token.agent_signature for token in active],
        "keep_token": keep.agent_signature if keep else None,
        "revoke_tokens": [token.agent_signature for token in active if token is not keep],
    }
    if dry_run:
        return report

    for source in sources:
        await session.execute(text(
            "INSERT INTO agent_aliases (workspace_id, alias, principal_id) "
            "VALUES (:ws, :alias, :p) ON CONFLICT DO NOTHING"),
            {"ws": workspace.id, "alias": source["agent_id"],
             "p": target["principal_id"]})
    if token_ids:
        await session.execute(text(
            "UPDATE agent_tokens SET agent_id = :target WHERE workspace_id = :ws "
            "AND canonical_id = ANY(CAST(:ids AS text[]))"),
            {"target": target["agent_id"], "ws": workspace.id, "ids": token_ids})
    signatures = list(dict.fromkeys(
        signature for entry in entries for signature in (entry.get("signatures") or [])
    ))
    legacy = list(dict.fromkeys(
        name for entry in entries for name in (entry.get("legacy_names") or [])
        + [entry["agent_id"]]
    ))
    status = "active" if keep is not None and keep.is_active and keep.revoked_at is None \
        else target["status"]
    await session.execute(text(
        "UPDATE agent_directory SET token_ids = CAST(:tokens AS text[]), "
        "signatures = CAST(:sigs AS text[]), legacy_names = CAST(:legacy AS text[]), "
        "status = :status, version = version + 1, updated_at = NOW() "
        "WHERE principal_id = :p"),
        {"tokens": token_ids, "sigs": signatures, "legacy": legacy,
         "status": status, "p": target["principal_id"]})
    await session.execute(text(
        "DELETE FROM agent_directory WHERE workspace_id = :ws "
        "AND principal_id = ANY(CAST(:ids AS text[]))"),
        {"ws": workspace.id, "ids": [entry["principal_id"] for entry in sources]})
    migration = await _migrate_refs(session, workspace, old_refs, new_ref)
    await append_event(
        session, workspace_id=workspace.id, agent="directory",
        event_type="AGENT_MERGED",
        payload={"agent_id": target["agent_id"],
                 "sources": [entry["agent_id"] for entry in sources],
                 "tokens": len(tokens), "keep_token": keep.agent_signature if keep else None,
                 "targets": [f"agent:{target['agent_id']}"]},
    )
    await session.commit()
    report.update(migration)

    revoked: list[str] = []
    actor = principal or principal_for_admin(workspace)
    for token in active:
        if token is keep:
            continue
        result = await revoke_token(
            session, actor, token.canonical_id,
            reason=f"merged into {target['agent_id']}; duplicate identity",
        )
        revoked.extend(result["revoked"])
    report["revoked"] = revoked
    return report
