"""Simulated resource rights (P6 R1): specs, vouchers, reservations, DEMO_M.

Contract: docs/federation/ICD_P6_RESOURCE_RIGHTS_ARENA.md §2-§4.

NO ECONOMIC VALUE. DEMO_M is a demonstration resource right: it cannot be
bought, withdrawn, exchanged for SIM (or anything else), or used to pay a real
third-party model. Its ledger is two tables of its own; nothing here reads or
writes the SIM ledger, and no function converts between units.

* A **spec** is what a workspace declares it can serve. Only MOCK_DEMO backs
  vouchers. A SELF_REPORTED_SUBSCRIPTION is display-only (never escrowed);
  CONTROLLED_PREPAID and AUTHORIZED_PROVIDER_SERVICE wait for the R4 owner
  gate and are refused.
* A **voucher** is a bounded right of one workspace (the beneficiary) to ask
  another (the issuer) for work. It carries no account, key or tool of the
  issuer: work runs only through the existing delegation bridge, on the
  issuer's side, with the issuer's own resources.
* A **reservation** binds part of a voucher to one delegation whose enforced
  ``model_tokens`` cap equals the reservation, so the provider cannot run past
  it. When the delegation ends, the metered usage is consumed (at most the
  reservation) and the rest goes back to the voucher.

Amounts are integers in tokens; 1 DEMO_M = 1,000,000 tokens.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_factory
from app.services import federation
from app.services.federation.canonical import sha256_hex
from app.services.federation.errors import FederationError
from app.services.federation.grants import Actor
from app.services.federation.ids import b32, require_type

UNIT = "DEMO_M"
TOKENS_PER_M = 1_000_000
NOTICE = (
    "DEMO_M is a simulated resource right with no economic value: it cannot be bought, "
    "withdrawn, exchanged for SIM or money, or used to pay a real third-party model"
)
CLASSES = (
    "MOCK_DEMO",
    "SELF_REPORTED_SUBSCRIPTION",
    "CONTROLLED_PREPAID",
    "AUTHORIZED_PROVIDER_SERVICE",
)
GATED_CLASSES = ("CONTROLLED_PREPAID", "AUTHORIZED_PROVIDER_SERVICE")  # R4 owner gate
MAX_VOUCHER = 100 * TOKENS_PER_M
MAX_EXPIRY_DAYS = 365
# Delegation states a reservation may attach to: the work has not ended.
OPEN_JOB_STATES = ("OFFERED", "ACCEPTED", "RUNNING")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_" + b32(secrets.token_bytes(16))[:26]


def as_m(tokens: int) -> str:
    """Display only: 2500000 -> '2.5'."""
    whole, rest = divmod(int(tokens), TOKENS_PER_M)
    return f"{whole}.{rest:06d}".rstrip("0").rstrip(".") if rest else str(whole)


def enabled() -> bool:
    settings = get_settings()
    return bool(federation.is_enabled() and settings.federation_resource_rights_enabled)


def require_enabled() -> None:
    if not enabled():
        raise FederationError("FED_E_RR_DISABLED", "resource rights are off on this deployment")


def _person(actor: Actor, what: str, *, owner: bool = False) -> None:
    if actor.kind != "user":
        raise FederationError("FED_E_APPROVAL_NOT_HUMAN", f"a person {what}")
    if owner and not actor.is_owner:
        raise FederationError("FED_E_APPROVAL_NOT_HUMAN", f"an owner {what}")


def _tokens(value: Any, name: str = "amount", *, most: int = MAX_VOUCHER) -> int:
    """Integers only: no floats, no strings, no implicit conversion."""
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= most:
        raise FederationError(
            "FED_E_PACKAGE_INVALID", f"{name} is an integer number of tokens, 1 to {most}"
        )
    return value


def check_unit(unit: Any) -> None:
    if unit is not None and unit != UNIT:
        raise FederationError(
            "FED_E_UNIT_MISMATCH",
            "only DEMO_M is accepted here; there is no conversion from SIM, ARENA_CHIP "
            "or any real quota",
            unit=str(unit)[:20],
        )


async def fid_of(session: AsyncSession, workspace_id: str) -> str:
    from app.services.federation import grants

    return await grants._local_fid(session, workspace_id)


# ── the DEMO_M ledger ─────────────────────────────────────────────────────


def cap(spec_id: str) -> str:
    return f"cap:{spec_id}"


def used(spec_id: str) -> str:
    return f"used:{spec_id}"


def voucher_account(voucher_id: str) -> str:
    return f"voucher:{voucher_id}"


def resv(reservation_id: str) -> str:
    return f"resv:{reservation_id}"


def stake(match_id: str, seat: int) -> str:
    return f"stake:{match_id}:{seat}"


async def lock(session: AsyncSession, account: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('rr:' || :a))"), {"a": account}
    )


async def balance(session: AsyncSession, account: str) -> int:
    return int(
        await session.scalar(
            text("SELECT COALESCE(sum(amount), 0) FROM federation_rr_entries WHERE account = :a"),
            {"a": account},
        )
        or 0
    )


async def post(
    session: AsyncSession,
    *,
    kind: str,
    entries: list[tuple[str, int]],
    reference: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[str, bool]:
    """One balanced DEMO_M transaction in the caller's database transaction.
    Returns (txn_id, created); a repeated idempotency key returns the first."""
    entries = [(a, int(n)) for a, n in entries if int(n) != 0]
    if not entries or sum(n for _, n in entries) != 0:
        raise FederationError("FED_E_PACKAGE_INVALID", "a DEMO_M transaction must balance")
    if idempotency_key:
        existing = await session.scalar(
            text("SELECT txn_id FROM federation_rr_txns WHERE idempotency_key = :k"),
            {"k": idempotency_key},
        )
        if existing:
            return existing, False
    txn_id = new_id("frx")
    await session.execute(
        text(
            "INSERT INTO federation_rr_txns (txn_id, kind, reference, idempotency_key) "
            "VALUES (:t, :k, :r, :i)"
        ),
        {"t": txn_id, "k": kind, "r": reference, "i": idempotency_key},
    )
    for account, amount in entries:
        await session.execute(
            text("INSERT INTO federation_rr_entries (txn_id, account, amount) VALUES (:t, :a, :m)"),
            {"t": txn_id, "a": account, "m": amount},
        )
    return txn_id, True


async def is_balanced() -> dict[str, Any]:
    async with async_session_factory() as session:
        unbalanced = await session.scalar(
            text(
                "SELECT count(*) FROM (SELECT txn_id FROM federation_rr_entries "
                "GROUP BY txn_id HAVING sum(amount) <> 0) x"
            )
        )
        total = await session.scalar(
            text("SELECT COALESCE(sum(amount), 0) FROM federation_rr_entries")
        )
        negative = await session.scalar(
            text(
                "SELECT count(*) FROM (SELECT account FROM federation_rr_entries WHERE "
                "account <> 'demo_mint' GROUP BY account HAVING sum(amount) < 0) x"
            )
        )
    return {
        "unit": UNIT,
        "unbalanced_transactions": int(unbalanced or 0),
        "total": int(total or 0),
        "negative_accounts": int(negative or 0),
        "ok": not unbalanced and not total and not negative,
    }


# ── specs ─────────────────────────────────────────────────────────────────


def _spec_out(row: dict[str, Any], capacity: int | None = None) -> dict[str, Any]:
    out = {
        "spec_id": row["spec_id"],
        "provider": row["provider_fid"],
        "resource_class": row["resource_class"],
        "escrowable": row["escrowable"],
        "unit": row["unit"],
        "model_family": row["model_family"],
        "allowed_use": row["allowed_use"],
        "license_basis_ref": row["license_basis_ref"],
        "measured_source": row["measured_source"],
        "confidence": row["confidence"],
        "declared_tokens": int(row["declared_tokens"]),
        "max_concurrency": row["max_concurrency"],
        "revocation_policy": row["revocation_policy"],
        "fulfilment_policy": row["fulfilment_policy"],
        "spec_hash": row["spec_hash"],
        "state": row["state"],
        "expires_at": row["expires_at"].isoformat(),
        "simulated": True,
    }
    if capacity is not None:
        out["unreserved_tokens"] = capacity
    if not row["escrowable"]:
        out["warning"] = "self-reported and unverified: shown only, never escrowed or staked"
    return out


async def spec_row(session: AsyncSession, spec_id: str, *, lock_row: bool = False) -> dict:
    row = (
        (
            await session.execute(
                text(
                    "SELECT * FROM federation_rr_specs WHERE spec_id = :s"
                    + (" FOR UPDATE" if lock_row else "")
                ),
                {"s": spec_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise FederationError("FED_E_RESOURCE_UNKNOWN", "no such resource spec")
    return dict(row)


def require_escrowable(spec: dict[str, Any]) -> None:
    if not spec["escrowable"] or spec["resource_class"] != "MOCK_DEMO":
        raise FederationError(
            "FED_E_RESOURCE_NOT_ESCROWABLE",
            "only simulated (MOCK_DEMO) capacity can back a voucher or a stake; a "
            "self-reported subscription never can",
            resource_class=spec["resource_class"],
        )
    if spec["state"] != "ACTIVE" or spec["expires_at"] <= _now():
        raise FederationError("FED_E_RESOURCE_UNKNOWN", "the resource spec is not active")


async def declare_spec(
    workspace_id: str,
    *,
    resource_class: str,
    model_family: str,
    allowed_use: str,
    declared_tokens: int,
    measured_source: str,
    confidence: str = "low",
    license_basis_ref: str | None = None,
    max_concurrency: int = 1,
    revocation_policy: str = "unused capacity returns to the provider; issued vouchers stay",
    fulfilment_policy: str = "work runs through the federation delegation bridge only",
    expires_days: int = 90,
    actor: Actor,
) -> dict[str, Any]:
    """An owner declares what this workspace can serve."""
    require_enabled()
    _person(actor, "declares a resource", owner=True)
    if resource_class not in CLASSES:
        raise FederationError("FED_E_PACKAGE_INVALID", "unknown resource class")
    if resource_class in GATED_CLASSES:
        raise FederationError(
            "FED_E_NOT_AVAILABLE",
            "real resource classes wait for the R4 owner and legal gate",
            resource_class=resource_class,
        )
    declared = _tokens(declared_tokens, "declared_tokens", most=10**10)
    if resource_class == "MOCK_DEMO" and measured_source != "mock":
        raise FederationError("FED_E_PACKAGE_INVALID", "simulated capacity is measured 'mock'")
    if resource_class == "SELF_REPORTED_SUBSCRIPTION" and measured_source != "self_reported":
        raise FederationError(
            "FED_E_PACKAGE_INVALID", "a subscription's remaining quota is only self_reported"
        )
    if confidence not in ("low", "medium", "high"):
        raise FederationError("FED_E_PACKAGE_INVALID", "confidence is low, medium or high")
    if not 1 <= int(expires_days) <= MAX_EXPIRY_DAYS:
        raise FederationError("FED_E_PACKAGE_INVALID", "a spec lasts 1 to 365 days")
    from app.services.federation import delegations

    for name, value in (
        ("model_family", model_family),
        ("allowed_use", allowed_use),
        ("license_basis_ref", license_basis_ref),
        ("revocation_policy", revocation_policy),
        ("fulfilment_policy", fulfilment_policy),
    ):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise FederationError("FED_E_PACKAGE_INVALID", f"{name} is text")
    # No credential or account handle travels in a declaration (RR-05).
    delegations.scrub(
        {
            "model_family": model_family,
            "allowed_use": allowed_use,
            "license_basis_ref": license_basis_ref,
            "revocation_policy": revocation_policy,
            "fulfilment_policy": fulfilment_policy,
        }
    )
    async with async_session_factory() as session:
        me = await fid_of(session, workspace_id)
        await lock(session, f"specs:{me}")
        escrowable = resource_class == "MOCK_DEMO"
        if escrowable:
            already = int(
                await session.scalar(
                    text(
                        "SELECT COALESCE(sum(declared_tokens), 0) FROM federation_rr_specs "
                        "WHERE provider_fid = :f AND resource_class = 'MOCK_DEMO'"
                    ),
                    {"f": me},
                )
                or 0
            )
            allowance = get_settings().federation_demo_m_allowance_tokens
            if already + declared > allowance:
                raise FederationError(
                    "FED_E_CAPACITY_EXCEEDED",
                    "past this workspace's simulated capacity allowance",
                    allowance=allowance,
                    declared=already,
                )
        spec_id = new_id("frs")
        expires_at = _now() + timedelta(days=int(expires_days))
        terms = {
            "provider": me,
            "resource_class": resource_class,
            "unit": UNIT,
            "model_family": model_family.strip(),
            "allowed_use": allowed_use.strip(),
            "license_basis_ref": license_basis_ref,
            "measured_source": measured_source,
            "confidence": confidence,
            "declared_tokens": declared,
            "max_concurrency": int(max_concurrency),
            "revocation_policy": revocation_policy,
            "fulfilment_policy": fulfilment_policy,
            "expires_at": expires_at.isoformat(),
        }
        await session.execute(
            text(
                "INSERT INTO federation_rr_specs (spec_id, provider_fid, resource_class, "
                "escrowable, model_family, allowed_use, license_basis_ref, measured_source, "
                "confidence, declared_tokens, max_concurrency, revocation_policy, "
                "fulfilment_policy, spec_hash, expires_at, created_by) VALUES (:id, :p, :c, :e, "
                ":mf, :au, :lb, :ms, :cf, :d, :mc, :rp, :fp, :h, :x, :by)"
            ),
            {
                "id": spec_id,
                "p": me,
                "c": resource_class,
                "e": escrowable,
                "mf": terms["model_family"],
                "au": terms["allowed_use"],
                "lb": license_basis_ref,
                "ms": measured_source,
                "cf": confidence,
                "d": declared,
                "mc": int(max_concurrency),
                "rp": revocation_policy,
                "fp": fulfilment_policy,
                "h": sha256_hex(terms),
                "x": expires_at,
                "by": actor.ref,
            },
        )
        if escrowable:
            await post(
                session,
                kind="capacity",
                entries=[("demo_mint", -declared), (cap(spec_id), declared)],
                reference=spec_id,
                idempotency_key=f"capacity:{spec_id}",
            )
        await session.commit()
        row = await spec_row(session, spec_id)
        capacity = await balance(session, cap(spec_id)) if escrowable else None
    return _spec_out(row, capacity)


async def revoke_spec(workspace_id: str, spec_id: str, *, actor: Actor) -> dict[str, Any]:
    """No new vouchers or stakes from this spec. Issued vouchers are not
    silently voided: they run to their own end (ICD §3)."""
    require_enabled()
    _person(actor, "revokes a resource", owner=True)
    async with async_session_factory() as session:
        me = await fid_of(session, workspace_id)
        done = await session.scalar(
            text(
                "UPDATE federation_rr_specs SET state = 'REVOKED' WHERE spec_id = :s AND "
                "provider_fid = :m AND state = 'ACTIVE' RETURNING spec_id"
            ),
            {"s": spec_id, "m": me},
        )
        if done is None:
            raise FederationError("FED_E_RESOURCE_UNKNOWN", "no active spec of yours")
        await session.commit()
        row = await spec_row(session, spec_id)
    return _spec_out(row)


async def list_specs(workspace_id: str) -> list[dict[str, Any]]:
    require_enabled()
    async with async_session_factory() as session:
        me = await fid_of(session, workspace_id)
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM federation_rr_specs WHERE provider_fid = :m "
                        "ORDER BY created_at DESC LIMIT 100"
                    ),
                    {"m": me},
                )
            )
            .mappings()
            .all()
        )
        out = []
        for r in rows:
            capacity = await balance(session, cap(r["spec_id"])) if r["escrowable"] else None
            out.append(_spec_out(dict(r), capacity))
    return out


# ── vouchers ──────────────────────────────────────────────────────────────


async def issue(
    session: AsyncSession,
    *,
    spec: dict[str, Any],
    beneficiary: str,
    amount: int,
    origin: str,
    origin_ref: str | None,
    usage_scope: str,
    expires_at: datetime,
    source_account: str,
    idempotency_key: str,
) -> str:
    """Create a voucher funded from ``source_account`` (the spec's capacity or
    an arena stake), inside the caller's transaction. A stake already held is
    awarded even if its spec was revoked since: obligations are not voided."""
    if origin == "grant":
        require_escrowable(spec)
    elif not spec["escrowable"]:
        raise FederationError("FED_E_RESOURCE_NOT_ESCROWABLE", "not simulated capacity")
    if beneficiary == spec["provider_fid"]:
        raise FederationError("FED_E_PACKAGE_INVALID", "a voucher is for another workspace")
    existing = (
        await session.scalar(
            text(
                "SELECT voucher_id FROM federation_rr_vouchers WHERE origin = :o AND "
                "origin_ref = :r AND beneficiary_fid = :b"
            ),
            {"o": origin, "r": origin_ref, "b": beneficiary},
        )
        if origin_ref
        else None
    )
    if existing:
        return existing
    voucher_id = new_id("frv")
    await session.execute(
        text(
            "INSERT INTO federation_rr_vouchers (voucher_id, spec_id, issuer_fid, "
            "beneficiary_fid, origin, origin_ref, face_amount, usage_scope, expires_at) VALUES "
            "(:v, :s, :i, :b, :o, :r, :a, :u, :x)"
        ),
        {
            "v": voucher_id,
            "s": spec["spec_id"],
            "i": spec["provider_fid"],
            "b": beneficiary,
            "o": origin,
            "r": origin_ref,
            "a": amount,
            "u": usage_scope,
            "x": expires_at,
        },
    )
    await post(
        session,
        kind={"grant": "issue", "compensate": "compensate"}.get(origin, "stake_award"),
        entries=[(source_account, -amount), (voucher_account(voucher_id), amount)],
        reference=voucher_id,
        idempotency_key=idempotency_key,
    )
    return voucher_id


async def issue_voucher(
    workspace_id: str,
    *,
    spec_id: str,
    beneficiary: str,
    amount: int,
    usage_scope: str,
    expires_days: int = 30,
    unit: str | None = None,
    actor: Actor,
) -> dict[str, Any]:
    """The issuer's person grants another workspace a simulated right."""
    require_enabled()
    _person(actor, "issues a voucher")
    check_unit(unit)
    amount = _tokens(amount)
    require_type(beneficiary, "workspace")
    if not isinstance(usage_scope, str) or not 5 <= len(usage_scope.strip()) <= 500:
        raise FederationError("FED_E_PACKAGE_INVALID", "usage_scope is 5 to 500 characters")
    if not 1 <= int(expires_days) <= MAX_EXPIRY_DAYS:
        raise FederationError("FED_E_PACKAGE_INVALID", "a voucher lasts 1 to 365 days")
    async with async_session_factory() as session:
        me = await fid_of(session, workspace_id)
        spec = await spec_row(session, spec_id, lock_row=True)
        if spec["provider_fid"] != me:
            raise FederationError("FED_E_RESOURCE_UNKNOWN", "issue from a spec of yours")
        require_escrowable(spec)
        await lock(session, cap(spec_id))
        left = await balance(session, cap(spec_id))
        if left < amount:
            raise FederationError(
                "FED_E_CAPACITY_EXCEEDED", "not enough unreserved capacity", left=left
            )
        voucher_id = await issue(
            session,
            spec=spec,
            beneficiary=beneficiary,
            amount=amount,
            origin="grant",
            origin_ref=None,
            usage_scope=usage_scope.strip(),
            expires_at=min(_now() + timedelta(days=int(expires_days)), spec["expires_at"]),
            source_account=cap(spec_id),
            idempotency_key=f"issue:{secrets.token_hex(8)}",
        )
        await session.commit()
    return await get_voucher(voucher_id)


