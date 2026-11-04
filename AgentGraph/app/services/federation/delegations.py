"""The task bridge (F-07): one workspace asks another to do a piece of work.

Contract: docs/federation/ICD_I_BRIDGE.md. Each side keeps its own copy of the
delegation (``federation_delegations``); the two copies move together through
signed ``delegate.*`` envelopes. The provider does the work in a task of its
own, created through the ordinary task service by the provider's own person
(BRIDGE-INV-2). The requester's local task, if any, is never touched from here.

Delivery is an outbox (ICD_I_BRIDGE §5): a transition is committed locally with
its signed envelope queued, then delivered. If the peer cannot be reached the
envelope stays queued and ``sync`` re-sends it, in order, without re-signing.
The receiver applies exactly the next version, treats an older or repeated one
as a no-op (F-008, F-012) and refuses a gap.

P1 (ICD placeholders): ``cancellation_policy`` is ``drain`` only, ``budget_cap``
is null or zero (no settlement), offers expire after 24 hours, at most three
revisions, results travel inline, and only people accept, reject or judge.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.services.federation import grants, identity
from app.services.federation.canonical import CanonicalError, canonical_bytes, sha256_hex
from app.services.federation.envelope import Verified, verify_envelope
from app.services.federation.errors import FederationError
from app.services.federation.grants import Actor
from app.services.federation.ids import b32, parse_federation_id, require_type

OFFER_TTL = timedelta(hours=24)  # P1 placeholder
MAX_REVISIONS = 3  # P1 placeholder
MAX_RESULT_BYTES = 48 * 1024  # stays inside the 64 KiB envelope limit
CLASSIFICATIONS = ("public", "internal", "confidential")
TERM_FIELDS = (
    "objective",
    "acceptance_criteria",
    "input_summary",
    "data_classification",
    "allowed_use",
    "deadline",
    "budget_cap",
    "result_contract",
    "cancellation_policy",
    "external_side_effects",
)
TERMINAL = (
    "ACCEPTED_RESULT",
    "REJECTED",
    "EXPIRED",
    "CANCELLED",
    "STOP_NOT_CONFIRMED",
    "FAILED",
    "DISPUTED",
)

# action -> (who acts, allowed from-states, to-state, envelope type)
TRANSITIONS: dict[str, tuple[str, tuple[str, ...], str, str]] = {
    "accept": ("provider", ("OFFERED",), "ACCEPTED", "delegate.accept"),
    "reject": ("provider", ("OFFERED",), "REJECTED", "delegate.reject"),
    "submit": ("provider", ("ACCEPTED", "RUNNING"), "SUBMITTED", "delegate.result"),
    "accept_result": ("requester", ("SUBMITTED",), "ACCEPTED_RESULT", "delegate.accept_result"),
    "revise": ("requester", ("SUBMITTED",), "RUNNING", "delegate.revise"),
    "dispute": ("requester", ("SUBMITTED",), "DISPUTED", "delegate.dispute"),
}
_ACTION_OF_TYPE = {spec[3]: action for action, spec in TRANSITIONS.items()}

# What must never cross: local identifiers and anything shaped like a credential
# (ID-INV-8, ICD_I_MSG §5, F-006).
_LOCAL_ID = re.compile(r"\b(prn|tok|ten|agk)_[A-Za-z0-9]{6,}")
_CREDENTIAL = re.compile(
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}|\b(sk|ghp|gho|xox[abp])[-_][A-Za-z0-9-]{12,}"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_job_id() -> str:
    return "fjob_" + b32(secrets.token_bytes(16))[:26]


def scrub(value: Any) -> None:
    """Refuse to send local ids or credentials, wherever they sit in a payload."""
    blob = json.dumps(value, ensure_ascii=False)
    if _CREDENTIAL.search(blob):
        raise FederationError(
            "FED_E_CREDENTIAL_IN_PAYLOAD", "the content looks like it carries a credential"
        )
    if _LOCAL_ID.search(blob):
        raise FederationError(
            "FED_E_TERMS_INVALID", "the content names a local identifier; describe it instead"
        )


def _text(terms: dict[str, Any], key: str, limit: int) -> str:
    value = terms.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise FederationError("FED_E_TERMS_INVALID", f"{key} is required (1-{limit} characters)")
    return value.strip()


def validate_terms(terms: Any) -> dict[str, Any]:
    """The frozen delegation object (ICD_I_BRIDGE §2), normalised."""
    if not isinstance(terms, dict) or set(terms) != set(TERM_FIELDS):
        raise FederationError(
            "FED_E_TERMS_INVALID",
            "terms must have exactly the documented fields",
            expected=list(TERM_FIELDS),
        )
    criteria = terms["acceptance_criteria"]
    if (
        not isinstance(criteria, list)
        or not 1 <= len(criteria) <= 20
        or not all(isinstance(c, str) and c.strip() and len(c) <= 1000 for c in criteria)
    ):
        raise FederationError("FED_E_TERMS_INVALID", "acceptance_criteria: 1-20 non-empty items")
    if terms["data_classification"] not in CLASSIFICATIONS:
        raise FederationError("FED_E_TERMS_INVALID", "unknown data_classification")
    if terms["cancellation_policy"] != "drain":
        raise FederationError("FED_E_TERMS_INVALID", "P1 supports cancellation_policy 'drain' only")
    if not isinstance(terms["external_side_effects"], bool):
        raise FederationError("FED_E_TERMS_INVALID", "external_side_effects is true or false")
    budget = terms["budget_cap"]
    if budget is not None and budget != {"amount": 0, "unit": "none"}:
        raise FederationError(
            "FED_E_SETTLEMENT_DISABLED",
            "settlement is not enabled: budget_cap must be null or {amount: 0, unit: none}",
        )
    try:
        deadline = datetime.fromisoformat(str(terms["deadline"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise FederationError("FED_E_TERMS_INVALID", "deadline is RFC 3339") from exc
    if deadline.tzinfo is None:
        raise FederationError("FED_E_TERMS_INVALID", "deadline needs a time zone")
    normal = {
        "objective": _text(terms, "objective", 4000),
        "acceptance_criteria": [c.strip() for c in criteria],
        "input_summary": _text(terms, "input_summary", 8000),
        "data_classification": terms["data_classification"],
        "allowed_use": _text(terms, "allowed_use", 1000),
        "deadline": deadline.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        "budget_cap": budget,
        "result_contract": _text(terms, "result_contract", 2000),
        "cancellation_policy": "drain",
        "external_side_effects": terms["external_side_effects"],
    }
    scrub(normal)
    return normal


def acceptance_hash(terms: dict[str, Any]) -> str:
    """ICD_I_BRIDGE §7: SHA-256 of the JCS of the frozen terms."""
    return sha256_hex({key: terms[key] for key in TERM_FIELDS})


def validate_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise FederationError("FED_E_TERMS_INVALID", "result is an object")
    summary = result.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 20_000:
        raise FederationError("FED_E_TERMS_INVALID", "result.summary is required")
    artifacts = result.get("artifacts", [])
    if not isinstance(artifacts, list) or len(artifacts) > 50:
        raise FederationError("FED_E_TERMS_INVALID", "result.artifacts: at most 50")
    clean = []
    for item in artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise FederationError("FED_E_TERMS_INVALID", "each artifact has a name")
        content = item.get("content")
        if content is not None and not isinstance(content, str):
            raise FederationError("FED_E_TERMS_INVALID", "artifact content is text")
        digest = (
            hashlib.sha256((content or "").encode()).hexdigest()
            if content is not None
            else item.get("sha256")
        )
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise FederationError("FED_E_TERMS_INVALID", "artifact needs content or a sha256")
        if content is not None and item.get("sha256") not in (None, digest):
            raise FederationError("FED_E_TERMS_INVALID", "artifact sha256 does not match")
        clean.append(
            {
                "name": item["name"][:256],
                "sha256": digest,
                **({"content": content} if content is not None else {}),
            }
        )
    out = {"summary": summary.strip(), "artifacts": clean}
    receipt = result.get("cost_receipt")
    if receipt is not None:
        out["cost_receipt"] = _validate_receipt(receipt)
    if len(canonical_bytes(out)) > MAX_RESULT_BYTES:
        raise FederationError("FED_E_TOO_LARGE", "result exceeds the inline size limit")
    scrub(out)
    return out


def _validate_receipt(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise FederationError("FED_E_TERMS_INVALID", "cost_receipt is an object")
    source = receipt.get("metering_source")
    confidence = receipt.get("confidence")
    if source not in ("platform", "adapter", "self_reported") or confidence not in (
        "high",
        "medium",
        "low",
    ):
        raise FederationError(
            "FED_E_TERMS_INVALID", "cost_receipt needs metering_source and confidence"
        )
    usage = receipt.get("usage", {})
    if not isinstance(usage, dict):
        raise FederationError("FED_E_TERMS_INVALID", "cost_receipt.usage is an object")
    try:
        canonical_bytes(usage)
    except CanonicalError as exc:
        raise FederationError("FED_E_TERMS_INVALID", f"cost_receipt.usage: {exc}") from exc
    return {
        "metering_source": source,
        "confidence": confidence,
        "usage": usage,
        "price_snapshot_id": receipt.get("price_snapshot_id"),
        "proof_ref": receipt.get("proof_ref"),
    }


# ── storage helpers ───────────────────────────────────────────────────────


def _out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "federation_job_id": row["federation_job_id"],
        "peer_workspace": row["peer_workspace_fid"],
        "connection_id": row["connection_id"],
        "role": row["role"],
        "state": row["state"],
        "version": row["version"],
        "terms": row["terms"],
        "acceptance_hash": row["acceptance_hash"],
        "local_task_key": row["local_task_key"],
        # What the peer wrote is data, never instructions (ICD_I_MSG §5).
        "result": row["result"],
        # Fields the peer wrote: data to read, never instructions to follow.
        "untrusted_fields": ["terms"]
        if row["role"] == "provider"
        else (["result"] if row["result"] else []),
        "revisions": row["revisions"],
        "pending_deliveries": len(row["outbox"] or []),
        "offer_expires_at": row["offer_expires_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _loads(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    for key in ("terms", "result", "outbox"):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    return row


async def _row(
    platform: AsyncSession,
    workspace_id: str,
    job_id: str,
    *,
    peer: str | None = None,
    lock: bool = False,
) -> dict[str, Any]:
    query = (
        "SELECT * FROM federation_delegations WHERE workspace_id = CAST(:w AS uuid) "
        "AND federation_job_id = :j"
    )
    params: dict[str, Any] = {"w": str(workspace_id), "j": job_id}
    if peer is not None:
        query += " AND peer_workspace_fid = :p"
        params["p"] = peer
    row = (
        (await platform.execute(text(query + (" FOR UPDATE" if lock else "")), params))
        .mappings()
        .first()
    )
    if row is None:
        raise FederationError("FED_E_JOB_UNKNOWN", "no such delegation")
    return _loads(row)


async def _history(
    platform: AsyncSession,
    row: dict[str, Any],
    action: str,
    *,
    from_state: str | None,
    actor: str,
    envelope: dict[str, Any] | None,
    note: str | None = None,
) -> None:
    await platform.execute(
        text(
            "INSERT INTO federation_delegation_history (workspace_id, tenant_id, "
            "peer_workspace_fid, "
            "federation_job_id, version, action, from_state, to_state, actor, envelope_sha256, "
            "note) "
            "VALUES (CAST(:w AS uuid), '', :p, :j, :v, :a, :f, :t, :who, :h, :n)"
        ),
        {
            "w": str(row["workspace_id"]),
            "p": row["peer_workspace_fid"],
            "j": row["federation_job_id"],
            "v": row["version"],
            "a": action,
            "f": from_state,
            "t": row["state"],
            "who": actor,
            "h": None if envelope is None else sha256_hex(envelope),
            "n": note,
        },
    )


async def _save(platform: AsyncSession, row: dict[str, Any]) -> None:
    await platform.execute(
        text(
            "UPDATE federation_delegations SET state = :s, version = :v, local_task_key = :k, "
            "result = CAST(:r AS jsonb), revisions = :rev, outbox = CAST(:o AS jsonb), "
            "updated_at = NOW() WHERE workspace_id = CAST(:w AS uuid) AND peer_workspace_fid = :p "
            "AND federation_job_id = :j"
        ),
        {
            "s": row["state"],
            "v": row["version"],
            "k": row["local_task_key"],
            "r": None if row["result"] is None else json.dumps(row["result"]),
            "rev": row["revisions"],
            "o": json.dumps(row["outbox"] or []),
            "w": str(row["workspace_id"]),
            "p": row["peer_workspace_fid"],
            "j": row["federation_job_id"],
        },
    )


async def _expire_if_due(platform: AsyncSession, row: dict[str, Any]) -> dict[str, Any]:
    if row["state"] == "OFFERED" and row["offer_expires_at"] <= _now():
        before = row["state"]
        row = {**row, "state": "EXPIRED", "version": row["version"] + 1}
        await _save(platform, row)
        await _history(platform, row, "expire", from_state=before, actor="system", envelope=None)
    return row


async def _local_fid(workspace_id: str) -> str:
    me = await identity.local_identity(workspace_id)
    if me is None:
        raise FederationError(
            "FED_E_IDENTITY_UNKNOWN", "federation is not enabled for this workspace"
        )
    return me["federation_id"]


def _direction(role: str) -> str:
    # The work flows requester -> provider: the requester sends "delegate" out,
    # the provider lets it in. Both sides check their own copy (GRANT-INV-3).
    return "out" if role == "requester" else "in"


# ── delivery ──────────────────────────────────────────────────────────────


async def sync(workspace_id: str, job_id: str) -> dict[str, Any]:
    """Deliver queued envelopes in order; stop at the first one that fails."""
    delivered = 0
    last_error = None
    while True:
        async with async_session_factory() as platform:
            row = await _row(platform, workspace_id, job_id, lock=True)
            outbox = list(row["outbox"] or [])
            if not outbox:
                await platform.commit()
                break
            try:
                await grants._transport.deliver(outbox[0])
            except FederationError as exc:
                last_error = exc.code
                await platform.commit()
                break
            row["outbox"] = outbox[1:]
            await platform.execute(
                text(
                    "UPDATE federation_delegations SET outbox = CAST(:o AS jsonb), updated_at = "
                    "NOW() "
                    "WHERE workspace_id = CAST(:w AS uuid) AND peer_workspace_fid = :p "
                    "AND federation_job_id = :j"
                ),
                {
                    "o": json.dumps(row["outbox"]),
                    "w": str(workspace_id),
                    "p": row["peer_workspace_fid"],
                    "j": job_id,
                },
            )
            await platform.commit()
            delivered += 1
    async with async_session_factory() as platform:
        row = await _row(platform, workspace_id, job_id)
    return {"delegation": _out(row), "delivered": delivered, "last_error": last_error}


# ── local actions ─────────────────────────────────────────────────────────


async def offer(
    workspace_id: str,
    *,
    peer_workspace: str,
    terms: dict[str, Any],
    actor: Actor,
    local_task_key: str | None = None,
) -> dict[str, Any]:
    """The requester's person sends a delegation (BRIDGE-INV-1 checked first)."""
    if actor.kind != "user":
        raise FederationError("FED_E_APPROVAL_NOT_HUMAN", "a person sends a delegation in P1")
    require_type(peer_workspace, "workspace")
    frozen = validate_terms(terms)
    connection = await grants.authorize(workspace_id, peer_workspace, "delegate", direction="out")
    local_fid = await _local_fid(workspace_id)
    job_id = new_job_id()
    expires = _now() + OFFER_TTL
    row = {
        "workspace_id": workspace_id,
        "federation_job_id": job_id,
        "peer_workspace_fid": peer_workspace,
        "connection_id": connection["connection_id"],
        "role": "requester",
        "state": "OFFERED",
        "version": 1,
        "terms": frozen,
        "acceptance_hash": acceptance_hash(frozen),
        "local_task_key": local_task_key,
        "result": None,
        "revisions": 0,
        "outbox": [],
        "offer_expires_at": expires,
    }
    envelope = await identity.sign_as(
        local_fid,
        type="delegate.offer",
        audience=peer_workspace,
        payload={
            "federation_job_id": job_id,
            "version": 1,
            "terms": frozen,
            "offer_expires_at": expires.replace(microsecond=0).isoformat(),
        },
    )
    row["outbox"] = [envelope]
    async with async_session_factory() as platform:
        await platform.execute(
            text(
                "INSERT INTO federation_delegations (workspace_id, tenant_id, federation_job_id, "
                "peer_workspace_fid, connection_id, role, state, version, terms, acceptance_hash, "
                "local_task_key, outbox, offer_expires_at) VALUES (CAST(:w AS uuid), '', :j, "
                ":p, :c, "
                "'requester', 'OFFERED', 1, CAST(:t AS jsonb), :h, :k, CAST(:o AS jsonb), :x)"
            ),
            {
                "w": str(workspace_id),
                "j": job_id,
                "p": peer_workspace,
                "c": connection["connection_id"],
                "t": json.dumps(frozen),
                "h": row["acceptance_hash"],
                "k": local_task_key,
                "o": json.dumps([envelope]),
                "x": expires,
            },
        )
        await _history(platform, row, "offer", from_state=None, actor=actor.ref, envelope=envelope)
        await platform.commit()
    return await sync(workspace_id, job_id)


