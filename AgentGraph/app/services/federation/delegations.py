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

Undelivered objects are retried by ``delivery_loop`` with backoff (30 s, 2 min,
10 min, then hourly) and dead-lettered after ``MAX_DELIVERY_ATTEMPTS`` or once
the envelope has expired; a person can then ``sync(..., resign=True)``, which
signs the same queued payloads again (the receiver still de-duplicates by
version).

Cancellation (F-08c) is a request flag on both copies, not a step in the
version chain, so it can never race the provider's own transitions. The
provider confirms with a versioned ``cancel_ack``; until then the work may
still deliver (``drain``), and after ``CANCEL_ACK_TIMEOUT`` the requester's view
says the stop is not confirmed and work may continue.

P1 (ICD placeholders): ``cancellation_policy`` is ``drain`` or ``hard_cancel``
per delegation until the owner picks one; ``budget_cap`` is null, zero, or a
resource cap (model tokens, tool calls, minutes; never money, so no
settlement); offers expire after 24 hours, at most three revisions, results
travel inline, and only people accept, reject, judge, cancel or confirm a
cancellation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_factory
from app.services.federation import grants, identity
from app.services.federation.canonical import CanonicalError, canonical_bytes, sha256_hex
from app.services.federation.envelope import Verified, verify_envelope
from app.services.federation.errors import FederationError
from app.services.federation.grants import Actor
from app.services.federation.ids import b32, parse_federation_id, require_type

_log = logging.getLogger("agentgraph.federation")

OFFER_TTL = timedelta(hours=24)  # P1 placeholder
ENVELOPE_TTL = timedelta(hours=24)  # queued objects stay deliverable this long
RETRY_BACKOFF = (30, 120, 600, 3600)  # seconds; the last one repeats
MAX_DELIVERY_ATTEMPTS = 8
CANCEL_ACK_TIMEOUT = timedelta(hours=1)  # P1 placeholder
MAX_REVISIONS = 3  # P1 placeholder
# ICD_I_BRIDGE §2: until the owner decides (vision §5.3 TBD) both stay
# available per delegation; drain is the default the console offers.
CANCELLATION_POLICIES = ("drain", "hard_cancel")
# F-017: resource caps only; money is not a unit while settlement is off.
USAGE_UNITS = ("model_tokens", "tool_calls", "minutes")
MAX_RESULT_BYTES = 48 * 1024  # stays inside the 64 KiB envelope limit
# When a newly queued object is first due for the delivery loop. The send right
# after the commit normally delivers it first; this is the safety net.
_FIRST_RETRY = f"NOW() + interval '{RETRY_BACKOFF[0]} seconds'"
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
    # Only after the requester asked (cancel_requested_at set on both copies).
    "cancel_ack": (
        "provider",
        ("OFFERED", "ACCEPTED", "RUNNING"),
        "CANCELLED",
        "delegate.cancel_ack",
    ),
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
    if terms["cancellation_policy"] not in CANCELLATION_POLICIES:
        raise FederationError(
            "FED_E_TERMS_INVALID",
            "cancellation_policy is 'drain' or 'hard_cancel'",
            allowed=list(CANCELLATION_POLICIES),
        )
    if not isinstance(terms["external_side_effects"], bool):
        raise FederationError("FED_E_TERMS_INVALID", "external_side_effects is true or false")
    budget = _validate_budget(terms["budget_cap"])
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
        "cancellation_policy": terms["cancellation_policy"],
        "external_side_effects": terms["external_side_effects"],
    }
    scrub(normal)
    return normal


def _validate_budget(budget: Any) -> dict[str, Any] | None:
    """A resource cap, never money (F-017; settlement stays off, ADR-005).

    null or {amount: 0, unit: none}: no cap. {amount: n, unit: model_tokens |
    tool_calls | minutes}: the provider's reported usage of that unit may not
    be exceeded by a new execution. Any other unit (a currency) is refused.
    """
    if budget is None or budget == {"amount": 0, "unit": "none"}:
        return budget
    if (
        isinstance(budget, dict)
        and set(budget) in ({"amount", "unit"}, {"amount", "unit", "mode"})
        and budget.get("mode", "enforce") in ("enforce", "shadow")
        and budget["unit"] in USAGE_UNITS
        and isinstance(budget["amount"], int)
        and not isinstance(budget["amount"], bool)
        and 1 <= budget["amount"] <= 10**12
    ):
        # enforce (default): no new run past the cap. shadow: recorded and
        # flagged, never blocking (F-09).
        return {
            "amount": budget["amount"],
            "unit": budget["unit"],
            "mode": budget.get("mode", "enforce"),
        }
    raise FederationError(
        "FED_E_SETTLEMENT_DISABLED",
        "settlement is not enabled: budget_cap is null, {amount: 0, unit: none}, or a "
        "resource cap {amount: n, unit: model_tokens | tool_calls | minutes}",
        allowed_units=list(USAGE_UNITS),
    )