async def _voucher_row(session: AsyncSession, voucher_id: str, *, lock_row: bool = False) -> dict:
    row = (
        (
            await session.execute(
                text(
                    "SELECT * FROM federation_rr_vouchers WHERE voucher_id = :v"
                    + (" FOR UPDATE" if lock_row else "")
                ),
                {"v": voucher_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise FederationError("FED_E_VOUCHER_UNKNOWN", "no such voucher")
    return dict(row)


async def _voucher_view(session: AsyncSession, row: dict[str, Any]) -> dict[str, Any]:
    reservations = (
        (
            await session.execute(
                text(
                    "SELECT * FROM federation_rr_reservations WHERE voucher_id = :v "
                    "ORDER BY created_at"
                ),
                {"v": row["voucher_id"]},
            )
        )
        .mappings()
        .all()
    )
    available = await balance(session, voucher_account(row["voucher_id"]))
    reserved = sum(int(r["amount"]) for r in reservations if r["state"] == "RESERVED")
    consumed = sum(int(r["consumed"]) for r in reservations)
    return {
        "voucher_id": row["voucher_id"],
        "spec_id": row["spec_id"],
        "issuer": row["issuer_fid"],
        "beneficiary": row["beneficiary_fid"],
        "origin": row["origin"],
        "origin_ref": row["origin_ref"],
        "unit": row["unit"],
        "face_amount": int(row["face_amount"]),
        "reserved_amount": reserved,
        "consumed_amount": consumed,
        "available_amount": available,
        "face_m": as_m(row["face_amount"]),
        "usage_scope": row["usage_scope"],
        "legal_basis_status": row["legal_basis_status"],
        "status": row["status"],
        "revocation_epoch": row["revocation_epoch"],
        "valid_from": row["valid_from"].isoformat(),
        "expires_at": row["expires_at"].isoformat(),
        "reservations": [
            {
                "reservation_id": r["reservation_id"],
                "federation_job_id": r["federation_job_id"],
                "amount": int(r["amount"]),
                "state": r["state"],
                "consumed": int(r["consumed"]),
                "consumed_by_source": _json(r["consumed_by_source"]),
                "delegation_outcome": r["delegation_outcome"],
            }
            for r in reservations
        ],
        "simulated": True,
        "redeemable": False,
        "notice": NOTICE,
    }


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def get_voucher(voucher_id: str) -> dict[str, Any]:
    async with async_session_factory() as session:
        return await _voucher_view(session, await _voucher_row(session, voucher_id))


async def get_voucher_for(workspace_id: str, voucher_id: str) -> dict[str, Any]:
    """Only the issuer and the beneficiary see a voucher (RR-05)."""
    require_enabled()
    async with async_session_factory() as session:
        me = await fid_of(session, workspace_id)
        row = await _voucher_row(session, voucher_id)
        if me not in (row["issuer_fid"], row["beneficiary_fid"]):
            raise FederationError("FED_E_VOUCHER_UNKNOWN", "no such voucher")
        return await _voucher_view(session, row)


async def list_vouchers(workspace_id: str) -> dict[str, Any]:
    require_enabled()
    async with async_session_factory() as session:
        me = await fid_of(session, workspace_id)
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM federation_rr_vouchers WHERE beneficiary_fid = :m OR "
                        "issuer_fid = :m ORDER BY created_at DESC LIMIT 200"
                    ),
                    {"m": me},
                )
            )
            .mappings()
            .all()
        )
        views = [await _voucher_view(session, dict(r)) for r in rows]
    return {
        "held": [v for v in views if v["beneficiary"] == me],
        "issued": [v for v in views if v["issuer"] == me],
        "unit": UNIT,
        "simulated": True,
        "notice": NOTICE,
    }


