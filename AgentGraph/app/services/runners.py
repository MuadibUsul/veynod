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


def _clean_lease(item: Any) -> dict[str, Any] | None:
    """Public worktree/lease fields only. A lease token is never stored."""
    if not isinstance(item, dict):
        return None
    task_key = _clean_str(item.get("task_key"), 128)
    if not task_key:
        return None
    entry: dict[str, Any] = {"task_key": task_key}
    for field, limit in (("kernel_task_id", 64), ("worktree", 32), ("lease", 32), ("gate", 32)):
        value = _clean_str(item.get(field), limit)
        if value:
            entry[field] = value
    symbols = []
    for symbol in (item.get("write_set") or [])[:8]:
        cleaned = _clean_str(symbol, 128)
        if cleaned:
            symbols.append(cleaned)
    if symbols:
        entry["write_set"] = symbols
    return entry


def _clean_report(given: Any) -> dict[str, Any] | None:
    """The runner's own account of how it runs sessions; anything else is dropped."""
    if not isinstance(given, dict):
        return None
    pinning = given.get("mcp_pinning")
    leases = [lease for lease in (_clean_lease(item) for item in (given.get("leases") or [])[:8])
              if lease]
    report: dict[str, Any] = {
        "mcp_pinning": pinning if isinstance(pinning, bool) else None,
        "runtime": _clean_str(given.get("runtime"), 32),
    }
    if leases:
        report["leases"] = leases
    return report


def kernel_card(hosts: list[dict[str, Any]]) -> dict[str, Any]:
    """What the Computers card shows: worktree leases (delivery is always the Kernel)."""
    leases: list[dict[str, Any]] = []
    for host in hosts:
        report = host.get("capabilities_report") or {}
        if not isinstance(report, dict):
            continue
        for item in report.get("leases") or []:
            cleaned = _clean_lease(item)
            if cleaned:
                leases.append(cleaned)
    first = leases[0] if leases else {}
    return {
        "delivery": "kernel",
        "worktree": first.get("worktree") or ("leased" if leases else "idle"),
        "lease": first.get("lease") or ("held" if leases else "none"),
        "gate": first.get("gate"),
        "leases": leases,
    }


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
        "capabilities_report": _clean_report(given.get("capabilities_report")),
    }


#: From this CLI release on, work reaches a computer only over its nerve
#: socket. An older CLI still says hello and heartbeats, so it would look
#: online and be offered work it can never receive.
MIN_RUNNER_VERSION = (0, 6, 0)


def runner_outdated(version: str | None) -> bool:
    """Whether a runner predates nerve-only dispatch. Unknown is not outdated."""
    if not version:
        return False
    numbers = [int(part) for part in re.findall(r"\d+", version)[:3]]
    if not numbers:
        return False
    return tuple(numbers + [0] * (3 - len(numbers))) < MIN_RUNNER_VERSION


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
        "outdated": runner_outdated(row["runner_version"]),
        "runtimes": row["runtimes"] or [],
        "max_concurrency": row["max_concurrency"],
        "running": running,
        "free_slots": max(0, row["max_concurrency"] - len(running)),
        "repos": list(row["repos"] or []),
        # None: a runner too old to say whether its sessions use its own key.
        "capabilities_report": row.get("capabilities_report"),
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
        "runner_version, runtimes, max_concurrency, repos, capabilities_report) "
        "VALUES (:ws, :t, :agent, :os, :arch, :digest, :version, CAST(:runtimes AS jsonb), "
        ":conc, CAST(:repos AS text[]), CAST(:report AS jsonb)) "
        "ON CONFLICT (workspace_id, token_id) DO UPDATE SET agent_id = EXCLUDED.agent_id, "
        "os = EXCLUDED.os, arch = EXCLUDED.arch, hostname_digest = EXCLUDED.hostname_digest, "
        "runner_version = EXCLUDED.runner_version, runtimes = EXCLUDED.runtimes, "
        "max_concurrency = EXCLUDED.max_concurrency, repos = EXCLUDED.repos, "
        "capabilities_report = EXCLUDED.capabilities_report, "
        "last_seen_at = NOW(), updated_at = NOW() RETURNING *, (xmax = 0) AS created"),
        {"ws": principal.workspace.id, "t": principal.token_cid,
         "agent": principal.agent_id, "os": host["os"], "arch": host["arch"],
         "digest": host["hostname_digest"], "version": host["runner_version"],
         "runtimes": json.dumps(host["runtimes"]), "conc": host["max_concurrency"],
         "repos": host["repos"],
         "report": (json.dumps(host["capabilities_report"])
                    if host["capabilities_report"] is not None else None)})).mappings().one()
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
    if body.get("capabilities_report") is not None:
        report = _clean_report(body.get("capabilities_report"))
        params["report"] = json.dumps(report) if report is not None else None
        sets.append("capabilities_report = CAST(:report AS jsonb)")
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


