"""Deploy gate v2: every project can deploy its merged images, if it wants to.

A project turns deploys on with a **target**: the computer on its server that
runs deploys (it dials out over nerve and advertises ``deploy`` in its
capabilities report; Veynod holds no SSH key and opens no port), the branch
whose merges deploy, and the trigger:

* ``auto``      every release on the branch deploys as soon as it arrives;
* ``scheduled`` the newest release deploys inside the target's window;
* ``manual``    only a person starts a run.

CI posts a **release** per merged commit: the image digest of every service
(``contracts.deploy_plan``). A **run** moves one target to one release. The gate:

1. only releases on the target's branch deploy (CI posts them after the merge,
   from the branch itself, with the target's release token);
2. monotonic: a release never deploys over a newer one, except a rollback a
   person asks for;
3. one run in flight per target, and at most one waiting: a newer release
   replaces the waiting one instead of queueing behind it (coalesce);
4. incremental: the run's plan restarts only the services whose digest changed;
   a release that changes nothing is recorded as deployed without a run.

A run is delivered as a runner placement with step ``deploy``: the same nerve
delivery and receipts as any other step. The deploy computer reports
``running`` then ``deployed``, ``rolled_back`` or ``failed``; a failed or
rolled-back run waits under "needs you".
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, require_permission
from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError, Forbidden, NotFound
from app.services.common import append_event
from app.services.runners import ONLINE_WINDOW, runner_outdated
from contracts.deploy_plan import DeployConfigError, plan, validate_manifest

logger = logging.getLogger(__name__)
settings = get_settings()

TRIGGERS = ("auto", "scheduled", "manual")
IN_FLIGHT = ("offered", "running")
ENDED = ("deployed", "rolled_back", "failed", "skipped")
REPORTED = ("running", "deployed", "rolled_back", "failed")
TOKEN_PREFIX = "vdr_"
_LOCK_SQL = "SELECT pg_try_advisory_lock(hashtext('veynod_deploys')::bigint)"
_UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtext('veynod_deploys')::bigint)"
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


# ------------------------------------------------------------------ schedule


def clean_schedule(value: Any) -> dict[str, Any] | None:
    """{"days": [1..7] (ISO weekday), "start": "HH:MM", "end": "HH:MM", "tz": "UTC"}."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ApiError(422, "validation_error", "schedule must be an object",
                       details={"field": "schedule"})
    days = value.get("days") or [1, 2, 3, 4, 5, 6, 7]
    start, end = str(value.get("start") or ""), str(value.get("end") or "")
    tz = str(value.get("tz") or "UTC")
    if (not isinstance(days, list) or not days
            or not all(isinstance(day, int) and 1 <= day <= 7 for day in days)):
        raise ApiError(422, "validation_error", "schedule.days are ISO weekdays 1..7",
                       details={"field": "schedule.days"})
    if not _HHMM.match(start) or not _HHMM.match(end) or start == end:
        raise ApiError(422, "validation_error",
                       "schedule.start and schedule.end are different HH:MM times",
                       details={"field": "schedule"})
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ApiError(422, "validation_error", f"unknown time zone {tz!r}",
                       details={"field": "schedule.tz"}) from exc
    return {"days": sorted(set(days)), "start": start, "end": end, "tz": tz}


def in_window(schedule: dict[str, Any] | None, now: datetime) -> bool:
    """Whether ``now`` falls in the window. A window may wrap past midnight
    (22:00-02:00); its weekday is the day it opened on."""
    if not schedule:
        return False
    local = now.astimezone(ZoneInfo(schedule.get("tz") or "UTC"))
    hhmm = local.strftime("%H:%M")
    start, end, days = schedule["start"], schedule["end"], schedule["days"]
    if start < end:
        return local.isoweekday() in days and start <= hhmm < end
    if hhmm >= start:
        return local.isoweekday() in days
    yesterday = (local - timedelta(days=1)).isoweekday()
    return hhmm < end and yesterday in days


# ------------------------------------------------------------------ reads


async def _project(session: AsyncSession, workspace_id: Any, project: str) -> dict[str, Any]:
    row = (await session.execute(text(
        "SELECT * FROM projects WHERE workspace_id = :ws AND (name = :p OR "
        "CAST(project_id AS text) = :p)"), {"ws": workspace_id, "p": project})).mappings().first()
    if row is None:
        raise NotFound("project")
    return dict(row)


async def _target(session: AsyncSession, project_id: Any, name: str,
                  lock: bool = False) -> dict[str, Any] | None:
    row = (await session.execute(text(
        "SELECT * FROM deploy_targets WHERE project_id = :p AND name = :n"
        + (" FOR UPDATE" if lock else "")), {"p": project_id, "n": name})).mappings().first()
    return dict(row) if row else None