def _cap(row: dict[str, Any]) -> dict[str, Any] | None:
    budget = (row.get("terms") or {}).get("budget_cap")
    return budget if budget and budget.get("unit") in USAGE_UNITS else None


async def _spent(platform: AsyncSession, row: dict[str, Any], unit: str) -> int:
    """Usage of one unit the provider reported for this delegation, all results."""
    receipts = (
        (
            await platform.execute(
                text(
                    "SELECT usage FROM federation_cost_receipts WHERE workspace_id = "
                    "CAST(:w AS uuid) AND peer_workspace_fid = :p AND federation_job_id = :j"
                ),
                {
                    "w": str(row["workspace_id"]),
                    "p": row["peer_workspace_fid"],
                    "j": row["federation_job_id"],
                },
            )
        )
        .scalars()
        .all()
    )
    total = 0
    for usage in receipts:
        usage = json.loads(usage) if isinstance(usage, str) else usage
        value = (usage or {}).get(unit)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            total += value
    return total


async def _budget_view(platform: AsyncSession, row: dict[str, Any]) -> dict[str, Any] | None:
    from app.services.federation import metering

    return (await metering.summary(platform, row))["budget"]


async def _require_budget_left(platform: AsyncSession, row: dict[str, Any]) -> None:
    view = await _budget_view(platform, row)
    if view is not None and view["blocking"]:
        raise FederationError(
            "FED_E_BUDGET_EXCEEDED",
            "the resource cap is used up: no new execution; accept, dispute or cancel",
            unit=view["unit"],
            cap=view["cap"],
            spent=view["spent"],
        )


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


def _state(row: dict[str, Any]) -> str:
    """The state as of now. An offer past its deadline reads EXPIRED.

    Expiry is not stored and takes no version: each side reaches it on its own
    clock, without an envelope, so a version step for it would make the two
    copies disagree about what the next version means (an accept sent before
    the deadline but delivered after it was dropped as a duplicate).
    """
    if row["state"] == "OFFERED" and row["offer_expires_at"] <= _now():
        return "EXPIRED"
    return row["state"]


