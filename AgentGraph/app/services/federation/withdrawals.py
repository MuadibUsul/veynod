"""Skill withdrawal and notices (P3-4).

Contract: docs/federation/ICD_P3_REGISTRY.md §11 (vision §11.4, F-023).

A version is withdrawn either by its publisher (``registry.yank``) or by the
platform's operators (``revoke``, e.g. for a malicious skill). Either way:

* the version and its digest stay on record and can still be looked up by
  digest; it cannot be newly installed or depended on;
* every workspace that has it pinned, directly or as a dependency, gets a
  notice saying what was withdrawn, why, by whom, and what it can do (the
  newest installable version, or the version it pinned before);
* every publisher whose own published skill depends on it gets a notice too;
* each notice is also a workspace event (``timeline.skill_withdrawn``), so
  agents and the console learn of it without polling.

Existing pins are not removed: the work that relies on them does not stop
without a person deciding. ``registry.use`` returns a warning with the content.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text

from app.database import async_session_factory
from app.services.federation import registry
from app.services.federation.errors import FederationError
from app.services.federation.grants import Actor

EVENT_TYPE = "SKILL_WITHDRAWN"  # becomes timeline.skill_withdrawn


async def _version_by_digest(digest: str) -> dict[str, Any]:
    async with async_session_factory() as platform:
        return await registry._version_row(platform, "", digest=digest)


async def _alternative(name: str) -> str | None:
    """The newest version of a package that can still be installed."""
    async with async_session_factory() as platform:
        return await platform.scalar(
            text(
                "SELECT version FROM registry_versions WHERE name = :n AND state = 'published' "
                "ORDER BY major DESC, minor DESC, patch DESC LIMIT 1"
            ),
            {"n": name},
        )


async def _previous_pin(workspace_id: str, name: str, current: str) -> str | None:
    """The last version this workspace pinned before, if it is still installable."""
    async with async_session_factory() as platform:
        return await platform.scalar(
            text(
                "SELECT h.version FROM registry_install_history h JOIN registry_versions v "
                "ON v.name = h.name AND v.version = h.version WHERE h.workspace_id = "
                "CAST(:w AS uuid) AND h.name = :n AND h.version <> :c AND v.state = 'published' "
                "ORDER BY h.id DESC LIMIT 1"
            ),
            {"w": str(workspace_id), "n": name, "c": current},
        )


async def notify(digest: str) -> int:
    """Write a notice (and an event) for every workspace affected by the
    withdrawal of this digest. Idempotent. Returns how many were written."""
    row = await _version_by_digest(digest)
    if row["state"] != "yanked":
        return 0
    source = row.get("yank_source") or "publisher"
    name, version, reason = row["name"], row["version"], row["yank_reason"] or ""
    newest = await _alternative(name)
    async with async_session_factory() as platform:
        pinned = (
            (
                await platform.execute(
                    text(
                        "SELECT workspace_id, as_dependency_of FROM registry_installs "
                        "WHERE digest = :d"
                    ),
                    {"d": digest},
                )
            )
            .mappings()
            .all()
        )
        dependants = (
            (
                await platform.execute(
                    text(
                        "SELECT DISTINCT v.name, v.version, m.workspace_id "
                        "FROM registry_versions v "
                        "CROSS JOIN LATERAL jsonb_array_elements(v.dependencies) dep "
                        "JOIN registry_packages p ON p.name = v.name "
                        "JOIN federation_local_map m ON m.federation_id = p.publisher_fid "
                        "AND m.id_type = 'workspace' "
                        "WHERE dep->>'digest' = :d AND v.state = 'published'"
                    ),
                    {"d": digest},
                )
            )
            .mappings()
            .all()
        )
    notices: list[tuple[str, str, dict[str, Any]]] = []
    for pin in pinned:
        workspace_id = str(pin["workspace_id"])
        notices.append(
            (
                workspace_id,
                "withdrawn",
                {
                    "pinned": True,
                    "as_dependency_of": pin["as_dependency_of"],
                    "update_to": newest,
                    "roll_back_to": await _previous_pin(workspace_id, name, version),
                },
            )
        )
    by_publisher: dict[str, list[dict[str, str]]] = {}
    for dep in dependants:
        by_publisher.setdefault(str(dep["workspace_id"]), []).append(
            {"name": dep["name"], "version": dep["version"]}
        )
    for workspace_id, skills in by_publisher.items():
        notices.append(
            (
                workspace_id,
                "dependency_withdrawn",
                {"your_skills": skills, "update_to": newest},
            )
        )
    if source == "platform":
        # The publisher learns that the platform withdrew its version.
        async with async_session_factory() as platform:
            publisher_ws = await platform.scalar(
                text(
                    "SELECT m.workspace_id FROM registry_packages p JOIN federation_local_map m "
                    "ON m.federation_id = p.publisher_fid AND m.id_type = 'workspace' "
                    "WHERE p.name = :n"
                ),
                {"n": name},
            )
        if publisher_ws is not None and str(publisher_ws) not in {n[0] for n in notices}:
            notices.append((str(publisher_ws), "withdrawn", {"publisher": True}))
    written = 0
    for workspace_id, kind, affected in notices:
        written += await _write(workspace_id, kind, source, row, reason, affected)
    return written


async def _write(
    workspace_id: str,
    kind: str,
    source: str,
    row: dict[str, Any],
    reason: str,
    affected: dict[str, Any],
) -> int:
    from app.services.common import append_event

    async with async_session_factory() as platform:
        notice_id = await platform.scalar(
            text(
                "INSERT INTO registry_notices (workspace_id, tenant_id, kind, source, name, "
                "version, digest, reason, affected) VALUES (CAST(:w AS uuid), '', :k, :s, :n, "
                ":v, :d, :r, CAST(:a AS jsonb)) ON CONFLICT (workspace_id, digest, kind, source) "
                "DO NOTHING RETURNING id"
            ),
            {
                "w": workspace_id,
                "k": kind,
                "s": source,
                "n": row["name"],
                "v": row["version"],
                "d": row["digest"],
                "r": reason,
                "a": json.dumps(affected),
            },
        )
        if notice_id is None:
            return 0
        await append_event(
            platform,
            workspace_id=uuid.UUID(workspace_id),
            agent="registry",
            event_type=EVENT_TYPE,
            platform="federation",
            payload={
                "id": str(notice_id),
                "details": {
                    "notice_id": notice_id,
                    "kind": kind,
                    "source": source,
                    "name": row["name"],
                    "version": row["version"],
                    "digest": row["digest"],
                    "reason": reason,
                    **affected,
                },
            },
        )
        await platform.commit()
    return 1


async def revoke(name: str, version: str, *, reason: str, operator: str) -> dict[str, Any]:
    """The platform withdraws a version (e.g. found malicious after publication).
    Works whether or not the publisher already yanked it."""
    if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 1000:
        raise registry._invalid("a revocation needs a reason")
    if not isinstance(operator, str) or not operator.strip():
        raise registry._invalid("name the operator")
    async with async_session_factory() as platform:
        row = await registry._version_row(platform, name, version)
        if row.get("yank_source") != "platform":
            await platform.execute(
                text(
                    "UPDATE registry_versions SET state = 'yanked', yanked_at = "
                    "COALESCE(yanked_at, NOW()), yanked_by = :by, yank_reason = :r, "
                    "yank_source = 'platform' WHERE name = :n AND version = :v"
                ),
                {
                    "by": f"operator:{operator.strip()}",
                    "r": reason.strip(),
                    "n": name,
                    "v": version,
                },
            )
            await platform.commit()
    await notify(row["digest"])
    return await registry.get_version(name, version)


async def lookup(digest: str) -> dict[str, Any]:
    """What a digest is, even long after it was withdrawn (F-023)."""
    row = await _version_by_digest(digest)
    out = registry._version_out(row)
    out.pop("manifest", None)
    return out


def _notice_out(row: dict[str, Any]) -> dict[str, Any]:
    affected = row["affected"]
    return {
        "id": row["id"],
        "kind": row["kind"],
        "source": row["source"],
        "name": row["name"],
        "version": row["version"],
        "digest": row["digest"],
        "reason": row["reason"],
        "affected": json.loads(affected) if isinstance(affected, str) else affected,
        "created_at": row["created_at"].isoformat(),
        "acknowledged_at": row["acknowledged_at"].isoformat() if row["acknowledged_at"] else None,
        "acknowledged_by": row["acknowledged_by"],
    }


async def notices(workspace_id: str, *, open_only: bool = False) -> list[dict[str, Any]]:
    where = " AND acknowledged_at IS NULL" if open_only else ""
    async with async_session_factory() as platform:
        rows = (
            (
                await platform.execute(
                    text(
                        "SELECT * FROM registry_notices WHERE workspace_id = CAST(:w AS uuid)"
                        f"{where} ORDER BY created_at DESC, id DESC LIMIT 200"
                    ),
                    {"w": str(workspace_id)},
                )
            )
            .mappings()
            .all()
        )
    return [_notice_out(dict(r)) for r in rows]


async def acknowledge(workspace_id: str, notice_id: int, *, actor: Actor) -> dict[str, Any]:
    """A person marks a notice as seen. It stays in the list."""
    if actor.kind != "user":
        raise FederationError("FED_E_APPROVAL_NOT_HUMAN", "a person acknowledges a notice")
    async with async_session_factory() as platform:
        row = (
            (
                await platform.execute(
                    text(
                        "UPDATE registry_notices SET acknowledged_at = COALESCE(acknowledged_at, "
                        "NOW()), acknowledged_by = COALESCE(acknowledged_by, :by) WHERE id = :id "
                        "AND workspace_id = CAST(:w AS uuid) RETURNING *"
                    ),
                    {"by": actor.ref, "id": int(notice_id), "w": str(workspace_id)},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise FederationError("FED_E_NOTICE_UNKNOWN", "no such notice in this workspace")
        await platform.commit()
    return _notice_out(dict(row))