def _computer_name(seats: list[dict[str, Any]]) -> str:
    """The computer is named after its own identity, not the reviewer beside it."""
    for seat in seats:
        if seat["seat"] == "work" and seat["agent_id"]:
            return seat["agent_id"]
    name = next((seat["agent_id"] for seat in seats if seat["agent_id"]), None)
    if not name:
        return seats[0]["host_id"]
    return name[: -len("-review")] if name.endswith("-review") else name


def computers_out(items: list[dict[str, Any]], roles: dict[str, str]) -> list[dict[str, Any]]:
    """Group host keys into computers.

    ``veynod up`` mints the computer's identity and, to review on the same
    machine, a ``-review`` one; each key says hello on its own. They are two
    seats at one workstation, not two computers: keys from the same machine
    share the hostname digest. A key without one stands alone. ``roles`` maps
    token ids to role scopes.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        key = f"h:{item['hostname_digest']}" if item["hostname_digest"] else f"k:{item['host_id']}"
        groups.setdefault(key, []).append(item)
    order = {"online": 0, "paused": 1, "offline": 2, "revoked": 3}
    out = []
    for hosts in groups.values():
        seats = [{
            "host_id": host["host_id"],
            "agent_id": host["agent_id"],
            "token_id": host["token_id"],
            "role": roles.get(host["token_id"]),
            "seat": "review" if roles.get(host["token_id"]) == "AUDITOR" else "work",
            "state": host["state"],
            "status": host["status"],
            "running": host["running"],
            "max_concurrency": host["max_concurrency"],
            "free_slots": host["free_slots"],
            "last_seen_at": host["last_seen_at"],
        } for host in hosts]
        seats.sort(key=lambda seat: (seat["seat"] != "work", seat["agent_id"] or ""))
        # What the computer can run now: a seat taken off the Loop no longer counts.
        live = [host for host in hosts if host["status"] != "revoked"] or hosts
        if any(seat["seat"] == "work" and seat["state"] == "online" for seat in seats):
            # The Kernel gate reviews code and `veynod up` no longer starts a
            # -review seat. A review seat that is gone or silent while the work
            # seat runs is a leftover of an older setup, not something to reconnect.
            seats = [seat for seat in seats if seat["seat"] != "review"
                     or seat["state"] not in ("revoked", "offline")]
        runtimes: dict[str, dict[str, Any]] = {}
        for host in live:
            for runtime in host["runtimes"] or []:
                known = runtimes.get(runtime["id"])
                if known is None or (runtime.get("available") and not known.get("available")):
                    runtimes[runtime["id"]] = runtime
        first = next(host for host in hosts if host["host_id"] == seats[0]["host_id"])
        states = [seat["state"] for seat in seats]
        out.append({
            "computer_id": seats[0]["host_id"],
            "name": _computer_name(seats),
            "os": first["os"],
            "arch": first["arch"],
            "runner_version": first["runner_version"],
            "outdated": any(host.get("outdated") for host in live),
            "runtimes": list(runtimes.values()),
            "repos": list(dict.fromkeys(repo for host in hosts for repo in host["repos"])),
            # Online while any seat is; paused only when every live seat is.
            "state": min(states, key=lambda state: order.get(state, 9)),
            "max_concurrency": sum(seat["max_concurrency"] for seat in seats
                                   if seat["status"] != "revoked"),
            "free_slots": sum(seat["free_slots"] for seat in seats
                              if seat["state"] == "online"),
            "running": [key for seat in seats for key in seat["running"]],
            "last_seen_at": max((seat["last_seen_at"] or "" for seat in seats), default="") or None,
            "seats": seats,
            "kernel": kernel_card(live),
        })
    out.sort(key=lambda computer: (order.get(computer["state"], 9), computer["name"]))
    return out


async def _roles(session: AsyncSession, workspace_id: Any, token_ids: list[str]) -> dict[str, str]:
    if not token_ids:
        return {}
    rows = (await session.execute(text(
        "SELECT canonical_id, role_scope::text AS role FROM agent_tokens "
        "WHERE workspace_id = :ws AND canonical_id = ANY(CAST(:ids AS text[]))"),
        {"ws": workspace_id, "ids": token_ids})).mappings().all()
    return {row["canonical_id"]: row["role"] for row in rows}


async def _attach_task_leases(session: AsyncSession, workspace_id: Any,
                              computers: list[dict[str, Any]]) -> None:
    """Prefer the product task's Kernel record over the runner's last hello."""
    keys = [key for computer in computers for key in (computer.get("running") or [])]
    if not keys:
        return
    rows = (await session.execute(text(
        "SELECT task_key, correlation FROM collab_tasks WHERE workspace_id = :ws "
        "AND task_key = ANY(CAST(:keys AS text[]))"),
        {"ws": workspace_id, "keys": keys})).mappings().all()
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        correlation = row["correlation"] if isinstance(row["correlation"], dict) else {}
        delivery = correlation.get("kernel_delivery") if isinstance(correlation, dict) else None
        if not isinstance(delivery, dict):
            continue
        by_key[row["task_key"]] = {
            "task_key": row["task_key"],
            "kernel_task_id": delivery.get("kernel_task_id"),
            "worktree": delivery.get("worktree") or "unknown",
            "lease": delivery.get("lease") or "unknown",
            "gate": delivery.get("gate"),
            "write_set": list(delivery.get("write_set") or []),
        }
    for computer in computers:
        found = [by_key[key] for key in computer.get("running") or [] if key in by_key]
        if not found:
            continue
        kernel = dict(computer.get("kernel") or {})
        kernel["leases"] = found
        kernel["worktree"] = found[0]["worktree"]
        kernel["lease"] = found[0]["lease"]
        kernel["gate"] = found[0].get("gate")
        kernel["delivery"] = kernel.get("delivery") or "kernel"
        computer["kernel"] = kernel


async def _host_rows(session: AsyncSession, workspace_id: Any) -> list[dict[str, Any]]:
    """Host rows with their key's state folded in, the way the scheduler sees them.

    A key revoked or switched off elsewhere (the Agents page, a rotation) leaves
    ``runner_hosts.status`` active; without this the card said "offline" while
    the scheduler already treated the seat as removed.
    """
    rows = (await session.execute(text(
        "SELECT h.*, (t.canonical_id IS NULL OR NOT t.is_active OR t.revoked_at IS NOT NULL) "
        "AS key_dead FROM runner_hosts h LEFT JOIN agent_tokens t "
        "ON t.canonical_id = h.token_id AND t.workspace_id = h.workspace_id "
        "WHERE h.workspace_id = :ws ORDER BY h.last_seen_at DESC LIMIT 200"),
        {"ws": workspace_id})).mappings().all()
    out = []
    for row in rows:
        host = dict(row)
        if host.pop("key_dead", False):
            host["status"] = "revoked"
        out.append(host)
    return out


async def computers(session: AsyncSession, workspace_id: Any) -> list[dict[str, Any]]:
    """The workspace's computers (revoked ones left out), each with its seats."""
    rows = [row for row in await _host_rows(session, workspace_id)
            if row["status"] != "revoked"]
    now = _now()
    items = [host_out(row, now) for row in rows]
    grouped = computers_out(items, await _roles(
        session, workspace_id, [item["token_id"] for item in items]))
    await _attach_task_leases(session, workspace_id, grouped)
    return grouped