async def act(
    workspace_id: str,
    job_id: str,
    action: str,
    *,
    actor: Actor,
    result: dict[str, Any] | None = None,
    note: str | None = None,
    create_local_task: Callable[[dict[str, Any]], Awaitable[str]] | None = None,
) -> dict[str, Any]:
    """accept / reject / submit (provider) and accept_result / revise / dispute (requester).

    Accepting creates the provider's own task through ``create_local_task``,
    which the route layer runs with the provider person's own credentials.
    """
    if action not in TRANSITIONS:
        raise FederationError("FED_E_JOB_TRANSITION", "unknown action", action=action)
    role, from_states, to_state, envelope_type = TRANSITIONS[action]
    if actor.kind != "user" and action != "submit":
        # Taking on work, refusing it and judging it are people's decisions.
        raise FederationError("FED_E_APPROVAL_NOT_HUMAN", "a person makes this decision")
    if action == "submit":
        result = validate_result(result)
    if note is not None:
        if not isinstance(note, str) or len(note) > 4000:
            raise FederationError("FED_E_TERMS_INVALID", "note is text up to 4000 characters")
        scrub(note)
    local_fid = await _local_fid(workspace_id)
    async with async_session_factory() as platform:
        row = await _row(platform, workspace_id, job_id, lock=True)
        row = await _expire_if_due(platform, row)
        if row["role"] != role:
            await platform.commit()
            raise FederationError("FED_E_JOB_TRANSITION", f"only the {role} can {action}")
        if row["state"] not in from_states:
            await platform.commit()
            raise FederationError(
                "FED_E_JOB_TRANSITION", f"cannot {action} in {row['state']}", state=row["state"]
            )
        if action == "revise" and row["revisions"] >= MAX_REVISIONS:
            raise FederationError("FED_E_JOB_TRANSITION", "revision limit reached; dispute instead")
        await grants.authorize(
            workspace_id, row["peer_workspace_fid"], "delegate", direction=_direction(role)
        )
        payload: dict[str, Any] = {"federation_job_id": job_id, "version": row["version"] + 1}
        if action == "accept":
            payload["acceptance_hash"] = row["acceptance_hash"]
        if action == "submit":
            payload["result"] = result
        if note:
            payload["note"] = note
        new = {**row, "state": to_state, "version": row["version"] + 1}
        if action == "accept":
            if create_local_task is None:
                raise FederationError("FED_E_JOB_TRANSITION", "accepting needs a local task")
            new["local_task_key"] = await create_local_task(row)
        if action == "submit":
            new["result"] = result
        if action == "revise":
            new["revisions"] = row["revisions"] + 1
        envelope = await identity.sign_as(
            local_fid, type=envelope_type, audience=row["peer_workspace_fid"], payload=payload
        )
        new["outbox"] = [*(row["outbox"] or []), envelope]
        await _save(platform, new)
        await _history(
            platform,
            new,
            action,
            from_state=row["state"],
            actor=actor.ref,
            envelope=envelope,
            note=note,
        )
        if action == "submit" and result.get("cost_receipt"):
            await _store_receipt(platform, new, result["cost_receipt"])
        await platform.commit()
    return await sync(workspace_id, job_id)


