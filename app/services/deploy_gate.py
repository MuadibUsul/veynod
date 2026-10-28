"""VPS deploy gate (agentgraph/spec-vps-auto-deploy-gate v1, schema 3.54).

The gate deploys merged ``main`` commits to the production VPS without GitHub
Actions. Three things feed it:

* a signed GitHub webhook (``push`` to the branch or a merged pull request),
  received here so the queue is durable and every trigger is audited;
* the runner on the VPS (``scripts/deploy_gate.py``), whose systemd timer
  polls ``origin/<branch>`` as a fallback and asks to queue a new commit;
* the operator, who can trigger, pause, resume, authorize or revoke from the
  console.

The runner claims queued runs, checks them (G-01..G-04), deploys with
``scripts/deploy_tlines.sh`` and reports back. This module only stores and
hands out that state: it never runs commands, and it never changes a task,
a trust state or a token (R-05). The tables are operator-only (forced RLS
with no application-role policy), so every session here is unscoped.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_factory
from app.errors import ApiError, NotFound
from app.ids import new_id
from app.models import CollabTask
from app.services.quality import gate_status

settings = get_settings()

TERMINAL = ("skipped", "blocked", "deployed", "rolled_back", "failed")
RUNNER_STATUSES = ("running", *TERMINAL)
CHECK_IDS = ("G-01", "G-02", "G-03", "G-04", "G-05")
# A run the runner claimed and never finished (it died mid-run) is failed
# after this long so the queue moves on.
STALE_RUN_AFTER = timedelta(hours=3)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_TEXT_MAX = 300
_LIST_MAX = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _clip(value: Any, limit: int = _TEXT_MAX) -> str:
    return str(value or "").strip()[:limit]


def _lines(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [line for line in (_clip(item) for item in value[:_LIST_MAX]) if line]


def _sha(value: Any) -> str | None:
    sha = str(value or "").strip().lower()
    return sha if _SHA.fullmatch(sha) else None


# ------------------------------------------------------------------ state


async def _audit(session: AsyncSession, actor: str, action: str,
                 run_id: str | None = None, **details: Any) -> None:
    await session.execute(text(
        "INSERT INTO deploy_gate_audit (actor, action, run_id, details) "
        "VALUES (:actor, :action, :run, CAST(:details AS jsonb))"),
        {"actor": actor[:128], "action": action, "run": run_id,
         "details": _json(details)})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


async def _state_row(session: AsyncSession, *, lock: bool = False) -> dict[str, Any]:
    row = (await session.execute(text(
        "SELECT * FROM deploy_gate_state WHERE id = 1" + (" FOR UPDATE" if lock else "")
    ))).mappings().one_or_none()
    if row is None:  # the migration seeds it; recreate if someone removed it
        await session.execute(text(
            "INSERT INTO deploy_gate_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING"))
        row = (await session.execute(text(
            "SELECT * FROM deploy_gate_state WHERE id = 1"))).mappings().one()
    return dict(row)


def state_out(row: dict[str, Any]) -> dict[str, Any]:
    seen = row.get("runner_last_seen_at")
    online = bool(seen and _now() - seen
                  <= timedelta(seconds=settings.deploy_gate_runner_offline_seconds))
    return {
        "configured": bool(settings.deploy_gate_runner_token),
        "webhook_configured": bool(settings.deploy_gate_webhook_secret),
        "repo": settings.deploy_gate_repo,
        "branch": settings.deploy_gate_branch,
        "paused": row["paused"],
        "paused_reason": row.get("paused_reason"),
        "auto_deploy_authorized": row["auto_deploy_authorized"],
        "authorized_by": row.get("authorized_by"),
        "authorized_at": _iso(row.get("authorized_at")),
        "deployed_commit": row.get("deployed_commit"),
        "deployed_at": _iso(row.get("deployed_at")),
        "runner": {
            "status": "online" if online else "offline",
            "host": row.get("runner_host"),
            "version": row.get("runner_version"),
            "last_seen_at": _iso(seen),
        },
        "updated_by": row.get("updated_by"),
        "updated_at": _iso(row.get("updated_at")),
    }


def run_out(row: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "created_at": _iso(row["created_at"]),
        "trigger_kind": row["trigger_kind"],
        "triggered_by": row["triggered_by"],
        "commit_sha": row.get("commit_sha"),
        "pr_number": row.get("pr_number"),
        "status": row["status"],
        "status_reason": row.get("status_reason"),
        "rolled_back": row["rolled_back"],
        "report": row.get("report") or {},
        "failed_checks": [check.get("id") for check in (row.get("checks") or [])
                          if isinstance(check, dict) and check.get("ok") is False],
        "finished_at": _iso(row.get("finished_at")),
    }
    if detail:
        out.update(checks=row.get("checks") or [], deploy=row.get("deploy") or {},
                   log_ref=row.get("log_ref"), claimed_by=row.get("claimed_by"),
                   claimed_at=_iso(row.get("claimed_at")),
                   delivery_id=row.get("delivery_id"))
    return out


async def overview(limit: int = 20) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    async with async_session_factory() as session:
        state = await _state_row(session)
        runs = (await session.execute(text(
            "SELECT * FROM deploy_gate_runs ORDER BY created_at DESC LIMIT :n"),
            {"n": limit})).mappings().all()
        queued = await session.scalar(text(
            "SELECT count(*) FROM deploy_gate_runs WHERE status = 'queued'"))
        await session.commit()
    return {"state": state_out(state), "queued": int(queued or 0),
            "runs": [run_out(dict(row)) for row in runs]}


async def get_run(run_id: str) -> dict[str, Any]:
    async with async_session_factory() as session:
        row = (await session.execute(text(
            "SELECT * FROM deploy_gate_runs WHERE id = :id"), {"id": run_id}
        )).mappings().one_or_none()
        if row is None:
            raise NotFound("deploy_gate_run", f"run {run_id} not found")
        audit = (await session.execute(text(
            "SELECT at, actor, action, details FROM deploy_gate_audit WHERE run_id = :id "
            "ORDER BY id"), {"id": run_id})).mappings().all()
    out = run_out(dict(row), detail=True)
    out["audit"] = [{"at": _iso(item["at"]), "actor": item["actor"], "action": item["action"],
                     "details": item["details"]} for item in audit]
    return out


async def list_audit(limit: int = 50, before: int | None = None) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    async with async_session_factory() as session:
        rows = (await session.execute(text(
            "SELECT * FROM deploy_gate_audit WHERE (CAST(:before AS bigint) IS NULL "
            "OR id < :before) ORDER BY id DESC LIMIT :n"),
            {"before": before, "n": limit + 1})).mappings().all()
    items = [{"id": row["id"], "at": _iso(row["at"]), "actor": row["actor"],
              "action": row["action"], "run_id": row["run_id"], "details": row["details"]}
             for row in rows[:limit]]
    return {"items": items, "next_before": items[-1]["id"] if len(rows) > limit else None}


# --------------------------------------------------------------- queueing


async def _existing_for_commit(session: AsyncSession, sha: str) -> dict[str, Any] | None:
    row = (await session.execute(text(
        "SELECT * FROM deploy_gate_runs WHERE commit_sha = :sha "
        "ORDER BY created_at DESC LIMIT 1"), {"sha": sha})).mappings().one_or_none()
    return dict(row) if row else None


async def _enqueue(session: AsyncSession, *, trigger_kind: str, triggered_by: str,
                   commit_sha: str | None, pr_number: int | None = None,
                   delivery_id: str | None = None, reason: str | None = None) -> dict[str, Any]:
    run_id = new_id("dgr")
    await session.execute(text(
        "INSERT INTO deploy_gate_runs (id, trigger_kind, triggered_by, delivery_id, commit_sha, "
        "pr_number, status_reason) VALUES (:id, :kind, :by, :delivery, :sha, :pr, :reason)"),
        {"id": run_id, "kind": trigger_kind, "by": triggered_by[:128], "delivery": delivery_id,
         "sha": commit_sha, "pr": pr_number, "reason": reason})
    await _audit(session, triggered_by, "queued", run_id, trigger=trigger_kind,
                 commit=commit_sha, pr=pr_number, reason=reason)
    return {"status": "queued", "run_id": run_id}


async def _enqueue_once(session: AsyncSession, *, trigger_kind: str, triggered_by: str,
                        commit_sha: str, pr_number: int | None = None,
                        delivery_id: str | None = None) -> dict[str, Any]:
    """Automatic triggers queue a commit once: a commit the gate has already seen
    (queued, running, blocked, deployed...) is not re-run every poll or redelivery.
    Only the operator's manual trigger runs a commit again."""
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('deploy_gate_queue'))"))
    existing = await _existing_for_commit(session, commit_sha)
    if existing is not None:
        if pr_number and not existing.get("pr_number"):
            await session.execute(text(
                "UPDATE deploy_gate_runs SET pr_number = :pr WHERE id = :id"),
                {"pr": pr_number, "id": existing["id"]})
        await _audit(session, triggered_by, "duplicate", existing["id"], trigger=trigger_kind,
                     commit=commit_sha, existing_status=existing["status"])
        return {"status": "duplicate", "run_id": existing["id"],
                "existing_status": existing["status"]}
    return await _enqueue(session, trigger_kind=trigger_kind, triggered_by=triggered_by,
                          commit_sha=commit_sha, pr_number=pr_number, delivery_id=delivery_id)


