"""Runner hosts: the computers that run agents (full-managed Loop, step 2).

A runner is the program on a VPS, a laptop, a Raspberry Pi or the Windows
client that runs agents through their local runtimes (Claude Code, Codex). It
only connects out: ``hello`` once when it starts, ``heartbeat`` every 30 s.
It reports what the machine can do so the scheduler can pick a host for a
task. One runner token is one host. Nothing secret is kept: values are taken
from a fixed list of fields, anything that looks like an email or a key is
dropped, and the hostname is stored only as a digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthKind, Principal
from app.errors import ApiError, Forbidden, NotFound
from app.services.common import append_event

ONLINE_WINDOW = timedelta(seconds=90)
HEARTBEAT_EVERY_S = 30
OS_NAMES = {"linux": "linux", "darwin": "darwin", "macos": "darwin", "mac": "darwin",
            "win32": "windows", "windows": "windows", "freebsd": "freebsd"}
ARCH_NAMES = {"x64": "x64", "x86_64": "x64", "amd64": "x64", "arm64": "arm64",
              "aarch64": "arm64", "arm": "arm", "armv7l": "arm", "ia32": "ia32"}
REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
RUNTIME_ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
# Values a runner must never send us; the field is dropped, not stored.
SECRETISH = re.compile(
    r"@|\b(sk|pk|rk)-[A-Za-z0-9_-]{8,}|\bgh[pousr]_[A-Za-z0-9]{8,}|\bxox[abp]-|"
    r"\bvey_[A-Za-z0-9]|\bag_[A-Za-z0-9]{8,}|\bey[A-Za-z0-9_-]{20,}\.|-----BEGIN",
    re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_str(value: Any, limit: int) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    value = str(value).strip()[:limit]
    if not value or SECRETISH.search(value):
        return None
    return value


def _clean_runtimes(given: Any) -> list[dict[str, Any]]:
    runtimes: list[dict[str, Any]] = []
    for item in (given if isinstance(given, list) else [])[:8]:
        if not isinstance(item, dict):
            continue
        runtime_id = _clean_str(item.get("id"), 32)
        if not runtime_id or not RUNTIME_ID.fullmatch(runtime_id.lower()):
            continue
        entry: dict[str, Any] = {"id": runtime_id.lower(),
                                 "available": item.get("available") is not False}
        version = _clean_str(item.get("version"), 32)
        if version:
            entry["version"] = version
        models = [name for name in (_clean_str(model if not isinstance(model, dict)
                                               else model.get("id"), 64)
                                    for model in (item.get("models") or [])[:20]) if name]
        if models:
            entry["models"] = models
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else None
        if usage:
            windows = []
            for window in (usage.get("windows") or [])[:4]:
                if not isinstance(window, dict):
                    continue
                name = _clean_str(window.get("name"), 32)
                try:
                    used = float(window.get("used_percent"))
                except (TypeError, ValueError):
                    used = None
                if name is None or used is None:
                    continue
                cleaned = {"name": name, "used_percent": max(0.0, min(100.0, used))}
                resets = _clean_str(window.get("resets_at"), 40)
                if resets:
                    cleaned["resets_at"] = resets
                windows.append(cleaned)
            plan = _clean_str(usage.get("plan"), 32)
            entry["usage"] = {**({"plan": plan} if plan else {}), "windows": windows}
        runtimes.append(entry)
    return runtimes


def _clean_host(given: Any) -> dict[str, Any]:
    if not isinstance(given, dict):
        raise ApiError(422, "validation_error", "host must be an object",
                       details={"field": "host"})
    os_name = OS_NAMES.get(str(given.get("os") or "").strip().lower())
    arch = ARCH_NAMES.get(str(given.get("arch") or "").strip().lower())
    if os_name is None:
        os_name = "other"
    if arch is None:
        arch = "other"
    hostname = str(given.get("hostname") or "").strip()
    try:
        concurrency = int(given.get("max_concurrency") or 1)
    except (TypeError, ValueError):
        concurrency = 1
    repos = [repo for repo in (str(item).strip() for item in (given.get("repos") or [])[:50])
             if REPO.fullmatch(repo)]
    return {
        "os": os_name,
        "arch": arch,
        "hostname_digest": (hashlib.sha256(hostname.lower().encode()).hexdigest()[:12]
                            if hostname else None),
        "runner_version": _clean_str(given.get("runner_version"), 32),
        "runtimes": _clean_runtimes(given.get("runtimes")),
        "max_concurrency": max(1, min(64, concurrency)),
        "repos": list(dict.fromkeys(repos)),
    }


def _state(row: dict[str, Any], now: datetime) -> str:
    if row["status"] != "active":
        return row["status"]
    seen = row["last_seen_at"]
    return "online" if seen and now - seen <= ONLINE_WINDOW else "offline"


def host_out(row: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    running = list(row["running"] or [])
    return {
        "host_id": row["host_id"],
        "permanent_address": f"host:{row['host_id']}",
        "agent_id": row["agent_id"],
        "token_id": row["token_id"],
        "os": row["os"],
        "arch": row["arch"],
        "hostname_digest": row["hostname_digest"],
        "runner_version": row["runner_version"],
        "runtimes": row["runtimes"] or [],
        "max_concurrency": row["max_concurrency"],
        "running": running,
        "free_slots": max(0, row["max_concurrency"] - len(running)),
        "repos": list(row["repos"] or []),
        "status": row["status"],
        "state": _state(row, now),
        "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None,
    }


def _runner_only(principal: Principal) -> None:
    if principal.auth_kind is not AuthKind.AGENT or not principal.token_cid:
        raise Forbidden("a runner says hello with its own agent token")


async def _presence(principal: Principal, session: AsyncSession) -> None:
    from app.services import digest, presence

    await presence.heartbeat(principal, await digest.owner_ref(session, principal))


async def hello(session: AsyncSession, principal: Principal,
                body: dict[str, Any]) -> dict[str, Any]:
    """A runner connects: create or refresh its host record."""
    _runner_only(principal)
    host = _clean_host(body.get("host"))
    current = (await session.execute(text(
        "SELECT status FROM runner_hosts WHERE workspace_id = :ws AND token_id = :t"),
        {"ws": principal.workspace.id, "t": principal.token_cid})).scalar_one_or_none()
    if current == "revoked":
        raise Forbidden("this host was revoked; ask the owner for a new runner token")
    row = (await session.execute(text(
        "INSERT INTO runner_hosts (workspace_id, token_id, agent_id, os, arch, hostname_digest, "
        "runner_version, runtimes, max_concurrency, repos) VALUES (:ws, :t, :agent, :os, :arch, "
        ":digest, :version, CAST(:runtimes AS jsonb), :conc, CAST(:repos AS text[])) "
        "ON CONFLICT (workspace_id, token_id) DO UPDATE SET agent_id = EXCLUDED.agent_id, "
        "os = EXCLUDED.os, arch = EXCLUDED.arch, hostname_digest = EXCLUDED.hostname_digest, "
        "runner_version = EXCLUDED.runner_version, runtimes = EXCLUDED.runtimes, "
        "max_concurrency = EXCLUDED.max_concurrency, repos = EXCLUDED.repos, "
        "last_seen_at = NOW(), updated_at = NOW() RETURNING *, (xmax = 0) AS created"),
        {"ws": principal.workspace.id, "t": principal.token_cid,
         "agent": principal.agent_id, "os": host["os"], "arch": host["arch"],
         "digest": host["hostname_digest"], "version": host["runner_version"],
         "runtimes": json.dumps(host["runtimes"]), "conc": host["max_concurrency"],
         "repos": host["repos"]})).mappings().one()
    if row["created"]:
        await append_event(
            session, workspace_id=principal.workspace.id, agent="runners",
            event_type="HOST_CONNECTED",
            payload={"host_id": row["host_id"], "agent_id": row["agent_id"],
                     "os": row["os"], "arch": row["arch"],
                     "runtimes": [item["id"] for item in host["runtimes"]]})
    await session.commit()
    await _presence(principal, session)
    return {**host_out(dict(row)), "created": bool(row["created"]),
            "heartbeat_every_s": HEARTBEAT_EVERY_S,
            "offline_after_s": int(ONLINE_WINDOW.total_seconds())}


async def heartbeat(session: AsyncSession, principal: Principal,
                    body: dict[str, Any]) -> dict[str, Any]:
    """Every 30 s: still here, what is running, what the quota looks like now."""
    _runner_only(principal)
    sets = ["last_seen_at = NOW()", "updated_at = NOW()"]
    params: dict[str, Any] = {"ws": principal.workspace.id, "t": principal.token_cid}
    if body.get("running") is not None:
        params["running"] = [str(key)[:128] for key in (body.get("running") or [])[:64]]
        sets.append("running = CAST(:running AS text[])")
    if body.get("runtimes") is not None:
        params["runtimes"] = json.dumps(_clean_runtimes(body.get("runtimes")))
        sets.append("runtimes = CAST(:runtimes AS jsonb)")
    if body.get("max_concurrency") is not None:
        try:
            params["conc"] = max(1, min(64, int(body["max_concurrency"])))
        except (TypeError, ValueError):
            raise ApiError(422, "validation_error", "max_concurrency must be a number",
                           details={"field": "max_concurrency"}) from None
        sets.append("max_concurrency = :conc")
    row = (await session.execute(text(
        f"UPDATE runner_hosts SET {', '.join(sets)} WHERE workspace_id = :ws "
        "AND token_id = :t AND status <> 'revoked' RETURNING *"), params)).mappings().one_or_none()
    if row is None:
        raise ApiError(409, "runner_hello_required",
                       "this runner has not said hello yet; POST /v1/runner/hello first",
                       details={"next": "POST /v1/runner/hello"})
    await session.commit()
    await _presence(principal, session)
    return {**host_out(dict(row)), "heartbeat_every_s": HEARTBEAT_EVERY_S}


async def _find(session: AsyncSession, workspace_id: Any, ref: str) -> dict[str, Any]:
    ref = ref.removeprefix("host:")
    row = (await session.execute(text(
        "SELECT * FROM runner_hosts WHERE workspace_id = :ws AND "
        "(host_id = :r OR token_id = :r OR agent_id = :r) "
        "ORDER BY (host_id = :r) DESC, last_seen_at DESC LIMIT 1"),
        {"ws": workspace_id, "r": ref})).mappings().one_or_none()
    if row is None:
        raise NotFound("runner_host")
    return dict(row)


async def list_hosts(session: AsyncSession, principal: Principal, *,
                     include_revoked: bool = False) -> dict[str, Any]:
    rows = (await session.execute(text(
        "SELECT * FROM runner_hosts WHERE workspace_id = :ws "
        + ("" if include_revoked else "AND status <> 'revoked' ")
        + "ORDER BY last_seen_at DESC LIMIT 200"),
        {"ws": principal.workspace.id})).mappings().all()
    now = _now()
    items = [host_out(dict(row), now) for row in rows]
    return {"items": items,
            "counts": {state: sum(1 for item in items if item["state"] == state)
                       for state in ("online", "offline", "paused", "revoked")}}


async def get_host(session: AsyncSession, principal: Principal, ref: str) -> dict[str, Any]:
    return host_out(await _find(session, principal.workspace.id, ref))


def _owner_only(principal: Principal) -> None:
    if not principal.is_human or (principal.auth_kind is AuthKind.HUMAN
                                  and principal.human_role not in ("OWNER", "ADMIN")):
        raise Forbidden("only the workspace owner or an admin can revoke a host")


async def revoke_host(session: AsyncSession, principal: Principal, ref: str,
                      reason: str | None = None) -> dict[str, Any]:
    """Take a computer off the Loop: its token stops working, its tasks go back."""
    _owner_only(principal)
    row = await _find(session, principal.workspace.id, ref)
    if row["status"] == "revoked":
        return {**host_out(row), "released_leases": 0}
    by = f"human:{principal.user_id}" if principal.user_id else "admin"
    await session.execute(text(
        "UPDATE runner_hosts SET status = 'revoked', revoked_by = :by, revoked_at = NOW(), "
        "running = '{}', updated_at = NOW() WHERE host_id = :id"),
        {"by": by, "id": row["host_id"]})
    await append_event(
        session, workspace_id=principal.workspace.id, agent="runners",
        event_type="HOST_REVOKED",
        payload={"host_id": row["host_id"], "agent_id": row["agent_id"], "by": by})
    from app.services import token_tree

    revoked = await token_tree.revoke_token(
        session, principal, row["token_id"],
        reason=(reason or "host revoked by the owner").strip()[:500])
    fresh = await _find(session, principal.workspace.id, row["host_id"])
    return {**host_out(fresh), "released_leases": revoked["released_leases"]}
