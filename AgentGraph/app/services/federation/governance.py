"""Basic governance between workspaces (F-10, docs/federation/ICD_I_GOVERNANCE.md).

Four things, kept apart (vision §13.1):

* **local block**: a workspace's own decision about one peer (grants.block);
  it says nothing about anyone else and is never a platform finding;
* **report**: a person files a case with an evidence *summary* (ids, hashes,
  signature digests; never copied content). Filing a report opens a case and
  does nothing else, however many reports there are (F-019);
* **case handling**: a deployment operator moves the case by hand through
  SUBMITTED, TRIAGED, INVESTIGATING, then INSUFFICIENT_EVIDENCE, DISMISSED or
  ACTION_PROPOSED, DECIDED, APPEAL_WINDOW, CLOSED. Every step is recorded;
* **sanctions**: a temporary freeze an operator applies on a decided case,
  or a permanent network-wide block, which needs a closed appeal window and
  a second, different operator. Who may adjudicate is still an owner
  decision (vision §13.3), so the permanent path stays this conservative.

Platform decisions are made on this deployment only. A "platform notice"
arriving from another workspace is refused however it is signed (F-020): no
workspace is the platform authority, and nothing global changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.services.federation import identity
from app.services.federation.envelope import verify_envelope
from app.services.federation.errors import FederationError
from app.services.federation.grants import Actor
from app.services.federation.ids import b32, parse_federation_id

log = logging.getLogger("agentgraph.federation")

CATEGORIES = ("spam", "abuse", "fraud", "impersonation", "data_leak", "prompt_injection", "other")
APPEAL_DAYS = 14  # P1 placeholder (vision §13.3: TBD)
MAX_FREEZE_DAYS = 90  # P1 placeholder
TERMINAL = ("INSUFFICIENT_EVIDENCE", "DISMISSED", "CLOSED", "WITHDRAWN")
MOVES: dict[str, tuple[str, ...]] = {
    "SUBMITTED": ("TRIAGED", "DISMISSED"),
    "TRIAGED": ("INVESTIGATING", "INSUFFICIENT_EVIDENCE", "DISMISSED"),
    "INVESTIGATING": ("ACTION_PROPOSED", "INSUFFICIENT_EVIDENCE", "DISMISSED"),
    "ACTION_PROPOSED": ("DECIDED", "DISMISSED"),
    "DECIDED": ("APPEAL_WINDOW",),
    "APPEAL_WINDOW": ("CLOSED",),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _new_id(prefix: str) -> str:
    return f"{prefix}_" + b32(secrets.token_bytes(16))[:26]


def _require_person(actor: Actor) -> None:
    if actor.kind != "user":
        raise FederationError("FED_E_APPROVAL_NOT_HUMAN", "a person files and answers reports")


def _text(value: Any, name: str, low: int, high: int) -> str:
    if not isinstance(value, str) or not low <= len(value.strip()) <= high:
        raise FederationError(
            "FED_E_TERMS_INVALID", f"{name} is text of {low} to {high} characters"
        )
    from app.services.federation.delegations import scrub

    scrub(value)
    return value.strip()


def _operator(value: Any) -> str:
    if not isinstance(value, str) or not 2 <= len(value.strip()) <= 120:
        raise FederationError(
            "FED_E_TERMS_INVALID", "operator names the person acting (2 to 120 characters)"
        )
    return "operator:" + value.strip()


# ── security events ───────────────────────────────────────────────────────


async def record_security_event(
    workspace_id: str,
    kind: str,
    *,
    peer: str | None = None,
    code: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Something another workspace tried that was refused for safety."""
    try:
        async with async_session_factory() as platform:
            await platform.execute(
                text(
                    "INSERT INTO federation_security_events (workspace_id, tenant_id, kind, "
                    "peer_fid, code, detail) VALUES (CAST(:w AS uuid), '', :k, :p, :c, "
                    "CAST(:d AS jsonb))"
                ),
                {
                    "w": str(workspace_id),
                    "k": kind[:40],
                    "p": peer if isinstance(peer, str) and len(peer) <= 64 else None,
                    "c": code,
                    "d": json.dumps(detail or {}),
                },
            )
            await platform.commit()
    except Exception:  # noqa: BLE001 - recording must never mask the refusal itself
        log.exception("could not record a federation security event")