# ---------------------------------------------------------------- webhook


def verify_github_signature(secret: str, body: bytes, header: str | None) -> bool:
    """GitHub's ``X-Hub-Signature-256: sha256=<hex HMAC of the raw body>``."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256=").strip())


def classify_github_event(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """What a verified GitHub delivery asks of the gate (pure, for tests).

    Returns {"queue": True, commit, pr_number, sender} for a merge into the
    branch, otherwise {"queue": False, "reason": ...}. Only the configured
    repository and branch count; deletions and force pushes never queue.
    """
    branch = settings.deploy_gate_branch
    repo = str((payload.get("repository") or {}).get("full_name") or "")
    sender = str((payload.get("sender") or {}).get("login") or "unknown")
    if event == "ping":
        return {"queue": False, "reason": "ping"}
    if repo.lower() != settings.deploy_gate_repo.lower():
        return {"queue": False, "reason": "other_repository"}
    if event == "push":
        if payload.get("ref") != f"refs/heads/{branch}":
            return {"queue": False, "reason": "not_deploy_branch"}
        if payload.get("deleted"):
            return {"queue": False, "reason": "branch_deleted"}
        if payload.get("forced"):
            return {"queue": False, "reason": "force_push"}
        sha = _sha(payload.get("after"))
        if sha is None:
            return {"queue": False, "reason": "no_commit"}
        return {"queue": True, "commit": sha, "pr_number": None, "sender": sender}
    if event == "pull_request":
        pull = payload.get("pull_request") or {}
        if payload.get("action") != "closed" or not pull.get("merged"):
            return {"queue": False, "reason": "not_merged"}
        if (pull.get("base") or {}).get("ref") != branch:
            return {"queue": False, "reason": "not_deploy_branch"}
        sha = _sha(pull.get("merge_commit_sha"))
        if sha is None:
            return {"queue": False, "reason": "no_commit"}
        number = pull.get("number")
        return {"queue": True, "commit": sha,
                "pr_number": number if isinstance(number, int) else None, "sender": sender}
    return {"queue": False, "reason": "unsupported_event"}


async def receive_github(event: str, delivery_id: str | None, body: bytes,
                         signature: str | None) -> dict[str, Any]:
    secret = settings.deploy_gate_webhook_secret
    if not secret:
        raise NotFound("deploy_gate", "the deploy gate webhook is not configured")
    if not verify_github_signature(secret, body, signature):
        # Unsigned or forged: refused before any write, so it cannot fill the audit.
        raise ApiError(401, "invalid_signature", "the webhook signature does not match")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError(400, "invalid_payload", "the webhook body is not JSON") from exc
    if not isinstance(payload, dict):
        raise ApiError(400, "invalid_payload", "the webhook body is not a JSON object")
    decision = classify_github_event(event, payload)
    delivery = _clip(delivery_id, 80) or None
    async with async_session_factory() as session:
        if not decision["queue"]:
            if decision["reason"] != "ping":
                sender = _clip((payload.get("sender") or {}).get("login"), 100)
                await _audit(session, f"github:{sender}", "webhook_ignored", event=event,
                             reason=decision["reason"], delivery=delivery)
            await session.commit()
            return {"status": "ignored", "reason": decision["reason"]}
        if delivery and await session.scalar(text(
                "SELECT 1 FROM deploy_gate_runs WHERE delivery_id = :d"), {"d": delivery}):
            return {"status": "duplicate", "reason": "delivery_seen"}
        result = await _enqueue_once(
            session, trigger_kind="webhook", triggered_by=f"github:{decision['sender'][:100]}",
            commit_sha=decision["commit"], pr_number=decision["pr_number"],
            delivery_id=delivery)
        await session.commit()
    return result


# ---------------------------------------------------------------- operator

OPERATOR = "operator"


async def set_paused(paused: bool, reason: str | None = None,
                     actor: str = OPERATOR) -> dict[str, Any]:
    async with async_session_factory() as session:
        await _state_row(session, lock=True)
        await session.execute(text(
            "UPDATE deploy_gate_state SET paused = :p, paused_reason = :r, updated_by = :by, "
            "updated_at = NOW() WHERE id = 1"),
            {"p": paused, "r": (_clip(reason) or None) if paused else None, "by": actor})
        await _audit(session, actor, "pause" if paused else "resume", reason=_clip(reason))
        state = await _state_row(session)
        await session.commit()
    return state_out(state)


async def set_authorized(authorized: bool, reason: str | None = None,
                         actor: str = OPERATOR) -> dict[str, Any]:
    async with async_session_factory() as session:
        await _state_row(session, lock=True)
        await session.execute(text(
            "UPDATE deploy_gate_state SET auto_deploy_authorized = :a, "
            "authorized_by = CASE WHEN :a THEN CAST(:by AS varchar) ELSE authorized_by END, "
            "authorized_at = CASE WHEN :a THEN NOW() ELSE authorized_at END, "
            "updated_by = :by, updated_at = NOW() WHERE id = 1"),
            {"a": authorized, "by": actor})
        await _audit(session, actor, "authorize" if authorized else "revoke",
                     reason=_clip(reason))
        state = await _state_row(session)
        await session.commit()
    return state_out(state)


async def trigger_manual(commit: str | None = None, reason: str | None = None,
                         actor: str = OPERATOR) -> dict[str, Any]:
    sha = None
    if commit:
        sha = _sha(commit)
        if sha is None:
            raise ApiError(422, "validation_error", "commit must be a full 40-character SHA",
                           details={"field": "commit"})
    async with async_session_factory() as session:
        result = await _enqueue(session, trigger_kind="manual", triggered_by=actor,
                                commit_sha=sha, reason=_clip(reason) or None)
        await session.commit()
    return result


# ------------------------------------------------------------------ runner


def runner_authorized(token: str | None) -> bool:
    expected = settings.deploy_gate_runner_token
    return bool(expected and token and hmac.compare_digest(token, expected))


async def heartbeat(host: str, version: str | None,
                    deployed_commit: str | None) -> dict[str, Any]:
    async with async_session_factory() as session:
        await _state_row(session, lock=True)
        await session.execute(text(
            "UPDATE deploy_gate_state SET runner_host = :host, runner_version = :version, "
            "runner_last_seen_at = NOW(), "
            "deployed_commit = COALESCE(deployed_commit, :deployed) WHERE id = 1"),
            {"host": _clip(host, 128), "version": _clip(version, 32) or None,
             "deployed": _sha(deployed_commit)})
        state = await _state_row(session)
        await session.commit()
    return state_out(state)


async def runner_enqueue(commit: str, host: str) -> dict[str, Any]:
    """The VPS timer found a new commit on the branch (fallback for a lost webhook)."""
    sha = _sha(commit)
    if sha is None:
        raise ApiError(422, "validation_error", "commit must be a full 40-character SHA",
                       details={"field": "commit"})
    async with async_session_factory() as session:
        result = await _enqueue_once(session, trigger_kind="poll",
                                     triggered_by=f"timer:{_clip(host, 100)}", commit_sha=sha)
        await session.commit()
    return result


async def claim(host: str) -> dict[str, Any]:
    """Hand the runner the newest queued run; older queued runs are folded into it.

    A paused gate claims nothing: queued runs are recorded as skipped, so a
    pause never deploys later by surprise. ``deploy_allowed`` says whether
    this run may reach production: the operator's manual trigger may, an
    automatic one only while auto-deploy is authorized.
    """
    runner = f"runner:{_clip(host, 100)}"
    async with async_session_factory() as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('deploy_gate_queue'))"))
        state = await _state_row(session, lock=True)
        stale = (await session.execute(text(
            "UPDATE deploy_gate_runs SET status = 'failed', finished_at = NOW(), "
            "status_reason = 'the runner stopped before finishing this run' "
            "WHERE status = 'running' AND claimed_at < :cutoff RETURNING id"),
            {"cutoff": _now() - STALE_RUN_AFTER})).scalars().all()
        for run_id in stale:
            await _audit(session, runner, "run_abandoned", run_id)
        queued = [dict(row) for row in (await session.execute(text(
            "SELECT * FROM deploy_gate_runs WHERE status = 'queued' "
            "ORDER BY created_at DESC FOR UPDATE"))).mappings().all()]
        if not queued:
            await session.commit()
            return {"run": None, "state": state_out(state)}
        if state["paused"]:
            for row in queued:
                await session.execute(text(
                    "UPDATE deploy_gate_runs SET status = 'skipped', finished_at = NOW(), "
                    "status_reason = 'the gate is paused' WHERE id = :id"), {"id": row["id"]})
                await _audit(session, runner, "skipped_paused", row["id"])
            await session.commit()
            return {"run": None, "state": state_out(state)}
        newest, older = queued[0], queued[1:]
        for row in older:
            await session.execute(text(
                "UPDATE deploy_gate_runs SET status = 'skipped', finished_at = NOW(), "
                "status_reason = 'superseded by a newer trigger' WHERE id = :id"),
                {"id": row["id"]})
            await _audit(session, runner, "superseded", row["id"], by=newest["id"])
        manual = any(row["trigger_kind"] == "manual" for row in queued)
        deploy_allowed = manual or state["auto_deploy_authorized"]
        await session.execute(text(
            "UPDATE deploy_gate_runs SET status = 'running', claimed_by = :by, "
            "claimed_at = NOW() WHERE id = :id"), {"by": runner, "id": newest["id"]})
        await _audit(session, runner, "claimed", newest["id"], deploy_allowed=deploy_allowed,
                     manual=manual, folded=[row["id"] for row in older])
        await session.commit()
        newest.update(status="running", claimed_by=runner)
    return {"run": run_out(newest, detail=True), "deploy_allowed": deploy_allowed,
            "deployed_commit": state.get("deployed_commit"), "state": state_out(state)}


def _clean_checks(value: Any) -> list[dict[str, Any]]:
    checks = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or item.get("id") not in CHECK_IDS:
            continue
        checks.append({"id": item["id"], "ok": bool(item.get("ok")),
                       "summary": _clip(item.get("summary"))})
    return checks


def clean_report(value: Any) -> dict[str, Any]:
    """The owner report keeps four plain-language fields and nothing else (A-08)."""
    value = value if isinstance(value, dict) else {}
    return {"changed": _lines(value.get("changed")),
            "how_to_try": _lines(value.get("how_to_try")),
            "rolled_back": bool(value.get("rolled_back")),
            "unfinished": _lines(value.get("unfinished"))}


async def update_run(run_id: str, host: str, body: dict[str, Any]) -> dict[str, Any]:
    runner = f"runner:{_clip(host, 100)}"
    status = body.get("status")
    if status not in RUNNER_STATUSES:
        raise ApiError(422, "validation_error", "unknown run status",
                       details={"field": "status", "allowed": list(RUNNER_STATUSES)})
    async with async_session_factory() as session:
        row = (await session.execute(text(
            "SELECT * FROM deploy_gate_runs WHERE id = :id FOR UPDATE"), {"id": run_id}
        )).mappings().one_or_none()
        if row is None:
            raise NotFound("deploy_gate_run", f"run {run_id} not found")
        if row["status"] != "running" or row["claimed_by"] != runner:
            raise ApiError(409, "run_not_claimed",
                           "this run is not running under this runner",
                           details={"status": row["status"], "claimed_by": row["claimed_by"]})
        commit = _sha(body.get("commit_sha")) or row["commit_sha"]
        if status == "deployed" and commit is None:
            raise ApiError(422, "validation_error", "a deployed run names its commit",
                           details={"field": "commit_sha"})
        deploy = body.get("deploy") if isinstance(body.get("deploy"), dict) else {}
        deploy = {key: _clip(value, 120) for key, value in deploy.items()
                  if key in ("stamp", "result", "app_version", "schema")}
        rolled_back = bool(body.get("rolled_back"))
        report = clean_report(body.get("report"))
        report["rolled_back"] = rolled_back
        await session.execute(text(
            "UPDATE deploy_gate_runs SET status = :status, status_reason = :reason, "
            "commit_sha = :sha, checks = CAST(:checks AS jsonb), "
            "deploy = CAST(:deploy AS jsonb), rolled_back = :rb, "
            "report = CAST(:report AS jsonb), log_ref = :log, "
            "finished_at = CASE WHEN :terminal THEN NOW() ELSE NULL END WHERE id = :id"),
            {"status": status, "reason": _clip(body.get("status_reason")) or None,
             "sha": commit, "checks": _json(_clean_checks(body.get("checks"))),
             "deploy": _json(deploy), "rb": rolled_back, "report": _json(report),
             "log": _clip(body.get("log_ref"), 200) or None, "terminal": status in TERMINAL,
             "id": run_id})
        if status == "deployed":
            await session.execute(text(
                "UPDATE deploy_gate_state SET deployed_commit = :sha, deployed_at = NOW() "
                "WHERE id = 1"), {"sha": commit})
        if status in TERMINAL:
            await _audit(session, runner, "finished", run_id, status=status,
                         failed_checks=[c["id"] for c in _clean_checks(body.get("checks"))
                                        if not c["ok"]],
                         rolled_back=rolled_back, commit=commit)
        updated = (await session.execute(text(
            "SELECT * FROM deploy_gate_runs WHERE id = :id"), {"id": run_id})).mappings().one()
        await session.commit()
    return run_out(dict(updated), detail=True)


# ----------------------------------------------------------- task evidence

_TASK_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


async def task_evidence(task_keys: list[str]) -> dict[str, Any]:
    """G-03: has every task a merged PR names been approved, with no failing gate?

    Read-only: the gate never changes a task (R-05). Tasks live on the board
    workspace named by DEPLOY_GATE_BOARD_WORKSPACE_ID.
    """
    keys = [key for key in dict.fromkeys(task_keys or []) if _TASK_KEY.fullmatch(str(key))][:20]
    try:
        workspace_id = uuid.UUID(str(settings.deploy_gate_board_workspace_id))
    except ValueError:
        return {"configured": False, "tasks": []}
    results = []
    async with async_session_factory() as session:
        for key in keys:
            task = (await session.execute(select(CollabTask).where(
                CollabTask.workspace_id == workspace_id, CollabTask.task_key == key)
            )).scalar_one_or_none()
            if task is None:
                results.append({"task_key": key, "found": False})
                continue
            verdict = await session.scalar(text(
                "SELECT verdict FROM audits WHERE task_id = :t ORDER BY created_at DESC, id DESC "
                "LIMIT 1"), {"t": task.id})
            gates = await gate_status(session, task.workspace_id, task=task)
            failing = sorted(kind for kind, entry in gates.items()
                             if entry["status"] in ("FAIL", "ERROR"))
            results.append({"task_key": key, "found": True, "status": task.status.value,
                            "verdict": verdict, "failing_gates": failing})
    return {"configured": True, "tasks": results}