async def list_hosts(session: AsyncSession, principal: Principal, *,
                     include_revoked: bool = False) -> dict[str, Any]:
    rows = await _host_rows(session, principal.workspace.id)
    now = _now()
    every = [host_out(row, now) for row in rows]
    items = [item for item in every if include_revoked or item["status"] != "revoked"]
    grouped = computers_out(every, await _roles(
        session, principal.workspace.id, [item["token_id"] for item in every]))
    # A computer still on the Loop keeps its removed seat, so the console can say
    # what it no longer does; one whose every seat is removed is listed on request.
    grouped = [computer for computer in grouped
               if include_revoked or computer["state"] != "revoked"]
    await _attach_task_leases(session, principal.workspace.id, grouped)
    return {"items": items,
            "counts": {state: sum(1 for item in items if item["state"] == state)
                       for state in ("online", "offline", "paused", "revoked")},
            # The same keys, one entry per machine with its seats.
            "computers": grouped,
            "computer_counts": {state: sum(1 for item in grouped if item["state"] == state)
                                for state in ("online", "offline", "paused", "revoked")}}


async def get_host(session: AsyncSession, principal: Principal, ref: str) -> dict[str, Any]:
    return host_out(await _find(session, principal.workspace.id, ref))


def _owner_only(principal: Principal) -> None:
    if not principal.is_human or (principal.auth_kind is AuthKind.HUMAN
                                  and principal.human_role not in ("OWNER", "ADMIN")):
        raise Forbidden("only the workspace owner or an admin can pause or revoke a host")


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


async def set_paused(session: AsyncSession, principal: Principal, ref: str,
                     paused: bool) -> dict[str, Any]:
    """Owner: stop giving a host work (its tasks move on), or let it work again."""
    _owner_only(principal)
    row = await _find(session, principal.workspace.id, ref)
    if row["status"] == "revoked":
        raise ApiError(409, "host_revoked", "a revoked host cannot be paused or resumed",
                       details={"host_id": row["host_id"]})
    wanted = "paused" if paused else "active"
    if row["status"] != wanted:
        by = f"human:{principal.user_id}" if principal.user_id else "admin"
        await session.execute(text(
            "UPDATE runner_hosts SET status = :status, updated_at = NOW() WHERE host_id = :id"),
            {"status": wanted, "id": row["host_id"]})
        await append_event(
            session, workspace_id=principal.workspace.id, agent="runners",
            event_type="HOST_PAUSED" if paused else "HOST_RESUMED",
            payload={"host_id": row["host_id"], "agent_id": row["agent_id"], "by": by,
                     "targets": [f"agent:{row['agent_id']}"] if row["agent_id"] else []})
        await session.commit()
    return host_out(await _find(session, principal.workspace.id, row["host_id"]))