async def _close_voucher(
    session: AsyncSession, row: dict[str, Any], status: str, kind: str, note: str
) -> None:
    """Return the unused remainder to the issuer's capacity and close."""
    left = await balance(session, voucher_account(row["voucher_id"]))
    if left:
        await post(
            session,
            kind=kind,
            entries=[(voucher_account(row["voucher_id"]), -left), (cap(row["spec_id"]), left)],
            reference=f"{row['voucher_id']}: {note}"[:500],
            idempotency_key=f"{kind}:{row['voucher_id']}",
        )
    await session.execute(
        text(
            "UPDATE federation_rr_vouchers SET status = :s, revocation_epoch = "
            "revocation_epoch + 1, updated_at = NOW() WHERE voucher_id = :v"
        ),
        {"s": status, "v": row["voucher_id"]},
    )


async def _open_reservations(session: AsyncSession, voucher_id: str) -> int:
    return int(
        await session.scalar(
            text(
                "SELECT count(*) FROM federation_rr_reservations WHERE voucher_id = :v AND "
                "state = 'RESERVED'"
            ),
            {"v": voucher_id},
        )
        or 0
    )


async def revoke_voucher(workspace_id: str, voucher_id: str, *, actor: Actor) -> dict[str, Any]:
    """The issuer takes back the unused part of a voucher it granted. An arena
    voucher is an obligation and cannot be revoked by its issuer."""
    require_enabled()
    _person(actor, "revokes a voucher")
    async with async_session_factory() as session:
        me = await fid_of(session, workspace_id)
        row = await _voucher_row(session, voucher_id, lock_row=True)
        if row["issuer_fid"] != me:
            raise FederationError("FED_E_VOUCHER_UNKNOWN", "no such voucher of yours")
        if row["origin"] != "grant":
            raise FederationError(
                "FED_E_VOUCHER_STATE",
                "an arena voucher is an obligation: only its beneficiary may waive it",
            )
        if row["status"] != "ACTIVE":
            raise FederationError("FED_E_VOUCHER_STATE", "the voucher is closed")
        if await _open_reservations(session, voucher_id):
            raise FederationError(
                "FED_E_VOUCHER_STATE", "work is in progress on this voucher: cancel it first"
            )
        await lock(session, voucher_account(voucher_id))
        await _close_voucher(session, row, "REVOKED", "revoke", f"revoked by {actor.ref}")
        await session.commit()
    return await get_voucher(voucher_id)