async def _store_receipt(platform: AsyncSession, row: dict[str, Any], receipt: dict[str, Any]):
    await platform.execute(
        text(
            "INSERT INTO federation_cost_receipts (workspace_id, tenant_id, peer_workspace_fid, "
            "federation_job_id, result_version, metering_source, confidence, usage, "
            "price_snapshot_id, proof_ref) VALUES (CAST(:w AS uuid), '', :p, :j, :v, :m, :c, "
            "CAST(:u AS jsonb), :ps, :pr) ON CONFLICT DO NOTHING"
        ),
        {
            "w": str(row["workspace_id"]),
            "p": row["peer_workspace_fid"],
            "j": row["federation_job_id"],
            "v": row["version"],
            "m": receipt["metering_source"],
            "c": receipt["confidence"],
            "u": json.dumps(receipt["usage"]),
            "ps": receipt.get("price_snapshot_id"),
            "pr": receipt.get("proof_ref"),
        },
    )


async def list_delegations(workspace_id: str) -> list[dict[str, Any]]:
    async with async_session_factory() as platform:
        rows = (
            (
                await platform.execute(
                    text(
                        "SELECT * FROM federation_delegations WHERE workspace_id = CAST(:w AS "
                        "uuid) "
                        "ORDER BY updated_at DESC"
                    ),
                    {"w": str(workspace_id)},
                )
            )
            .mappings()
            .all()
        )
        out = [_out(await _expire_if_due(platform, _loads(r))) for r in rows]
        await platform.commit()
    return out