def _out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "federation_job_id": row["federation_job_id"],
        "peer_workspace": row["peer_workspace_fid"],
        "connection_id": row["connection_id"],
        "role": row["role"],
        "state": _state(row),
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
        "delivery": {
            "attempts": row.get("delivery_attempts") or 0,
            "next_retry_at": _iso(row.get("next_retry_at")),
            "last_error": row.get("last_delivery_error"),
            # Needs a person: retries stopped (ICD_I_MSG §4.1).
            "dead_letter": row.get("dead_lettered_at") is not None,
        },
        "cancel": _cancel_view(row),
        "offer_expires_at": row["offer_expires_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _cancel_view(row: dict[str, Any]) -> dict[str, Any] | None:
    requested = row.get("cancel_requested_at")
    if requested is None:
        return None
    open_ = _state(row) not in TERMINAL
    return {
        "requested_at": requested.isoformat(),
        "requested_by": row.get("cancel_requested_by"),
        "reason": row.get("cancel_reason"),
        # F-016: never claim the work stopped before the provider says so.
        "work_may_continue": open_,
        "stop_not_confirmed": open_ and requested + CANCEL_ACK_TIMEOUT <= _now(),
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
    # Anything queued gets a retry time in the same transaction, so the delivery
    # loop finds it even if the send that follows the commit never runs.
    await platform.execute(
        text(
            "UPDATE federation_delegations SET state = :s, version = :v, local_task_key = :k, "
            "result = CAST(:r AS jsonb), revisions = :rev, outbox = CAST(:o AS jsonb), "
            "next_retry_at = CASE WHEN jsonb_array_length(CAST(:o AS jsonb)) > 0 "
            f"THEN COALESCE(next_retry_at, {_FIRST_RETRY}) ELSE next_retry_at END, "
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


def _envelope_expired(envelope: dict[str, Any]) -> bool:
    try:
        expires = datetime.strptime(envelope["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, TypeError, ValueError):
        return True
    return expires.replace(tzinfo=timezone.utc) <= _now()


async def _resign(workspace_id: str, outbox: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same queued payloads, signed again (a person's explicit retry)."""
    local_fid = await _local_fid(workspace_id)
    fresh = []
    for envelope in outbox:
        if envelope.get("issuer") != local_fid:
            raise FederationError("FED_E_ENVELOPE_INVALID", "queued object is not ours")
        fresh.append(
            await identity.sign_as(
                local_fid,
                type=envelope["type"],
                audience=envelope["audience"],
                payload=envelope["payload"],
                ttl=ENVELOPE_TTL,
            )
        )
    return fresh


async def sync(workspace_id: str, job_id: str, *, resign: bool = False) -> dict[str, Any]:
    """Deliver queued envelopes in order; stop at the first one that fails.

    A failure schedules the next automatic attempt with backoff; too many
    failures, or an expired envelope, dead-letter the delegation until a person
    syncs it with ``resign=True``.
    """
    delivered = 0
    last_error = None
    if resign:
        async with async_session_factory() as platform:
            row = await _row(platform, workspace_id, job_id, lock=True)
            fresh = await _resign(workspace_id, list(row["outbox"] or []))
            await platform.execute(
                text(
                    "UPDATE federation_delegations SET outbox = CAST(:o AS jsonb), "
                    "delivery_attempts = 0, dead_lettered_at = NULL, next_retry_at = NULL, "
                    "updated_at = NOW() WHERE workspace_id = CAST(:w AS uuid) "
                    "AND peer_workspace_fid = :p AND federation_job_id = :j"
                ),
                {
                    "o": json.dumps(fresh),
                    "w": str(workspace_id),
                    "p": row["peer_workspace_fid"],
                    "j": job_id,
                },
            )
            await platform.commit()
    while True:
        # Read the head of the queue, then let go of the row before sending: the
        # receiver locks its own copy, and a sender holding its row across the
        # send deadlocks with the peer sending the other way (ICD_I_BRIDGE §5).
        async with async_session_factory() as platform:
            row = await _row(platform, workspace_id, job_id, lock=True)
            outbox = list(row["outbox"] or [])
            where = {"w": str(workspace_id), "p": row["peer_workspace_fid"], "j": job_id}
            if not outbox:
                await platform.execute(
                    text(
                        "UPDATE federation_delegations SET delivery_attempts = 0, "
                        "next_retry_at = NULL, last_delivery_error = NULL, "
                        "dead_lettered_at = NULL WHERE workspace_id = CAST(:w AS uuid) "
                        "AND peer_workspace_fid = :p AND federation_job_id = :j"
                    ),
                    where,
                )
                await platform.commit()
                break
            await platform.commit()
        head = outbox[0]
        head_hash = sha256_hex(head)
        error: str | None = None
        try:
            if _envelope_expired(head):
                raise FederationError("FED_E_EXPIRED", "the queued object has expired")
            await grants._transport.deliver(head)
        except FederationError as exc:
            error = exc.code
        except Exception:  # noqa: BLE001 - a failed send is retried, not lost
            _log.exception("delivering %s for %s failed", head.get("type"), job_id)
            error = "FED_E_UNREACHABLE"
        async with async_session_factory() as platform:
            row = await _row(platform, workspace_id, job_id, lock=True)
            outbox = list(row["outbox"] or [])
            if not outbox or sha256_hex(outbox[0]) != head_hash:
                # Another sync moved the queue meanwhile; read it again.
                await platform.commit()
                if error is not None:
                    last_error = error
                    break
                continue
            if error is not None:
                last_error = error
                attempts = (row.get("delivery_attempts") or 0) + 1
                dead = error == "FED_E_EXPIRED" or attempts >= MAX_DELIVERY_ATTEMPTS
                wait = RETRY_BACKOFF[min(attempts, len(RETRY_BACKOFF)) - 1]
                await platform.execute(
                    text(
                        "UPDATE federation_delegations SET delivery_attempts = :n, "
                        "last_delivery_error = :e, next_retry_at = :next, "
                        "dead_lettered_at = CASE WHEN :dead THEN COALESCE(dead_lettered_at, "
                        "NOW()) ELSE NULL END WHERE workspace_id = CAST(:w AS uuid) "
                        "AND peer_workspace_fid = :p AND federation_job_id = :j"
                    ),
                    {
                        **where,
                        "n": attempts,
                        "e": error,
                        "dead": dead,
                        "next": None if dead else _now() + timedelta(seconds=wait),
                    },
                )
                await platform.commit()
                break
            await platform.execute(
                text(
                    "UPDATE federation_delegations SET outbox = CAST(:o AS jsonb), "
                    "delivery_attempts = 0, last_delivery_error = NULL, updated_at = NOW() "
                    "WHERE workspace_id = CAST(:w AS uuid) AND peer_workspace_fid = :p "
                    "AND federation_job_id = :j"
                ),
                {**where, "o": json.dumps(outbox[1:])},
            )
            await platform.commit()
            delivered += 1
    async with async_session_factory() as platform:
        row = await _row(platform, workspace_id, job_id)
    return {"delegation": _out(row), "delivered": delivered, "last_error": last_error}


async def deliver_due(limit: int = 50) -> int:
    """One pass of automatic retries: queued, due and not dead-lettered."""
    async with async_session_factory() as platform:
        due = (
            await platform.execute(
                text(
                    "SELECT workspace_id, federation_job_id FROM federation_delegations "
                    "WHERE dead_lettered_at IS NULL AND next_retry_at IS NOT NULL "
                    "AND next_retry_at <= NOW() AND outbox <> '[]'::jsonb "
                    "ORDER BY next_retry_at LIMIT :n"
                ),
                {"n": limit},
            )
        ).all()
    for workspace_id, job_id in due:
        try:
            await sync(str(workspace_id), job_id)
        except FederationError:
            continue
    return len(due)


async def delivery_loop() -> None:
    """Background retries (started by the app only while federation is on)."""
    import asyncio
    import logging

    from app.services import federation

    log = logging.getLogger("agentgraph.federation")
    while True:
        try:
            if federation.is_enabled():
                from app.services.federation import governance, messaging, skill_updates

                await deliver_due()
                await messaging.deliver_due()
                await governance.expire_due()
                await skill_updates.apply_followed()
                from app.services.federation import reputation

                await reputation.issue_due()
                from app.services.federation import market

                await market.advance()
                from app.services.federation import arena, rights

                await rights.advance()  # no-op unless resource rights are on
                await arena.advance()  # no-op unless the arena is on
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must survive one bad pass
            log.exception("federation delivery pass failed")
        await asyncio.sleep(get_settings().federation_delivery_interval_seconds)


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
        ttl=ENVELOPE_TTL,
    )
    row["outbox"] = [envelope]
    async with async_session_factory() as platform:
        await platform.execute(
            text(
                "INSERT INTO federation_delegations (workspace_id, tenant_id, federation_job_id, "
                "peer_workspace_fid, connection_id, role, state, version, terms, acceptance_hash, "
                "local_task_key, outbox, offer_expires_at, next_retry_at) VALUES "
                "(CAST(:w AS uuid), '', :j, :p, :c, "
                "'requester', 'OFFERED', 1, CAST(:t AS jsonb), :h, :k, CAST(:o AS jsonb), :x, "
                f"{_FIRST_RETRY})"
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
    cancel_local_task: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """accept / reject / submit / cancel_ack (provider) and accept_result /
    revise / dispute (requester).

    Accepting creates the provider's own task through ``create_local_task``
    and confirming a cancellation cancels it through ``cancel_local_task``;
    the route layer runs both with the provider person's own credentials.
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
        if row["role"] != role:
            raise FederationError("FED_E_JOB_TRANSITION", f"only the {role} can {action}")
        state = _state(row)
        if state not in from_states:
            raise FederationError(
                "FED_E_JOB_TRANSITION", f"cannot {action} in {state}", state=state
            )
        if action == "revise" and row["revisions"] >= MAX_REVISIONS:
            raise FederationError("FED_E_JOB_TRANSITION", "revision limit reached; dispute instead")
        cancel_asked = row.get("cancel_requested_at") is not None
        if action == "cancel_ack" and not cancel_asked:
            raise FederationError("FED_E_JOB_TRANSITION", "nobody asked to cancel this delegation")
        if action == "accept" and cancel_asked:
            # The requester withdrew the offer before it was taken on.
            raise FederationError("FED_E_JOB_TRANSITION", "cancellation requested; confirm it")
        if (
            action == "submit"
            and cancel_asked
            and row["terms"].get("cancellation_policy") == "hard_cancel"
        ):
            # hard_cancel: once asked, stop; only the confirmation may follow.
            raise FederationError(
                "FED_E_JOB_TRANSITION", "hard cancel requested: stop and confirm the cancellation"
            )
        if action == "revise":
            # F-017: a revision is a new execution; none past the resource cap.
            await _require_budget_left(platform, row)
        await grants.authorize(
            workspace_id, row["peer_workspace_fid"], "delegate", direction=_direction(role)
        )
        if action == "submit":
            from app.services.federation import metering

            receipt = await metering.receipt_for_delivery(
                platform, row, result.pop("cost_receipt", None)
            )
            if receipt is not None:
                result["cost_receipt"] = receipt
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
        if action == "cancel_ack" and row["local_task_key"] and cancel_local_task is not None:
            await cancel_local_task(row)
        envelope = await identity.sign_as(
            local_fid,
            type=envelope_type,
            audience=row["peer_workspace_fid"],
            payload=payload,
            ttl=ENVELOPE_TTL,
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
        if action == "submit":
            if result.get("cost_receipt"):
                await _store_receipt(platform, new, result["cost_receipt"])
            await metering.close_run(platform, new, new["version"])
        await platform.commit()
    return await sync(workspace_id, job_id)


async def request_cancel(
    workspace_id: str, job_id: str, *, actor: Actor, reason: str
) -> dict[str, Any]:
    """The requester's person asks the provider to stop (F-08c, policy 'drain').

    Not a version step: the provider may still deliver what it has, and only
    its versioned ``cancel_ack`` ends the delegation as CANCELLED.
    """
    if actor.kind != "user":
        raise FederationError("FED_E_APPROVAL_NOT_HUMAN", "a person asks to cancel")
    if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 1000:
        raise FederationError("FED_E_TERMS_INVALID", "a reason of 3-1000 characters is required")
    scrub(reason)
    local_fid = await _local_fid(workspace_id)
    async with async_session_factory() as platform:
        row = await _row(platform, workspace_id, job_id, lock=True)
        if row["role"] != "requester":
            raise FederationError("FED_E_JOB_TRANSITION", "only the requester asks to cancel")
        if _state(row) in TERMINAL:
            raise FederationError("FED_E_JOB_TRANSITION", f"already {_state(row)}")
        if row.get("cancel_requested_at") is not None:
            raise FederationError("FED_E_JOB_TRANSITION", "cancellation already requested")
        envelope = await identity.sign_as(
            local_fid,
            type="delegate.cancel",
            audience=row["peer_workspace_fid"],
            payload={
                "federation_job_id": job_id,
                "version": row["version"],
                "reason": reason.strip(),
            },
            ttl=ENVELOPE_TTL,
        )
        await platform.execute(
            text(
                "UPDATE federation_delegations SET cancel_requested_at = NOW(), "
                "cancel_requested_by = :who, cancel_reason = :r, "
                "outbox = outbox || CAST(:e AS jsonb), "
                f"next_retry_at = COALESCE(next_retry_at, {_FIRST_RETRY}), updated_at = NOW() "
                "WHERE workspace_id = CAST(:w AS uuid) AND peer_workspace_fid = :p "
                "AND federation_job_id = :j"
            ),
            {
                "who": actor.ref,
                "r": reason.strip(),
                "e": json.dumps([envelope]),
                "w": str(workspace_id),
                "p": row["peer_workspace_fid"],
                "j": job_id,
            },
        )
        await platform.commit()
    return await sync(workspace_id, job_id)


async def _on_cancel(workspace_id: str, msg: Verified, job_id: str) -> dict[str, Any]:
    reason = msg.payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise FederationError("FED_E_ENVELOPE_INVALID", "reason is required")
    async with async_session_factory() as platform:
        row = await _row(platform, workspace_id, job_id, peer=msg.issuer, lock=True)
        if row["role"] != "provider":
            raise FederationError("FED_E_JOB_TRANSITION", "this side cannot receive that object")
        await grants.authorize(
            workspace_id,
            msg.issuer,
            "delegate",
            direction="in",
            signed_at=grants.signed_at(msg.envelope),
        )
        if _state(row) in TERMINAL or row.get("cancel_requested_at") is not None:
            await platform.commit()
            return {"status": "duplicate", "version": row["version"]}
        await platform.execute(
            text(
                "UPDATE federation_delegations SET cancel_requested_at = NOW(), "
                "cancel_requested_by = :who, cancel_reason = :r, updated_at = NOW() "
                "WHERE workspace_id = CAST(:w AS uuid) AND peer_workspace_fid = :p "
                "AND federation_job_id = :j"
            ),
            {
                "who": f"peer:{msg.issuer}",
                "r": reason.strip()[:1000],
                "w": str(workspace_id),
                "p": msg.issuer,
                "j": job_id,
            },
        )
        await platform.commit()
    return {"status": "received", "version": row["version"]}


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
        out = []
        for raw in rows:
            row = _loads(raw)
            from app.services.federation import metering

            used = await metering.summary(platform, row)
            out.append({**_out(row), "budget": used["budget"], "usage": used["usage"]})
    return out


async def get(workspace_id: str, job_id: str) -> dict[str, Any]:
    async with async_session_factory() as platform:
        row = await _row(platform, workspace_id, job_id)
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
        from app.services.federation import metering

        usage = await metering.summary(platform, row)
    return {
        "delegation": {**_out(row), "budget": usage["budget"]},
        # F-09: what was used, who measured it, the cap mode and the evidence.
        "metering": usage,
        "history": [{**dict(h), "created_at": h["created_at"].isoformat()} for h in history],
        # Each receipt says who measured the usage and how sure that is;
        # self-reported usage is the provider's own claim (vision §8.3).
        "cost_receipts": [dict(r) for r in receipts],
    }


# ── inbound ───────────────────────────────────────────────────────────────


async def receive(workspace_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
    local_fid = await _local_fid(workspace_id)
    try:
        verified = await verify_envelope(
            envelope,
            local_workspace_fid=local_fid,
            key_lookup=identity.key_lookup,
            replay_check=identity.replay_check_for(workspace_id),
        )
    except FederationError as exc:
        if exc.code != "FED_E_REPLAY":
            raise
        # The sender is retrying the very same envelope (the guard runs last, so
        # it verified). Refusing it as a replay left the sender's queue stuck
        # behind an object the first try had applied but whose answer was lost.
        # Every delegate.* handler is idempotent (an offer by job id, a cancel
        # request by its flag, a transition by version), so let it answer the
        # repeat as a duplicate, or apply it if the first try never got there.
        verified = await verify_envelope(
            envelope,
            local_workspace_fid=local_fid,
            key_lookup=identity.key_lookup,
            replay_check=identity.accept_recorded,
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
    if verified.type == "delegate.cancel":
        return await _on_cancel(workspace_id, verified, job_id)
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
    if expires.tzinfo is None:
        raise FederationError("FED_E_ENVELOPE_INVALID", "offer_expires_at needs a time zone")
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
            row = await _row(platform, workspace_id, job_id)
            await platform.commit()
            if row["peer_workspace_fid"] != msg.issuer:
                # A job id names one delegation in this workspace, whoever sent it.
                raise FederationError(
                    "FED_E_ENVELOPE_INVALID", "federation_job_id is already in use"
                )
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
        if action == "accept":
            # Expiry is judged by when the provider signed, not by when the
            # object arrived: an accept sent in time but delivered late counts.
            skew = timedelta(seconds=get_settings().federation_clock_skew_seconds)
            if grants.signed_at(msg.envelope) > row["offer_expires_at"] + skew:
                raise FederationError(
                    "FED_E_JOB_TRANSITION", "the offer had expired when it was accepted"
                )
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
            # The provider checks its own copy: no new execution past the cap.
            await _require_budget_left(platform, row)
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
    "deliver_due",
    "delivery_loop",
    "get",
    "list_delegations",
    "new_job_id",
    "offer",
    "receive",
    "request_cancel",
    "scrub",
    "sync",
    "validate_result",
    "validate_terms",
]