async def waive_voucher(workspace_id: str, voucher_id: str, *, actor: Actor) -> dict[str, Any]:
    """The beneficiary gives up the unused part."""
    require_enabled()
    _person(actor, "waives a voucher")
    async with async_session_factory() as session:
        me = await fid_of(session, workspace_id)
        row = await _voucher_row(session, voucher_id, lock_row=True)
        if row["beneficiary_fid"] != me:
            raise FederationError("FED_E_VOUCHER_UNKNOWN", "no such voucher of yours")
        if row["status"] != "ACTIVE":
            raise FederationError("FED_E_VOUCHER_STATE", "the voucher is closed")
        if await _open_reservations(session, voucher_id):
            raise FederationError("FED_E_VOUCHER_STATE", "work is in progress on this voucher")
        await lock(session, voucher_account(voucher_id))
        await _close_voucher(session, row, "WAIVED", "waive", f"waived by {actor.ref}")
        await session.commit()
    return await get_voucher(voucher_id)


# ── reservations ──────────────────────────────────────────────────────────


async def reserve(
    workspace_id: str,
    *,
    voucher_id: str,
    federation_job_id: str,
    amount: int,
    idempotency_key: str,
    unit: str | None = None,
    actor: Actor,
) -> dict[str, Any]:
    """The beneficiary binds part of its voucher to a delegation it sent the
    issuer. The delegation's enforced model_tokens cap must equal the
    reservation, so the provider cannot run past it; the connection must
    still allow delegation (a revoked or frozen connection refuses)."""
    require_enabled()
    check_unit(unit)
    amount = _tokens(amount)
    if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128:
        raise FederationError("FED_E_RESERVATION_INVALID", "idempotency_key is 8-128 characters")
    from app.services.federation import delegations, grants

    async with async_session_factory() as session:
        me = await fid_of(session, workspace_id)
        row = await _voucher_row(session, voucher_id)
        if row["beneficiary_fid"] != me:
            raise FederationError("FED_E_VOUCHER_UNKNOWN", "no such voucher of yours")
        replay = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM federation_rr_reservations WHERE voucher_id = :v AND "
                        "idempotency_key = :k"
                    ),
                    {"v": voucher_id, "k": idempotency_key},
                )
            )
            .mappings()
            .first()
        )
    if replay is not None:
        if replay["federation_job_id"] != federation_job_id or int(replay["amount"]) != amount:
            raise FederationError(
                "FED_E_RESERVATION_INVALID", "this idempotency key was used for another request"
            )
        return {
            **(await get_voucher(voucher_id)),
            "reservation_id": replay["reservation_id"],
            "replayed": True,
        }
    # The connection must still allow this workspace to delegate to the issuer.
    await grants.authorize(workspace_id, row["issuer_fid"], "delegate", direction="out")
    async with async_session_factory() as session:
        job = await delegations._row(session, workspace_id, federation_job_id)
        if job["role"] != "requester" or job["peer_workspace_fid"] != row["issuer_fid"]:
            raise FederationError(
                "FED_E_RESERVATION_INVALID", "reserve for a delegation you sent the issuer"
            )
        state = delegations._state(job)
        if state not in OPEN_JOB_STATES:
            raise FederationError(
                "FED_E_RESERVATION_INVALID", "the delegation has ended", state=state
            )
        budget = (job["terms"] or {}).get("budget_cap") or {}
        if (
            budget.get("unit") != "model_tokens"
            or budget.get("mode", "enforce") != "enforce"
            or budget.get("amount") != amount
        ):
            raise FederationError(
                "FED_E_RESERVATION_INVALID",
                "the delegation needs an enforced model_tokens budget_cap equal to the reservation",
                needed={"amount": amount, "unit": "model_tokens", "mode": "enforce"},
            )
        await lock(session, voucher_account(voucher_id))
        row = await _voucher_row(session, voucher_id, lock_row=True)
        if row["status"] != "ACTIVE" or row["expires_at"] <= _now():
            raise FederationError("FED_E_VOUCHER_STATE", "the voucher is not active")
        if await session.scalar(
            text("SELECT 1 FROM federation_rr_reservations WHERE federation_job_id = :j"),
            {"j": federation_job_id},
        ):
            raise FederationError(
                "FED_E_RESERVATION_INVALID", "this delegation already has a reservation"
            )
        left = await balance(session, voucher_account(voucher_id))
        if left < amount:
            raise FederationError(
                "FED_E_CAPACITY_EXCEEDED", "the voucher does not have that much left", left=left
            )
        reservation_id = new_id("frr")
        await session.execute(
            text(
                "INSERT INTO federation_rr_reservations (reservation_id, voucher_id, "
                "federation_job_id, amount, idempotency_key, created_by) VALUES "
                "(:r, :v, :j, :a, :k, :by)"
            ),
            {
                "r": reservation_id,
                "v": voucher_id,
                "j": federation_job_id,
                "a": amount,
                "k": idempotency_key,
                "by": actor.ref,
            },
        )
        await post(
            session,
            kind="reserve",
            entries=[(voucher_account(voucher_id), -amount), (resv(reservation_id), amount)],
            reference=federation_job_id,
            idempotency_key=f"reserve:{reservation_id}",
        )
        await session.commit()
    return {**(await get_voucher(voucher_id)), "reservation_id": reservation_id, "replayed": False}