def _target_out(row: dict[str, Any], host: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "target_id": str(row["target_id"]),
        "name": row["name"],
        "enabled": row["enabled"],
        "branch": row["branch"],
        "trigger": row["trigger_mode"],
        "schedule": row["schedule"],
        "host_id": row["host_id"],
        "computer": host,
        "health_url": row["health_url"],
        "health_timeout_s": row["health_timeout_s"],
        "auto_rollback": row["auto_rollback"],
        "has_release_token": bool(row["release_token_hash"]),
        "deployed": {"commit": row["deployed_commit"], "at": _iso(row["deployed_at"]),
                     "release_id": row["deployed_release_id"] and str(row["deployed_release_id"]),
                     "services": row["deployed_manifest"] or {}},
        "updated_at": _iso(row["updated_at"]),
    }


def _run_out(row: dict[str, Any]) -> dict[str, Any]:
    report = row.get("report") or {}
    return {
        "run_id": str(row["run_id"]),
        "target_id": str(row["target_id"]),
        "release_id": str(row["release_id"]),
        "commit": row.get("commit_sha"),
        "seq": row.get("seq"),
        "from_commit": row["from_commit"],
        "trigger": row["trigger"],
        "status": row["status"],
        "plan": row["plan"],
        "placement_id": row["placement_id"],
        "reason": row["reason"],
        "note": report.get("note"),
        "report": report,
        "requested_by": row["requested_by"],
        "needs_you": row["status"] in ("failed", "rolled_back") and not report.get("seen"),
        "created_at": _iso(row["created_at"]),
        "started_at": _iso(row["started_at"]),
        "ended_at": _iso(row["ended_at"]),
    }


async def _host(session: AsyncSession, workspace_id: Any,
                host_id: str | None) -> dict[str, Any] | None:
    if not host_id:
        return None
    row = (await session.execute(text(
        "SELECT h.*, t.is_active AS token_active, t.revoked_at AS token_revoked_at "
        "FROM runner_hosts h JOIN agent_tokens t ON t.canonical_id = h.token_id "
        "AND t.workspace_id = h.workspace_id WHERE h.workspace_id = :ws AND h.host_id = :h"),
        {"ws": workspace_id, "h": host_id})).mappings().first()
    return dict(row) if row else None


def is_deploy_host(host: dict[str, Any]) -> bool:
    """A computer that runs deploys says so; it takes no other work."""
    report = host.get("capabilities_report") or {}
    return isinstance(report, dict) and report.get("deploy") is True


def _host_state(host: dict[str, Any] | None, now: datetime) -> str:
    if host is None:
        return "missing"
    if not host["token_active"] or host["token_revoked_at"] is not None \
            or host["status"] == "revoked":
        return "revoked"
    if host["status"] != "active":
        return host["status"]
    if not is_deploy_host(host):
        return "not_a_deploy_computer"
    if runner_outdated(host.get("runner_version")):
        return "outdated"
    if now - host["last_seen_at"] > ONLINE_WINDOW:
        return "offline"
    return "online"