async def get(workspace_id: str, job_id: str) -> dict[str, Any]:
    async with async_session_factory() as platform:
        row = await _expire_if_due(platform, await _row(platform, workspace_id, job_id, lock=True))
        await platform.commit()
        history = (
            (
                await platform.execute(
                    text(
                        "SELECT version, action, from_state, to_state, actor, note, created_at "
                        "FROM federation_delegation_history WHERE workspace_id = CAST(:w AS uuid) "
                        "AND federation_job_id = :j ORDER BY version"
                    ),
                    {"w": str(workspace_id), "j": job_id},
                )
            )
            .mappings()
            .all()
        )
        receipts = (
            (
                await platform.execute(
                    text(
                        "SELECT result_version, metering_source, confidence, usage, "
                        "price_snapshot_id, "
                        "proof_ref, disputed FROM federation_cost_receipts WHERE workspace_id = "
                        "CAST(:w AS uuid) AND federation_job_id = :j ORDER BY result_version"
                    ),
                    {"w": str(workspace_id), "j": job_id},
                )
            )
            .mappings()
            .all()
        )
    return {
        "delegation": _out(row),
        "history": [{**dict(h), "created_at": h["created_at"].isoformat()} for h in history],
        "cost_receipts": [dict(r) for r in receipts],
    }