async def _beneficiary_workspace(session: AsyncSession, fid: str) -> str | None:
    value = await session.scalar(
        text(
            "SELECT workspace_id FROM federation_local_map WHERE federation_id = :f AND "
            "id_type = 'workspace'"
        ),
        {"f": fid},
    )
    return str(value) if value else None


async def settle_reservations(limit: int = 200) -> int:
    """Consume what ended delegations used (at most the reservation) and
    release the rest. Idempotent: each reservation settles once."""
    from app.services.federation import delegations, metering

    async with async_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT r.reservation_id, r.federation_job_id, v.beneficiary_fid FROM "
                        "federation_rr_reservations r JOIN federation_rr_vouchers v ON "
                        "v.voucher_id = r.voucher_id WHERE r.state = 'RESERVED' "
                        "ORDER BY r.created_at LIMIT :l"
                    ),
                    {"l": limit},
                )
            )
            .mappings()
            .all()
        )
    done = 0
    for r in rows:
        async with async_session_factory() as session:
            ws = await _beneficiary_workspace(session, r["beneficiary_fid"])
            if ws is None:
                continue
            try:
                job = await delegations._row(session, ws, r["federation_job_id"])
            except FederationError:
                continue
            state = delegations._state(job)
            if state not in delegations.TERMINAL or state == "DISPUTED":
                continue  # still running, or held while the requester disputes
            usage = (await metering.summary(session, job))["usage"].get("model_tokens", {})
            await lock(session, resv(r["reservation_id"]))
            res = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM federation_rr_reservations WHERE reservation_id = :r "
                            "FOR UPDATE"
                        ),
                        {"r": r["reservation_id"]},
                    )
                )
                .mappings()
                .first()
            )
            if res is None or res["state"] != "RESERVED":
                continue
            amount = int(res["amount"])
            consumed = min(int(usage.get("total", 0)), amount)
            by_source = usage.get("by_source", {})
            entries: list[tuple[str, int]] = [(resv(res["reservation_id"]), -amount)]
            spec_id = await session.scalar(
                text("SELECT spec_id FROM federation_rr_vouchers WHERE voucher_id = :v"),
                {"v": res["voucher_id"]},
            )
            if consumed:
                entries.append((used(spec_id), consumed))
            if amount - consumed:
                entries.append((voucher_account(res["voucher_id"]), amount - consumed))
            await post(
                session,
                kind="consume" if consumed else "release",
                entries=entries,
                reference=r["federation_job_id"],
                idempotency_key=f"settle:{res['reservation_id']}",
            )
            await session.execute(
                text(
                    "UPDATE federation_rr_reservations SET state = :s, consumed = :c, "
                    "consumed_by_source = CAST(:b AS jsonb), delegation_outcome = :o, "
                    "settled_at = NOW() WHERE reservation_id = :r"
                ),
                {
                    "s": "CONSUMED" if consumed else "RELEASED",
                    "c": consumed,
                    "b": json.dumps(by_source),
                    "o": state,
                    "r": res["reservation_id"],
                },
            )
            left = await balance(session, voucher_account(res["voucher_id"]))
            if left == 0 and not await _open_reservations(session, res["voucher_id"]):
                await session.execute(
                    text(
                        "UPDATE federation_rr_vouchers SET status = 'EXHAUSTED', updated_at = "
                        "NOW() WHERE voucher_id = :v AND status = 'ACTIVE'"
                    ),
                    {"v": res["voucher_id"]},
                )
            await session.commit()
            done += 1
    return done