def _computer(host: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    if host is None:
        return None
    return {"host_id": host["host_id"], "agent_id": host["agent_id"],
            "state": _host_state(host, now), "runner_version": host.get("runner_version"),
            "last_seen_at": _iso(host["last_seen_at"])}


async def get_project_deploys(session: AsyncSession, principal: Principal,
                              project: str, limit: int = 20) -> dict[str, Any]:
    """The project's targets, its newest releases and each target's recent runs."""
    require_permission(principal, "task:read")
    ws = principal.workspace.id
    proj = await _project(session, ws, project)
    now = _now()
    targets = []
    for row in (await session.execute(text(
            "SELECT * FROM deploy_targets WHERE project_id = :p ORDER BY name"),
            {"p": proj["project_id"]})).mappings().all():
        target = dict(row)
        out = _target_out(target, _computer(await _host(session, ws, target["host_id"]), now))
        out["runs"] = await _runs(session, target["target_id"], limit)
        targets.append(out)
    releases = (await session.execute(text(
        "SELECT release_id, seq, commit_sha, branch, built, manifest, created_at "
        "FROM deploy_releases WHERE project_id = :p ORDER BY seq DESC LIMIT :n"),
        {"p": proj["project_id"], "n": limit})).mappings().all()
    return {
        "project": proj["name"],
        "targets": targets,
        "releases": [{"release_id": str(item["release_id"]), "seq": item["seq"],
                      "commit": item["commit_sha"], "branch": item["branch"],
                      "built": item["built"], "services": sorted(item["manifest"] or {}),
                      "created_at": _iso(item["created_at"])} for item in releases],
    }


async def _runs(session: AsyncSession, target_id: Any, limit: int) -> list[dict[str, Any]]:
    rows = (await session.execute(text(
        "SELECT r.*, d.commit_sha, d.seq FROM deploy_runs r JOIN deploy_releases d "
        "ON d.release_id = r.release_id WHERE r.target_id = :t "
        "ORDER BY r.created_at DESC LIMIT :n"), {"t": target_id, "n": limit})).mappings().all()
    return [_run_out(dict(row)) for row in rows]


# ------------------------------------------------------------------ settings


def _may_configure(principal: Principal) -> None:
    """Going live is a person's decision: only people turn deploys on or start one."""
    if not principal.is_human:
        raise Forbidden("only a person configures or starts deploys")


async def put_target(session: AsyncSession, principal: Principal, project: str,
                     name: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Create or change a target. Fields left out keep their value."""
    _may_configure(principal)
    ws = principal.workspace.id
    proj = await _project(session, ws, project)
    current = await _target(session, proj["project_id"], name, lock=True)
    values: dict[str, Any] = {}
    if "enabled" in fields:
        values["enabled"] = bool(fields["enabled"])
    if "branch" in fields:
        if not _BRANCH.match(str(fields["branch"] or "")):
            raise ApiError(422, "validation_error", "branch is not a branch name",
                           details={"field": "branch"})
        values["branch"] = fields["branch"]
    if "trigger" in fields:
        if fields["trigger"] not in TRIGGERS:
            raise ApiError(422, "validation_error", "trigger is auto, scheduled or manual",
                           details={"field": "trigger"})
        values["trigger_mode"] = fields["trigger"]
    if "schedule" in fields:
        values["schedule"] = clean_schedule(fields["schedule"])
    if "host_id" in fields:
        host_id = fields["host_id"] or None
        if host_id and await _host(session, ws, host_id) is None:
            raise ApiError(422, "validation_error", "no computer with that host_id here",
                           details={"field": "host_id"})
        values["host_id"] = host_id
    if "health_url" in fields:
        url = fields["health_url"] or None
        if url and not re.match(r"^https?://\S+$", url):
            raise ApiError(422, "validation_error", "health_url is an http(s) URL",
                           details={"field": "health_url"})
        values["health_url"] = url
    if "health_timeout_s" in fields:
        values["health_timeout_s"] = int(fields["health_timeout_s"])
    if "auto_rollback" in fields:
        values["auto_rollback"] = bool(fields["auto_rollback"])
    merged = {**(current or {}), **values}
    if merged.get("trigger_mode") == "scheduled" and not merged.get("schedule"):
        raise ApiError(422, "validation_error", "a scheduled target needs a schedule window",
                       details={"field": "schedule"})
    if merged.get("enabled") and not merged.get("host_id"):
        raise ApiError(422, "validation_error",
                       "name the deploy computer (host_id) before turning deploys on",
                       details={"field": "host_id"})
    if current is None:
        cols = ["workspace_id", "project_id", "name", "created_by", *values]
        params = {"workspace_id": ws, "project_id": proj["project_id"], "name": name,
                  "created_by": principal.agent_signature, **values}
        await session.execute(text(
            f"INSERT INTO deploy_targets ({', '.join(cols)}) VALUES ("
            + ", ".join(f"CAST(:{col} AS jsonb)" if col == "schedule" else f":{col}"
                        for col in cols) + ")"),
            {**params, **({"schedule": json.dumps(values["schedule"])}
                          if "schedule" in values else {})})
    elif values:
        sets = ", ".join(f"{col} = CAST(:{col} AS jsonb)" if col == "schedule"
                         else f"{col} = :{col}" for col in values)
        await session.execute(text(
            f"UPDATE deploy_targets SET {sets}, updated_at = NOW() WHERE target_id = :id"),
            {**values, **({"schedule": json.dumps(values["schedule"])}
                          if "schedule" in values else {}), "id": current["target_id"]})
    target = await _target(session, proj["project_id"], name)
    assert target is not None
    await append_event(session, workspace_id=ws, agent=principal.agent_signature,
                       event_type="DEPLOY_TARGET_CHANGED",
                       payload={"project": proj["name"], "target": name,
                                "enabled": target["enabled"], "trigger": target["trigger_mode"],
                                "changed": sorted(values)})
    if not target["enabled"]:
        # Turning deploys off stops what has not started; a run in flight finishes.
        await session.execute(text(
            "UPDATE deploy_runs SET status = 'skipped', reason = 'target_disabled', "
            "ended_at = NOW(), updated_at = NOW() WHERE target_id = :t AND status = 'queued'"),
            {"t": target["target_id"]})
    await session.commit()
    return _target_out(target, _computer(await _host(session, ws, target["host_id"]), _now()))


async def rotate_release_token(session: AsyncSession, principal: Principal, project: str,
                               name: str) -> dict[str, Any]:
    """A new token for CI's release posts. Shown once; only its hash is kept."""
    _may_configure(principal)
    proj = await _project(session, principal.workspace.id, project)
    target = await _target(session, proj["project_id"], name, lock=True)
    if target is None:
        raise NotFound("deploy target")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    await session.execute(text(
        "UPDATE deploy_targets SET release_token_hash = :h, updated_at = NOW() "
        "WHERE target_id = :id"), {"h": _hash(token), "id": target["target_id"]})
    await session.commit()
    return {"release_token": token,
            "note": "Store it as a CI secret (VEYNOD_RELEASE_TOKEN). It is not shown again."}


# ------------------------------------------------------------------ releases


async def post_release(session: AsyncSession, token: str | None,
                       body: dict[str, Any]) -> dict[str, Any]:
    """CI says a merged commit's images are pushed. Queues auto targets."""
    if not token or not token.startswith(TOKEN_PREFIX):
        raise ApiError(401, "unauthorized", "a release needs the target's release token")
    target = (await session.execute(text(
        "SELECT * FROM deploy_targets WHERE release_token_hash = :h"),
        {"h": _hash(token)})).mappings().first()
    if target is None:
        raise ApiError(401, "unauthorized", "unknown release token")
    commit = str(body.get("commit") or "").lower()
    branch = str(body.get("branch") or "")
    if not _SHA.match(commit):
        raise ApiError(422, "validation_error", "commit is a git sha", details={"field": "commit"})
    if not _BRANCH.match(branch):
        raise ApiError(422, "validation_error", "branch is a branch name",
                       details={"field": "branch"})
    try:
        manifest = validate_manifest(body.get("manifest"))
    except DeployConfigError as exc:
        raise ApiError(422, "validation_error", str(exc), details={"field": "manifest"}) from exc
    built = [name for name in (body.get("built") or []) if isinstance(name, str)][:50]
    project_id, ws = target["project_id"], target["workspace_id"]
    # Serialise release numbering per project.
    await session.execute(text("SELECT 1 FROM projects WHERE project_id = :p FOR UPDATE"),
                          {"p": project_id})
    existing = (await session.execute(text(
        "SELECT * FROM deploy_releases WHERE project_id = :p AND branch = :b AND commit_sha = :c"),
        {"p": project_id, "b": branch, "c": commit})).mappings().first()
    if existing is None:
        release = dict((await session.execute(text(
            "INSERT INTO deploy_releases (workspace_id, project_id, seq, commit_sha, branch, "
            "manifest, built, source) VALUES (:ws, :p, (SELECT COALESCE(MAX(seq), 0) + 1 FROM "
            "deploy_releases WHERE project_id = :p), :c, :b, CAST(:m AS jsonb), "
            "CAST(:built AS jsonb), :src) RETURNING *"),
            {"ws": ws, "p": project_id, "c": commit, "b": branch, "m": json.dumps(manifest),
             "built": json.dumps(built), "src": str(body.get("source") or "")[:256] or None},
        )).mappings().one())
        await append_event(session, workspace_id=ws, agent="ci", event_type="DEPLOY_RELEASED",
                           payload={"project_id": str(project_id), "commit": commit,
                                    "branch": branch, "seq": release["seq"], "built": built})
    else:
        release = dict(existing)  # CI retried: same release, same answer
    queued = []
    for row in (await session.execute(text(
            "SELECT * FROM deploy_targets WHERE project_id = :p AND enabled AND "
            "trigger_mode = 'auto' AND branch = :b FOR UPDATE"),
            {"p": project_id, "b": branch})).mappings().all():
        outcome = await _queue(session, dict(row), release, "auto", "ci")
        queued.append({"target": row["name"], **outcome})
    await session.commit()
    if any(item.get("queued") for item in queued):
        await tick(ws)  # start at once instead of at the next loop pass
    return {"release_id": str(release["release_id"]), "seq": release["seq"], "commit": commit,
            "queued": queued}


async def _release(session: AsyncSession, project_id: Any, branch: str,
                   commit: str | None) -> dict[str, Any] | None:
    if commit:
        row = (await session.execute(text(
            "SELECT * FROM deploy_releases WHERE project_id = :p AND branch = :b "
            "AND commit_sha LIKE :c ORDER BY seq DESC LIMIT 1"),
            {"p": project_id, "b": branch, "c": commit.lower() + "%"})).mappings().first()
    else:
        row = (await session.execute(text(
            "SELECT * FROM deploy_releases WHERE project_id = :p AND branch = :b "
            "ORDER BY seq DESC LIMIT 1"), {"p": project_id, "b": branch})).mappings().first()
    return dict(row) if row else None


async def _deployed_seq(session: AsyncSession, target: dict[str, Any]) -> int:
    if not target["deployed_release_id"]:
        return 0
    return int((await session.execute(text(
        "SELECT seq FROM deploy_releases WHERE release_id = :r"),
        {"r": target["deployed_release_id"]})).scalar_one_or_none() or 0)


async def _queue(session: AsyncSession, target: dict[str, Any], release: dict[str, Any],
                 trigger: str, by: str) -> dict[str, Any]:
    """The gate. Returns what happened: queued, coalesced or refused (with why)."""
    if release["branch"] != target["branch"]:
        return {"queued": False, "reason": "wrong_branch"}
    if trigger in ("auto", "scheduled") and (await session.execute(text(
            "SELECT 1 FROM deploy_runs WHERE target_id = :t AND release_id = :r "
            "AND status IN ('failed', 'rolled_back')"),
            {"t": target["target_id"], "r": release["release_id"]})).first():
        # A release that failed here is retried by a person, never by a timer.
        return {"queued": False, "reason": "failed_before"}
    if trigger != "rollback":
        if release["seq"] <= await _deployed_seq(session, target):
            return {"queued": False, "reason": "not_newer"}
        newer_in_flight = (await session.execute(text(
            "SELECT 1 FROM deploy_runs r JOIN deploy_releases d ON d.release_id = r.release_id "
            "WHERE r.target_id = :t AND r.status IN ('offered', 'running') AND d.seq >= :s"),
            {"t": target["target_id"], "s": release["seq"]})).first()
        if newer_in_flight:
            return {"queued": False, "reason": "not_newer"}
    waiting = (await session.execute(text(
        "SELECT r.*, d.seq FROM deploy_runs r JOIN deploy_releases d "
        "ON d.release_id = r.release_id WHERE r.target_id = :t AND r.status = 'queued' "
        "FOR UPDATE OF r"),
        {"t": target["target_id"]})).mappings().first()
    if waiting is not None:
        if trigger != "rollback" and waiting["trigger"] != "rollback" \
                and waiting["seq"] >= release["seq"]:
            return {"queued": False, "reason": "newer_waiting", "run_id": str(waiting["run_id"])}
        # Coalesce: the waiting run now deploys this release instead.
        await session.execute(text(
            "UPDATE deploy_runs SET release_id = :r, trigger = :tr, requested_by = :by, "
            "reason = 'coalesced', updated_at = NOW() WHERE run_id = :id"),
            {"r": release["release_id"], "tr": trigger, "by": by, "id": waiting["run_id"]})
        return {"queued": True, "coalesced": True, "run_id": str(waiting["run_id"])}
    run_id = (await session.execute(text(
        "INSERT INTO deploy_runs (workspace_id, target_id, release_id, trigger, requested_by) "
        "VALUES (:ws, :t, :r, :tr, :by) RETURNING run_id"),
        {"ws": target["workspace_id"], "t": target["target_id"], "r": release["release_id"],
         "tr": trigger, "by": by})).scalar_one()
    return {"queued": True, "run_id": str(run_id)}


async def request_deploy(session: AsyncSession, principal: Principal, project: str, name: str,
                         commit: str | None = None) -> dict[str, Any]:
    """A person deploys the newest release, or a named older one (a rollback)."""
    _may_configure(principal)
    proj = await _project(session, principal.workspace.id, project)
    target = await _target(session, proj["project_id"], name, lock=True)
    if target is None:
        raise NotFound("deploy target")
    if not target["enabled"]:
        raise ApiError(409, "deploys_off", "deploys are off for this target; turn them on first")
    release = await _release(session, proj["project_id"], target["branch"], commit)
    if release is None:
        raise ApiError(404, "not_found", "no release on the target's branch"
                       + (f" for commit {commit}" if commit else ""))
    older = release["seq"] <= await _deployed_seq(session, target)
    if older and not commit:
        return {"queued": False, "reason": "already_deployed", "commit": release["commit_sha"]}
    outcome = await _queue(session, target, release, "rollback" if older else "manual",
                           principal.agent_signature)
    await session.commit()
    if outcome.get("queued"):
        await tick(principal.workspace.id)
    return {**outcome, "commit": release["commit_sha"], "seq": release["seq"]}


async def acknowledge(session: AsyncSession, principal: Principal, run_id: str) -> dict[str, Any]:
    """A person has seen a failed or rolled-back run: it leaves "needs you"."""
    _may_configure(principal)
    row = (await session.execute(text(
        "UPDATE deploy_runs SET report = COALESCE(report, '{}'::jsonb) || '{\"seen\": true}', "
        "updated_at = NOW() WHERE run_id = CAST(:id AS uuid) AND workspace_id = :ws RETURNING *"),
        {"id": run_id, "ws": principal.workspace.id})).mappings().first()
    if row is None:
        raise NotFound("deploy run")
    await session.commit()
    return _run_out(dict(row))


async def needs_you(session: AsyncSession, workspace_id: Any) -> list[dict[str, Any]]:
    """Failed or rolled-back runs no one has looked at yet, newest first."""
    rows = (await session.execute(text(
        "SELECT r.*, d.commit_sha, d.seq, t.name AS target, p.name AS project "
        "FROM deploy_runs r JOIN deploy_releases d ON d.release_id = r.release_id "
        "JOIN deploy_targets t ON t.target_id = r.target_id "
        "JOIN projects p ON p.project_id = t.project_id "
        "WHERE r.workspace_id = :ws AND r.status IN ('failed', 'rolled_back') "
        "AND NOT COALESCE((r.report ->> 'seen')::boolean, false) "
        "ORDER BY r.ended_at DESC NULLS LAST LIMIT 20"), {"ws": workspace_id})).mappings().all()
    return [{**_run_out(dict(row)), "project": row["project"], "target": row["target"]}
            for row in rows]


# ------------------------------------------------------------------ the loop


async def tick(workspace_id: Any | None = None) -> dict[str, int]:
    """Queue scheduled deploys, start what waits, end what timed out; then deliver."""
    from app.services import scheduler

    counts = {"scheduled": 0, "offered": 0, "skipped": 0, "waiting": 0, "timed_out": 0,
              "requeued": 0}
    now = _now()
    async with async_session_factory() as session:
        params = {"ws": str(workspace_id) if workspace_id else None}
        scope = "(CAST(:ws AS uuid) IS NULL OR t.workspace_id = CAST(:ws AS uuid))"
        await _settle_in_flight(session, scope, params, now, counts)
        for row in (await session.execute(text(
                f"SELECT t.* FROM deploy_targets t WHERE t.enabled AND "
                f"t.trigger_mode = 'scheduled' AND {scope}"), params)).mappings().all():
            target = dict(row)
            if not in_window(target["schedule"], now):
                continue
            release = await _release(session, target["project_id"], target["branch"], None)
            if release is None:
                continue
            outcome = await _queue(session, target, release, "scheduled", "schedule")
            if outcome.get("queued") and not outcome.get("coalesced"):
                counts["scheduled"] += 1
        for row in (await session.execute(text(
                "SELECT r.*, d.seq, d.commit_sha, d.manifest FROM deploy_runs r "
                "JOIN deploy_releases d ON d.release_id = r.release_id "
                f"JOIN deploy_targets t ON t.target_id = r.target_id WHERE r.status = 'queued' "
                f"AND {scope} AND NOT EXISTS (SELECT 1 FROM deploy_runs f WHERE "
                "f.target_id = r.target_id AND f.status IN ('offered', 'running')) "
                "ORDER BY r.created_at FOR UPDATE OF r SKIP LOCKED"), params)).mappings().all():
            counts[await _start(session, dict(row), now)] += 1
        await session.commit()
    if counts["offered"]:
        await scheduler.dispatch_offered(workspace_id)
    return counts


async def _settle_in_flight(session: AsyncSession, scope: str, params: dict[str, Any],
                            now: datetime, counts: dict[str, int]) -> None:
    """A placement the nerve could not deliver goes back to waiting; one that
    ran out of time fails."""
    timeout = timedelta(seconds=settings.deploy_run_timeout_seconds)
    rows = (await session.execute(text(
        "SELECT r.*, p.status AS placement_status, p.reason AS placement_reason "
        "FROM deploy_runs r JOIN deploy_targets t ON t.target_id = r.target_id "
        "LEFT JOIN runner_placements p ON p.placement_id = r.placement_id "
        f"WHERE r.status IN ('offered', 'running') AND {scope} FOR UPDATE OF r SKIP LOCKED"),
        params)).mappings().all()
    claim = timedelta(seconds=settings.runner_claim_timeout_seconds)
    for row in rows:
        run = dict(row)
        unclaimed = run["status"] == "offered" and now - run["updated_at"] > claim
        if unclaimed:
            # Never reached the computer, or it never started: try again later.
            await _end_placement(session, run["placement_id"], "moved", "not_claimed")
            run["placement_reason"] = "not_claimed"
        if run["status"] == "offered" and (
                unclaimed or run["placement_status"] in ("moved", "withdrawn", "failed")):
            waiting = (await session.execute(text(
                "SELECT 1 FROM deploy_runs WHERE target_id = :t AND status = 'queued'"),
                {"t": run["target_id"]})).first()
            if waiting:
                await _finish(session, run, "skipped", "superseded")
            else:
                await session.execute(text(
                    "UPDATE deploy_runs SET status = 'queued', placement_id = NULL, "
                    "reason = :why, updated_at = NOW() WHERE run_id = :id"),
                    {"why": (run["placement_reason"] or "not_delivered")[:120],
                     "id": run["run_id"]})
            counts["requeued"] += 1
        elif now - (run["started_at"] or run["updated_at"]) > timeout:
            await _finish(session, run, "failed", "timed_out")
            await _end_placement(session, run["placement_id"], "failed", "timed_out")
            counts["timed_out"] += 1


async def _start(session: AsyncSession, run: dict[str, Any], now: datetime) -> str:
    target = dict((await session.execute(text(
        "SELECT * FROM deploy_targets WHERE target_id = :t FOR UPDATE"),
        {"t": run["target_id"]})).mappings().one())
    if not target["enabled"]:
        await _finish(session, run, "skipped", "target_disabled")
        return "skipped"
    steps = plan(target["deployed_manifest"], run["manifest"])
    if steps.empty:
        # Nothing to pull or restart: the target is on this release already.
        await _finish(session, run, "deployed", "nothing_changed", plan_=steps.as_dict())
        await _mark_deployed(session, target, run)
        return "skipped"
    host = await _host(session, target["workspace_id"], target["host_id"])
    state = _host_state(host, now)
    if state != "online":
        reason = f"deploy_computer_{state}"
        if run["reason"] != reason:
            await session.execute(text(
                "UPDATE deploy_runs SET reason = :r, updated_at = NOW() WHERE run_id = :id"),
                {"r": reason, "id": run["run_id"]})
        return "waiting"
    assert host is not None
    previous = target["deployed_manifest"] or {}
    detail = {
        "run_id": str(run["run_id"]),
        "release": {"commit": run["commit_sha"], "seq": run["seq"], "manifest": run["manifest"]},
        "previous": {"commit": target["deployed_commit"], "manifest": previous},
        "plan": steps.as_dict(),
        "health": {"url": target["health_url"], "timeout_s": target["health_timeout_s"]},
        "auto_rollback": target["auto_rollback"],
        "trigger": run["trigger"],
    }
    placement_id = (await session.execute(text(
        "INSERT INTO runner_placements (workspace_id, host_id, agent_id, mode, status, step, "
        "task_key, detail) VALUES (:ws, :host, :agent, 'assign', 'offered', 'deploy', NULL, "
        "CAST(:d AS jsonb)) RETURNING placement_id"),
        {"ws": target["workspace_id"], "host": host["host_id"], "agent": host["agent_id"],
         "d": json.dumps(detail)})).scalar_one()
    await session.execute(text(
        "UPDATE deploy_runs SET status = 'offered', placement_id = :pl, "
        "plan = CAST(:plan AS jsonb), "
        "from_commit = :from, reason = NULL, updated_at = NOW() WHERE run_id = :id"),
        {"pl": placement_id, "plan": json.dumps(steps.as_dict()),
         "from": target["deployed_commit"], "id": run["run_id"]})
    await append_event(session, workspace_id=target["workspace_id"], agent="deploys",
                       event_type="DEPLOY_STARTED",
                       payload={"run_id": str(run["run_id"]), "target": target["name"],
                                "commit": run["commit_sha"], "changed": list(steps.changed),
                                "placement_id": placement_id, "idempotency_key": placement_id})
    return "offered"


async def _finish(session: AsyncSession, run: dict[str, Any], status: str, reason: str | None,
                  report: dict[str, Any] | None = None,
                  plan_: dict[str, Any] | None = None) -> None:
    await session.execute(text(
        "UPDATE deploy_runs SET status = :s, reason = COALESCE(:r, reason), "
        "report = COALESCE(CAST(:rep AS jsonb), report), "
        "plan = COALESCE(CAST(:plan AS jsonb), plan), "
        "ended_at = NOW(), updated_at = NOW() WHERE run_id = :id"),
        {"s": status, "r": reason, "rep": json.dumps(report) if report is not None else None,
         "plan": json.dumps(plan_) if plan_ is not None else None, "id": run["run_id"]})


async def _mark_deployed(session: AsyncSession, target: dict[str, Any],
                         run: dict[str, Any]) -> None:
    release = (await session.execute(text(
        "SELECT release_id, commit_sha, manifest FROM deploy_releases WHERE release_id = :r"),
        {"r": run["release_id"]})).mappings().one()
    await session.execute(text(
        "UPDATE deploy_targets SET deployed_release_id = :r, deployed_commit = :c, "
        "deployed_manifest = CAST(:m AS jsonb), deployed_at = NOW(), updated_at = NOW() "
        "WHERE target_id = :t"),
        {"r": release["release_id"], "c": release["commit_sha"],
         "m": json.dumps(release["manifest"]), "t": target["target_id"]})


async def _end_placement(session: AsyncSession, placement_id: str | None, status: str,
                         reason: str, note: str | None = None) -> None:
    if not placement_id:
        return
    await session.execute(text(
        "UPDATE runner_placements SET status = :s, reason = :r, note = COALESCE(:n, note), "
        "ended_at = NOW(), updated_at = NOW() WHERE placement_id = :id "
        "AND status IN ('offered', 'claimed')"),
        {"s": status, "r": reason, "n": note and note[:500], "id": placement_id})


# ------------------------------------------------------------------ reports


def _clean_report(body: dict[str, Any]) -> dict[str, Any]:
    log = body.get("log") or []
    if not isinstance(log, list):
        log = []
    services = body.get("services") or {}
    return {
        "note": str(body.get("note") or "")[:500] or None,
        "log": [str(line)[:400] for line in log[-80:]],
        "services": {str(name)[:64]: str(state)[:32]
                     for name, state in list(services.items())[:50]}
        if isinstance(services, dict) else {},
        "health": str(body.get("health") or "")[:32] or None,
    }


async def report(session: AsyncSession, principal: Principal, placement_id: str,
                 body: dict[str, Any]) -> dict[str, Any]:
    """The deploy computer says where the run is: running, then how it ended."""
    from app.services.scheduler import _own_placement

    status = body.get("status")
    if status not in REPORTED:
        raise ApiError(422, "validation_error",
                       "status is running, deployed, rolled_back or failed",
                       details={"field": "status"})
    placement = await _own_placement(session, principal, placement_id)
    if placement["step"] != "deploy":
        raise ApiError(409, "not_a_deploy", "this placement is not a deploy")
    run = (await session.execute(text(
        "SELECT * FROM deploy_runs WHERE placement_id = :p FOR UPDATE"),
        {"p": placement_id})).mappings().first()
    if run is None:
        raise NotFound("deploy run")
    run = dict(run)
    if run["status"] in ENDED:
        return _run_out(run)  # a retried report: the run already ended
    cleaned = _clean_report(body)
    if status == "running":
        await session.execute(text(
            "UPDATE deploy_runs SET status = 'running', started_at = COALESCE(started_at, NOW()), "
            "report = CAST(:rep AS jsonb), updated_at = NOW() WHERE run_id = :id"),
            {"rep": json.dumps(cleaned), "id": run["run_id"]})
        await session.execute(text(
            "UPDATE runner_placements SET status = 'claimed', updated_at = NOW() "
            "WHERE placement_id = :id AND status = 'offered'"), {"id": placement_id})
    else:
        await _finish(session, run, status, None, report=cleaned)
        await _end_placement(session, placement_id,
                             "delivered" if status == "deployed" else "failed", status,
                             cleaned["note"])
        target = dict((await session.execute(text(
            "SELECT * FROM deploy_targets WHERE target_id = :t FOR UPDATE"),
            {"t": run["target_id"]})).mappings().one())
        if status == "deployed":
            await _mark_deployed(session, target, run)
        await append_event(session, workspace_id=run["workspace_id"], agent="deploys",
                           event_type="DEPLOY_" + status.upper(),
                           payload={"run_id": str(run["run_id"]), "target": target["name"],
                                    "note": cleaned["note"]})
    await session.commit()
    row = (await session.execute(text(
        "SELECT r.*, d.commit_sha, d.seq FROM deploy_runs r JOIN deploy_releases d "
        "ON d.release_id = r.release_id WHERE r.run_id = :id"),
        {"id": run["run_id"]})).mappings().one()
    if status != "running":
        await tick(run["workspace_id"])  # a run that waited behind this one starts now
    return _run_out(dict(row))


# ------------------------------------------------------------------ loop


async def deploy_loop(interval_seconds: float | None = None) -> None:
    """Every few seconds, by one process at a time."""
    import asyncio

    interval = interval_seconds or settings.deploy_loop_interval_seconds
    while True:
        try:
            async with async_session_factory() as lock:
                if await lock.scalar(text(_LOCK_SQL)):
                    try:
                        await tick()
                    finally:
                        await lock.execute(text(_UNLOCK_SQL))
                        await lock.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - background resilience
            logger.exception("deploy pass failed")
        await asyncio.sleep(interval)