async def list_security_events(workspace_id: str, limit: int = 100) -> list[dict[str, Any]]:
    async with async_session_factory() as platform:
        rows = (
            (
                await platform.execute(
                    text(
                        "SELECT event_id, kind, peer_fid, code, detail, created_at FROM "
                        "federation_security_events WHERE workspace_id = CAST(:w AS uuid) "
                        "ORDER BY event_id DESC LIMIT :n"
                    ),
                    {"w": str(workspace_id), "n": max(1, min(limit, 500))},
                )
            )
            .mappings()
            .all()
        )
    return [
        {
            "event_id": r["event_id"],
            "kind": r["kind"],
            "peer": r["peer_fid"],
            "code": r["code"],
            "detail": r["detail"] if isinstance(r["detail"], dict) else json.loads(r["detail"]),
            "at": _iso(r["created_at"]),
        }
        for r in rows
    ]


# ── reports ───────────────────────────────────────────────────────────────


async def _local_fid(platform: AsyncSession, workspace_id: str) -> str:
    from app.services.federation import grants

    return await grants._local_fid(platform, workspace_id)


async def _evidence(
    platform: AsyncSession, workspace_id: str, accused_ws: str, refs: Any
) -> list[dict[str, Any]]:
    """Turn references into a summary of what can be verified later."""
    if refs is None:
        return []
    if not isinstance(refs, list) or len(refs) > 50:
        raise FederationError("FED_E_TERMS_INVALID", "evidence is a list of up to 50 references")
    out: list[dict[str, Any]] = []
    for ref in refs:
        kind = ref.get("kind") if isinstance(ref, dict) else None
        ref_id = ref.get("id") if isinstance(ref, dict) else None
        if kind == "message" and isinstance(ref_id, str):
            row = (
                (
                    await platform.execute(
                        text(
                            "SELECT message_id, direction, kind, payload_sha256, envelope, "
                            "created_at FROM federation_messages WHERE workspace_id = "
                            "CAST(:w AS uuid) AND message_id = :m AND peer_workspace_fid = :p"
                        ),
                        {"w": str(workspace_id), "m": ref_id, "p": accused_ws},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise FederationError(
                    "FED_E_CASE_UNKNOWN", "no such message with that workspace", ref=ref_id
                )
            envelope = row["envelope"] or {}
            signature = envelope.get("signature") if isinstance(envelope, dict) else None
            out.append(
                {
                    "kind": "message",
                    "message_id": row["message_id"],
                    "direction": row["direction"],
                    "payload_sha256": row["payload_sha256"],
                    # The sender's own signature over it, by digest: it can be
                    # shown to an investigator without copying the text.
                    "signature_sha256": hashlib.sha256(signature.encode()).hexdigest()
                    if isinstance(signature, str)
                    else None,
                    "at": _iso(row["created_at"]),
                }
            )
        elif kind == "delegation" and isinstance(ref_id, str):
            row = (
                (
                    await platform.execute(
                        text(
                            "SELECT federation_job_id, role, state, version, acceptance_hash, "
                            "updated_at FROM federation_delegations WHERE workspace_id = "
                            "CAST(:w AS uuid) AND federation_job_id = :j AND "
                            "peer_workspace_fid = :p"
                        ),
                        {"w": str(workspace_id), "j": ref_id, "p": accused_ws},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise FederationError(
                    "FED_E_CASE_UNKNOWN", "no such delegation with that workspace", ref=ref_id
                )
            out.append(
                {
                    "kind": "delegation",
                    "federation_job_id": row["federation_job_id"],
                    "role": row["role"],
                    "state": row["state"],
                    "version": row["version"],
                    "acceptance_hash": row["acceptance_hash"],
                    "at": _iso(row["updated_at"]),
                }
            )
        else:
            raise FederationError(
                "FED_E_TERMS_INVALID", "evidence items are {kind: message | delegation, id}"
            )
    return out


async def _history(
    platform: AsyncSession,
    row: dict[str, Any],
    *,
    from_state: str | None,
    to_state: str,
    actor: str,
    note: str | None = None,
) -> None:
    await platform.execute(
        text(
            "INSERT INTO federation_report_history (report_id, workspace_id, tenant_id, "
            "from_state, to_state, actor, note) VALUES (:r, CAST(:w AS uuid), '', :f, :t, :a, :n)"
        ),
        {
            "r": row["report_id"],
            "w": str(row["workspace_id"]),
            "f": from_state,
            "t": to_state,
            "a": actor,
            "n": note,
        },
    )


async def file_report(
    workspace_id: str,
    *,
    accused: str,
    category: str,
    statement: str,
    evidence: Any = None,
    block: bool = False,
    actor: Actor,
) -> dict[str, Any]:
    """Open a case. Nothing is sanctioned by filing (F-019)."""
    _require_person(actor)
    id_type, _ = parse_federation_id(accused)
    if id_type not in ("workspace", "agent"):
        raise FederationError("FED_E_TYPE_MISMATCH", "report a workspace or an agent")
    if category not in CATEGORIES:
        raise FederationError(
            "FED_E_TERMS_INVALID", "unknown category", categories=list(CATEGORIES)
        )
    statement = _text(statement, "statement", 10, 4000)
    accused_view = await identity.resolve(accused)
    accused_ws = accused if id_type == "workspace" else accused_view["parent_id"]
    async with async_session_factory() as platform:
        local = await _local_fid(platform, workspace_id)
        if accused_ws == local:
            raise FederationError("FED_E_TERMS_INVALID", "a workspace cannot report itself")
        summary = await _evidence(platform, workspace_id, accused_ws, evidence)
        row = {
            "report_id": _new_id("frp"),
            "workspace_id": workspace_id,
        }
        await platform.execute(
            text(
                "INSERT INTO federation_reports (report_id, workspace_id, tenant_id, reporter, "
                "accused_fid, category, statement, evidence, also_blocked) VALUES (:r, "
                "CAST(:w AS uuid), '', :by, :acc, :cat, :st, CAST(:ev AS jsonb), :blk)"
            ),
            {
                "r": row["report_id"],
                "w": str(workspace_id),
                "by": actor.ref,
                "acc": accused,
                "cat": category,
                "st": statement,
                "ev": json.dumps(summary),
                "blk": bool(block),
            },
        )
        await _history(platform, row, from_state=None, to_state="SUBMITTED", actor=actor.ref)
        await platform.commit()
    if block:
        # The reporter's own choice, and only their own: a local block.
        await _block_locally(workspace_id, accused_ws, actor)
    return await get_report(workspace_id, row["report_id"])


async def _block_locally(workspace_id: str, peer_ws: str, actor: Actor) -> None:
    from app.services.federation import grants

    for connection in await grants.list_connections(workspace_id):
        if connection["peer_workspace"] == peer_ws and connection["state"] in (
            "REQUESTED",
            "ACTIVE",
            "EXPIRED",
            "SUSPENDED",
        ):
            if actor.is_owner:
                await grants.block(workspace_id, connection["connection_id"], actor=actor)
            else:
                await grants.freeze(workspace_id, connection["connection_id"], actor=actor)


async def _report_row(
    platform: AsyncSession, report_id: str, *, workspace_id: str | None = None, lock: bool = False
) -> dict[str, Any]:
    sql = "SELECT * FROM federation_reports WHERE report_id = :r"
    params: dict[str, Any] = {"r": report_id}
    if workspace_id is not None:
        sql += " AND workspace_id = CAST(:w AS uuid)"
        params["w"] = str(workspace_id)
    if lock:
        sql += " FOR UPDATE"
    row = (await platform.execute(text(sql), params)).mappings().first()
    if row is None:
        raise FederationError("FED_E_CASE_UNKNOWN", "no such report")
    return dict(row)


async def _case_history(platform: AsyncSession, report_id: str) -> list[dict[str, Any]]:
    rows = (
        (
            await platform.execute(
                text(
                    "SELECT from_state, to_state, actor, note, created_at FROM "
                    "federation_report_history WHERE report_id = :r ORDER BY history_id"
                ),
                {"r": report_id},
            )
        )
        .mappings()
        .all()
    )
    return [{**dict(r), "created_at": _iso(r["created_at"])} for r in rows]


async def _sanctions_for_case(platform: AsyncSession, report_id: str) -> list[dict[str, Any]]:
    rows = (
        (
            await platform.execute(
                text("SELECT * FROM federation_sanctions WHERE report_id = :r ORDER BY created_at"),
                {"r": report_id},
            )
        )
        .mappings()
        .all()
    )
    return [_sanction_out(dict(r)) for r in rows]


def _report_out(row: dict[str, Any]) -> dict[str, Any]:
    evidence = row["evidence"]
    return {
        "report_id": row["report_id"],
        "accused": row["accused_fid"],
        "category": row["category"],
        "statement": row["statement"],
        "evidence": evidence if isinstance(evidence, list) else json.loads(evidence),
        "case_state": row["case_state"],
        "decision": row["decision"],
        "appeal_until": _iso(row["appeal_until"]),
        "appeal_statement": row["appeal_statement"],
        "also_blocked": row["also_blocked"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


async def get_report(workspace_id: str, report_id: str) -> dict[str, Any]:
    async with async_session_factory() as platform:
        row = await _report_row(platform, report_id, workspace_id=workspace_id)
        history = await _case_history(platform, report_id)
        sanctions = await _sanctions_for_case(platform, report_id)
    return {**_report_out(row), "history": history, "sanctions": sanctions}


async def list_reports(workspace_id: str) -> list[dict[str, Any]]:
    async with async_session_factory() as platform:
        rows = (
            (
                await platform.execute(
                    text(
                        "SELECT * FROM federation_reports WHERE workspace_id = CAST(:w AS uuid) "
                        "ORDER BY created_at DESC"
                    ),
                    {"w": str(workspace_id)},
                )
            )
            .mappings()
            .all()
        )
    return [_report_out(dict(r)) for r in rows]


async def withdraw(workspace_id: str, report_id: str, *, actor: Actor) -> dict[str, Any]:
    _require_person(actor)
    async with async_session_factory() as platform:
        row = await _report_row(platform, report_id, workspace_id=workspace_id, lock=True)
        if row["case_state"] not in ("SUBMITTED", "TRIAGED", "INVESTIGATING"):
            raise FederationError(
                "FED_E_CASE_TRANSITION",
                "a case can be withdrawn only before an action is proposed",
                state=row["case_state"],
            )
        await _move(platform, row, "WITHDRAWN", actor=actor.ref)
        await platform.commit()
    return await get_report(workspace_id, report_id)


async def _move(
    platform: AsyncSession,
    row: dict[str, Any],
    to_state: str,
    *,
    actor: str,
    note: str | None = None,
    decision: str | None = None,
) -> None:
    appeal_until = row["appeal_until"]
    if to_state == "APPEAL_WINDOW":
        appeal_until = _now() + timedelta(days=APPEAL_DAYS)
    await platform.execute(
        text(
            "UPDATE federation_reports SET case_state = :s, decision = COALESCE(:d, decision), "
            "appeal_until = :u, updated_at = NOW() WHERE report_id = :r"
        ),
        {"s": to_state, "d": decision, "u": appeal_until, "r": row["report_id"]},
    )
    await _history(
        platform, row, from_state=row["case_state"], to_state=to_state, actor=actor, note=note
    )


# ── the accused side ──────────────────────────────────────────────────────


async def notices(workspace_id: str) -> list[dict[str, Any]]:
    """Decided cases about this workspace or its agents. Who reported and what
    they wrote is not shown; the category, the decision and the right to
    appeal are."""
    async with async_session_factory() as platform:
        local = await _local_fid(platform, workspace_id)
        rows = (
            (
                await platform.execute(
                    text(
                        "SELECT r.* FROM federation_reports r LEFT JOIN federation_identities i "
                        "ON i.federation_id = r.accused_fid WHERE (r.accused_fid = :me OR "
                        "i.parent_id = :me) AND r.case_state IN ('DECIDED', 'APPEAL_WINDOW', "
                        "'CLOSED') ORDER BY r.updated_at DESC"
                    ),
                    {"me": local},
                )
            )
            .mappings()
            .all()
        )
        out = []
        for row in rows:
            out.append(
                {
                    "report_id": row["report_id"],
                    "accused": row["accused_fid"],
                    "category": row["category"],
                    "case_state": row["case_state"],
                    "decision": row["decision"],
                    "appeal_until": _iso(row["appeal_until"]),
                    "appealed": row["appeal_statement"] is not None,
                    "sanctions": await _sanctions_for_case(platform, row["report_id"]),
                    "issued_by": "this Veynod deployment's operators",
                }
            )
    return out


async def appeal(
    workspace_id: str, report_id: str, *, statement: str, actor: Actor
) -> dict[str, Any]:
    _require_person(actor)
    if not actor.is_owner:
        raise FederationError("FED_E_APPROVAL_NOT_HUMAN", "an owner of the workspace appeals")
    statement = _text(statement, "statement", 10, 4000)
    async with async_session_factory() as platform:
        local = await _local_fid(platform, workspace_id)
        row = await _report_row(platform, report_id, lock=True)
        accused_parent = await platform.scalar(
            text("SELECT parent_id FROM federation_identities WHERE federation_id = :f"),
            {"f": row["accused_fid"]},
        )
        if local not in (row["accused_fid"], accused_parent):
            raise FederationError("FED_E_CASE_UNKNOWN", "no such case about this workspace")
        if row["case_state"] != "APPEAL_WINDOW" or (
            row["appeal_until"] is not None and row["appeal_until"] <= _now()
        ):
            raise FederationError("FED_E_CASE_TRANSITION", "the appeal window is not open")
        if row["appeal_statement"] is not None:
            raise FederationError("FED_E_CASE_TRANSITION", "already appealed")
        await platform.execute(
            text(
                "UPDATE federation_reports SET appeal_statement = :s, updated_at = NOW() "
                "WHERE report_id = :r"
            ),
            {"s": statement, "r": report_id},
        )
        await _history(
            platform,
            row,
            from_state=row["case_state"],
            to_state=row["case_state"],
            actor=f"accused:{actor.ref}",
            note="appeal filed",
        )
        await platform.commit()
    return {"report_id": report_id, "appealed": True}


# ── operators ─────────────────────────────────────────────────────────────


async def list_cases(state: str | None = None) -> list[dict[str, Any]]:
    async with async_session_factory() as platform:
        sql = "SELECT * FROM federation_reports"
        params: dict[str, Any] = {}
        if state:
            sql += " WHERE case_state = :s"
            params["s"] = state
        rows = (await platform.execute(text(sql + " ORDER BY created_at"), params)).mappings().all()
        counts = {
            r[0]: r[1]
            for r in (
                await platform.execute(
                    text(
                        "SELECT accused_fid, count(*) FROM federation_reports GROUP BY accused_fid"
                    )
                )
            ).all()
        }
    return [
        {**_report_out(dict(r)), "reports_about_accused": counts.get(r["accused_fid"], 0)}
        for r in rows
    ]


async def get_case(report_id: str) -> dict[str, Any]:
    async with async_session_factory() as platform:
        row = await _report_row(platform, report_id)
        history = await _case_history(platform, report_id)
        sanctions = await _sanctions_for_case(platform, report_id)
    return {**_report_out(row), "history": history, "sanctions": sanctions}


async def move_case(
    report_id: str,
    to_state: str,
    *,
    operator: str,
    note: str | None = None,
    decision: str | None = None,
) -> dict[str, Any]:
    who = _operator(operator)
    async with async_session_factory() as platform:
        row = await _report_row(platform, report_id, lock=True)
        if to_state not in MOVES.get(row["case_state"], ()):
            raise FederationError(
                "FED_E_CASE_TRANSITION",
                f"a case in {row['case_state']} cannot move to {to_state}",
                allowed=list(MOVES.get(row["case_state"], ())),
            )
        if to_state == "DECIDED":
            decision = _text(decision, "decision", 5, 2000)
        if to_state == "CLOSED" and row["appeal_until"] and row["appeal_until"] > _now():
            if row["appeal_statement"] is None:
                raise FederationError(
                    "FED_E_CASE_TRANSITION",
                    "the appeal window is still open",
                    until=_iso(row["appeal_until"]),
                )
        if note is not None:
            note = _text(note, "note", 1, 4000)
        await _move(platform, row, to_state, actor=who, note=note, decision=decision)
        await platform.commit()
    return await get_case(report_id)


def _sanction_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "sanction_id": row["sanction_id"],
        "subject": row["subject_fid"],
        "report_id": row["report_id"],
        "kind": row["kind"],
        "state": row["state"],
        "reason": row["reason"],
        "proposed_by": row["proposed_by"],
        "confirmed_by": row["confirmed_by"],
        "expires_at": _iso(row["expires_at"]),
        "created_at": _iso(row["created_at"]),
        "decided_at": _iso(row["decided_at"]),
        "lifted_at": _iso(row["lifted_at"]),
    }


async def _apply_status(fid: str) -> None:
    """The identity is frozen while any sanction on it is active."""
    async with async_session_factory() as platform:
        active = await platform.scalar(
            text(
                "SELECT count(*) FROM federation_sanctions WHERE subject_fid = :f "
                "AND state = 'active'"
            ),
            {"f": fid},
        )
        status = await platform.scalar(
            text("SELECT status FROM federation_identities WHERE federation_id = :f"), {"f": fid}
        )
    if active and status == "active":
        await identity.set_status(fid, "frozen", actor="system:sanction")
    elif not active and status == "frozen":
        await identity.set_status(fid, "active", actor="system:sanction")


async def propose_sanction(
    report_id: str,
    *,
    kind: str,
    reason: str,
    operator: str,
    days: int | None = None,
) -> dict[str, Any]:
    """A temporary freeze takes effect on a decided case. A permanent block is
    only ever proposed here; it needs a closed case and a second operator."""
    who = _operator(operator)
    reason = _text(reason, "reason", 5, 2000)
    if kind not in ("temporary_freeze", "permanent_block"):
        raise FederationError("FED_E_SANCTION_RULE", "kind is temporary_freeze or permanent_block")
    async with async_session_factory() as platform:
        row = await _report_row(platform, report_id, lock=True)
        if kind == "temporary_freeze":
            if row["case_state"] not in ("DECIDED", "APPEAL_WINDOW", "CLOSED"):
                raise FederationError(
                    "FED_E_SANCTION_RULE",
                    "a freeze follows a decided case",
                    state=row["case_state"],
                )
            if not isinstance(days, int) or not 1 <= days <= MAX_FREEZE_DAYS:
                raise FederationError(
                    "FED_E_SANCTION_RULE", f"a freeze lasts 1 to {MAX_FREEZE_DAYS} days"
                )
        elif row["case_state"] != "CLOSED":
            raise FederationError(
                "FED_E_SANCTION_RULE",
                "a permanent block is proposed only after the case is closed, appeal included",
                state=row["case_state"],
            )
        sanction_id = _new_id("fsn")
        state = "active" if kind == "temporary_freeze" else "proposed"
        await platform.execute(
            text(
                "INSERT INTO federation_sanctions (sanction_id, subject_fid, report_id, kind, "
                "state, reason, proposed_by, expires_at, decided_at) VALUES (:s, :f, :r, :k, :st, "
                ":why, :by, :x, :d)"
            ),
            {
                "s": sanction_id,
                "f": row["accused_fid"],
                "r": report_id,
                "k": kind,
                "st": state,
                "why": reason,
                "by": who,
                "x": _now() + timedelta(days=days) if kind == "temporary_freeze" else None,
                "d": _now() if state == "active" else None,
            },
        )
        await _history(
            platform,
            row,
            from_state=row["case_state"],
            to_state=row["case_state"],
            actor=who,
            note=f"sanction {kind} {state}",
        )
        await platform.commit()
    await _apply_status(row["accused_fid"])
    return await get_sanction(sanction_id)


async def confirm_sanction(sanction_id: str, *, operator: str) -> dict[str, Any]:
    who = _operator(operator)
    async with async_session_factory() as platform:
        row = await _sanction_row(platform, sanction_id, lock=True)
        if row["kind"] != "permanent_block" or row["state"] != "proposed":
            raise FederationError(
                "FED_E_SANCTION_RULE", "only a proposed permanent block is confirmed"
            )
        if who == row["proposed_by"]:
            raise FederationError(
                "FED_E_SANCTION_RULE", "a second, different operator confirms a permanent block"
            )
        await platform.execute(
            text(
                "UPDATE federation_sanctions SET state = 'active', confirmed_by = :by, "
                "decided_at = NOW() WHERE sanction_id = :s"
            ),
            {"by": who, "s": sanction_id},
        )
        await platform.commit()
    await _apply_status(row["subject_fid"])
    return await get_sanction(sanction_id)


async def lift_sanction(sanction_id: str, *, operator: str, reason: str) -> dict[str, Any]:
    who = _operator(operator)
    _text(reason, "reason", 5, 2000)
    async with async_session_factory() as platform:
        row = await _sanction_row(platform, sanction_id, lock=True)
        if row["state"] == "lifted":
            raise FederationError("FED_E_SANCTION_RULE", "already lifted")
        await platform.execute(
            text(
                "UPDATE federation_sanctions SET state = 'lifted', lifted_at = NOW(), "
                "lifted_by = :by WHERE sanction_id = :s"
            ),
            {"by": who, "s": sanction_id},
        )
        await platform.commit()
    await _apply_status(row["subject_fid"])
    return await get_sanction(sanction_id)


async def expire_due() -> int:
    """Lift temporary freezes whose time is up (run by the delivery loop)."""
    async with async_session_factory() as platform:
        due = (
            (
                await platform.execute(
                    text(
                        "UPDATE federation_sanctions SET state = 'lifted', lifted_at = NOW(), "
                        "lifted_by = 'system:expired' WHERE kind = 'temporary_freeze' AND "
                        "state = 'active' AND expires_at <= NOW() RETURNING subject_fid"
                    )
                )
            )
            .scalars()
            .all()
        )
        await platform.commit()
    for fid in set(due):
        await _apply_status(fid)
    return len(due)


async def _sanction_row(
    platform: AsyncSession, sanction_id: str, *, lock: bool = False
) -> dict[str, Any]:
    row = (
        (
            await platform.execute(
                text(
                    "SELECT * FROM federation_sanctions WHERE sanction_id = :s"
                    + (" FOR UPDATE" if lock else "")
                ),
                {"s": sanction_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise FederationError("FED_E_CASE_UNKNOWN", "no such sanction")
    return dict(row)


async def get_sanction(sanction_id: str) -> dict[str, Any]:
    async with async_session_factory() as platform:
        return _sanction_out(await _sanction_row(platform, sanction_id))


async def global_status(fid: str) -> dict[str, Any]:
    """What this deployment has decided about one identity."""
    async with async_session_factory() as platform:
        rows = (
            (
                await platform.execute(
                    text(
                        "SELECT * FROM federation_sanctions WHERE subject_fid = :f "
                        "ORDER BY created_at"
                    ),
                    {"f": fid},
                )
            )
            .mappings()
            .all()
        )
        status = await platform.scalar(
            text("SELECT status FROM federation_identities WHERE federation_id = :f"), {"f": fid}
        )
    sanctions = [_sanction_out(dict(r)) for r in rows]
    return {
        "subject": fid,
        "identity_status": status,
        "active": [s for s in sanctions if s["state"] == "active"],
        "proposed": [s for s in sanctions if s["state"] == "proposed"],
        "permanently_blocked": any(
            s["kind"] == "permanent_block" and s["state"] == "active" for s in sanctions
        ),
    }


# ── inbound (F-020) ───────────────────────────────────────────────────────


async def receive_notice(workspace_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
    """Another workspace sent a "platform notice". No workspace speaks for the
    platform: refused whether or not its signature checks out, recorded as a
    security event, and nothing global changes."""
    issuer = envelope.get("issuer") if isinstance(envelope, dict) else None
    async with async_session_factory() as platform:
        local = await _local_fid(platform, workspace_id)
    try:
        verified = await verify_envelope(
            envelope,
            local_workspace_fid=local,
            key_lookup=identity.key_lookup,
            replay_check=identity.replay_check_for(workspace_id),
        )
    except FederationError as exc:
        await record_security_event(
            workspace_id,
            "forged_platform_notice",
            peer=issuer if isinstance(issuer, str) else None,
            code=exc.code,
            detail={"stage": "signature"},
        )
        raise
    await record_security_event(
        workspace_id,
        "forged_platform_notice",
        peer=verified.issuer,
        code="FED_E_AUTHORITY_INVALID",
        detail={"stage": "authority", "object_id": verified.envelope.get("object_id")},
    )
    raise FederationError(
        "FED_E_AUTHORITY_INVALID",
        "platform notices come from this deployment's operators, never from another workspace",
    )