# ── inbound ───────────────────────────────────────────────────────────────


async def receive(workspace_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
    local_fid = await _local_fid(workspace_id)
    verified = await verify_envelope(
        envelope,
        local_workspace_fid=local_fid,
        key_lookup=identity.key_lookup,
        replay_check=identity.replay_check_for(workspace_id),
    )
    if parse_federation_id(verified.issuer)[0] != "workspace":
        raise FederationError("FED_E_TYPE_MISMATCH", "delegations are between workspaces")
    job_id = verified.payload.get("federation_job_id")
    version = verified.payload.get("version")
    if (
        not isinstance(job_id, str)
        or not re.fullmatch(r"fjob_[a-z2-7]{26}", job_id)
        or not isinstance(version, int)
        or isinstance(version, bool)
    ):
        raise FederationError(
            "FED_E_ENVELOPE_INVALID", "federation_job_id and version are required"
        )
    if verified.type == "delegate.offer":
        return await _on_offer(workspace_id, verified, job_id)
    return await _on_transition(workspace_id, verified, job_id, version)


async def _on_offer(workspace_id: str, msg: Verified, job_id: str) -> dict[str, Any]:
    # F-002: without delegate both ways nothing is written.
    connection = await grants.authorize(
        workspace_id,
        msg.issuer,
        "delegate",
        direction="in",
        signed_at=grants.signed_at(msg.envelope),
    )
    frozen = validate_terms(msg.payload.get("terms"))
    try:
        expires = datetime.fromisoformat(msg.payload["offer_expires_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FederationError("FED_E_ENVELOPE_INVALID", "offer_expires_at is required") from exc
    expires = min(expires, _now() + OFFER_TTL)
    async with async_session_factory() as platform:
        inserted = await platform.scalar(
            text(
                "INSERT INTO federation_delegations (workspace_id, tenant_id, federation_job_id, "
                "peer_workspace_fid, connection_id, role, state, version, terms, acceptance_hash, "
                "offer_expires_at) VALUES (CAST(:w AS uuid), '', :j, :p, :c, 'provider', "
                "'OFFERED', 1, "
                "CAST(:t AS jsonb), :h, :x) ON CONFLICT DO NOTHING RETURNING 1"
            ),
            {
                "w": str(workspace_id),
                "j": job_id,
                "p": msg.issuer,
                "c": connection["connection_id"],
                "t": json.dumps(frozen),
                "h": acceptance_hash(frozen),
                "x": expires,
            },
        )
        if inserted is None:
            # F-012: the same delegation again is acknowledged, never duplicated.
            row = await _row(platform, workspace_id, job_id, peer=msg.issuer)
            await platform.commit()
            return {"status": "duplicate", "version": row["version"]}
        row = await _row(platform, workspace_id, job_id, peer=msg.issuer)
        await _history(
            platform,
            row,
            "offer",
            from_state=None,
            actor=f"peer:{msg.issuer}",
            envelope=msg.envelope,
        )
        await platform.commit()
    return {"status": "received", "version": 1}


async def _on_transition(
    workspace_id: str, msg: Verified, job_id: str, version: int
) -> dict[str, Any]:
    action = _ACTION_OF_TYPE.get(msg.type)
    if action is None:
        raise FederationError("FED_E_ENVELOPE_INVALID", "not a delegation object", type=msg.type)
    actor_role, from_states, to_state, _ = TRANSITIONS[action]
    local_role = "requester" if actor_role == "provider" else "provider"
    async with async_session_factory() as platform:
        row = await _row(platform, workspace_id, job_id, peer=msg.issuer, lock=True)
        if row["role"] != local_role:
            raise FederationError("FED_E_JOB_TRANSITION", "this side cannot receive that object")
        if version <= row["version"]:
            # F-008: an older or repeated object is kept out; state never goes back.
            await platform.commit()
            return {"status": "duplicate", "version": row["version"]}
        if version != row["version"] + 1:
            raise FederationError(
                "FED_E_JOB_STALE",
                "an earlier object is missing; sync first",
                have=row["version"],
                got=version,
            )
        if row["state"] not in from_states:
            raise FederationError("FED_E_JOB_TRANSITION", f"cannot {action} in {row['state']}")
        await grants.authorize(
            workspace_id,
            msg.issuer,
            "delegate",
            direction=_direction(local_role),
            signed_at=grants.signed_at(msg.envelope),
        )
        new = {**row, "state": to_state, "version": version}
        if action == "accept" and msg.payload.get("acceptance_hash") != row["acceptance_hash"]:
            # F-015: the peer accepted something other than what was offered.
            raise FederationError(
                "FED_E_ACCEPTANCE_MISMATCH", "the accepted terms differ from the offered terms"
            )
        if action == "submit":
            new["result"] = validate_result(msg.payload.get("result"))
        if action == "revise":
            new["revisions"] = row["revisions"] + 1
        note = msg.payload.get("note") if isinstance(msg.payload.get("note"), str) else None
        await _save(platform, new)
        await _history(
            platform,
            new,
            action,
            from_state=row["state"],
            actor=f"peer:{msg.issuer}",
            envelope=msg.envelope,
            note=note,
        )
        if action == "submit" and new["result"].get("cost_receipt"):
            await _store_receipt(platform, new, new["result"]["cost_receipt"])
        await platform.commit()
    return {"status": "received", "version": version}


__all__ = [
    "TRANSITIONS",
    "acceptance_hash",
    "act",
    "get",
    "list_delegations",
    "new_job_id",
    "offer",
    "receive",
    "scrub",
    "sync",
    "validate_result",
    "validate_terms",
]