async def expire_due(limit: int = 200) -> int:
    """A voucher past its end with nothing in progress returns its unused part
    to the issuer. One with work in progress waits for that work to end."""
    async with async_session_factory() as session:
        ids = [
            r[0]
            for r in (
                await session.execute(
                    text(
                        "SELECT voucher_id FROM federation_rr_vouchers WHERE status = 'ACTIVE' "
                        "AND expires_at <= NOW() ORDER BY expires_at LIMIT :l"
                    ),
                    {"l": limit},
                )
            ).all()
        ]
    done = 0
    for voucher_id in ids:
        async with async_session_factory() as session:
            await lock(session, voucher_account(voucher_id))
            row = await _voucher_row(session, voucher_id, lock_row=True)
            if row["status"] != "ACTIVE" or await _open_reservations(session, voucher_id):
                continue
            await _close_voucher(session, row, "EXPIRED", "expire", "expired")
            await session.commit()
            done += 1
    return done


async def advance() -> int:
    if not enabled():
        return 0
    return await settle_reservations() + await expire_due()


async def wallet(fid: str) -> dict[str, Any]:
    """A workspace's DEMO_M position, labelled as simulated."""
    require_enabled()
    require_type(fid, "workspace")
    async with async_session_factory() as session:
        specs = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM federation_rr_specs WHERE provider_fid = :f "
                        "ORDER BY created_at"
                    ),
                    {"f": fid},
                )
            )
            .mappings()
            .all()
        )
        capacity = []
        for s in specs:
            if s["escrowable"]:
                capacity.append(
                    {
                        "spec_id": s["spec_id"],
                        "unreserved_tokens": await balance(session, cap(s["spec_id"])),
                        "consumed_tokens": await balance(session, used(s["spec_id"])),
                        "state": s["state"],
                    }
                )
        held = int(
            await session.scalar(
                text(
                    "SELECT COALESCE(sum(e.amount), 0) FROM federation_rr_entries e JOIN "
                    "federation_rr_vouchers v ON e.account = 'voucher:' || v.voucher_id "
                    "WHERE v.beneficiary_fid = :f"
                ),
                {"f": fid},
            )
            or 0
        )
    return {
        "fid": fid,
        "unit": UNIT,
        "tokens_per_unit": TOKENS_PER_M,
        "capacity": capacity,
        "voucher_tokens_held": held,
        "simulated": True,
        "redeemable": False,
        "notice": NOTICE,
    }
