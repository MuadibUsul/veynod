"""Simulated double-entry ledger for the federation market (P5).

SIMULATED ONLY: the unit is SIM, a demonstration credit. It is not money,
cannot be bought or withdrawn, and exists only on this deployment.

Accounts are ``ws:<workspace fid>``, ``escrow:<contract id>`` and ``mint``
(where demonstration credits come from; it is the only account that may go
below zero). Every transaction sums to zero, which the database checks when
it commits, and nothing is ever updated or deleted.
"""

from __future__ import annotations

import secrets
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_factory
from app.services.federation.errors import FederationError
from app.services.federation.ids import b32, require_type

UNIT = "SIM"
NOTICE = "simulated demonstration credits: not money, cannot be bought or withdrawn"


def ws(fid: str) -> str:
    return f"ws:{fid}"


def escrow(contract_id: str) -> str:
    return f"escrow:{contract_id}"


def new_id(prefix: str) -> str:
    return f"{prefix}_" + b32(secrets.token_bytes(16))[:26]


async def lock(session: AsyncSession, account: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('ledger:' || :a))"), {"a": account}
    )


async def balance(session: AsyncSession, account: str) -> int:
    return int(
        await session.scalar(
            text(
                "SELECT COALESCE(sum(amount), 0) FROM federation_ledger_entries WHERE account = :a"
            ),
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
) -> str:
    """Write one balanced transaction inside the caller's database
    transaction. With an idempotency key, a repeat returns the first one."""
    if sum(amount for _, amount in entries) != 0 or not entries:
        raise FederationError("FED_E_PACKAGE_INVALID", "a ledger transaction must balance")
    if idempotency_key:
        existing = await session.scalar(
            text("SELECT txn_id FROM federation_ledger_txns WHERE idempotency_key = :k"),
            {"k": idempotency_key},
        )
        if existing:
            return existing
    txn_id = new_id("ftx")
    await session.execute(
        text(
            "INSERT INTO federation_ledger_txns (txn_id, kind, reference, idempotency_key) "
            "VALUES (:t, :k, :r, :i)"
        ),
        {"t": txn_id, "k": kind, "r": reference, "i": idempotency_key},
    )
    for account, amount in entries:
        await session.execute(
            text(
                "INSERT INTO federation_ledger_entries (txn_id, account, amount) "
                "VALUES (:t, :a, :m)"
            ),
            {"t": txn_id, "a": account, "m": int(amount)},
        )
    return txn_id


async def ensure_allowance(session: AsyncSession, fid: str) -> None:
    """Every workspace gets its demonstration credits once."""
    amount = get_settings().federation_sim_allowance
    if amount > 0:
        await post(
            session,
            kind="allowance",
            entries=[("mint", -amount), (ws(fid), amount)],
            reference=fid,
            idempotency_key=f"allowance:{fid}",
        )


async def wallet(fid: str, *, limit: int = 50) -> dict[str, Any]:
    require_type(fid, "workspace")
    async with async_session_factory() as session:
        await ensure_allowance(session, fid)
        await session.commit()
        available = await balance(session, ws(fid))
        held = int(
            await session.scalar(
                text(
                    "SELECT COALESCE(sum(e.amount), 0) FROM federation_ledger_entries e JOIN "
                    "federation_contracts c ON e.account = 'escrow:' || c.contract_id "
                    "WHERE c.buyer_fid = :f"
                ),
                {"f": fid},
            )
            or 0
        )
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT e.amount, e.created_at, t.kind, t.reference, t.txn_id FROM "
                        "federation_ledger_entries e JOIN federation_ledger_txns t ON "
                        "t.txn_id = e.txn_id WHERE e.account = :a ORDER BY e.id DESC LIMIT :l"
                    ),
                    {"a": ws(fid), "l": limit},
                )
            )
            .mappings()
            .all()
        )
    return {
        "fid": fid,
        "unit": UNIT,
        "simulated": True,
        "notice": NOTICE,
        "available": available,
        "held_in_escrow": held,
        "entries": [
            {
                "txn_id": r["txn_id"],
                "kind": r["kind"],
                "amount": int(r["amount"]),
                "reference": r["reference"],
                "at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
    }


async def mint(fid: str, amount: int, *, reason: str, operator: str) -> dict[str, Any]:
    """An operator adds demonstration credits to a workspace."""
    require_type(fid, "workspace")
    if not isinstance(amount, int) or not 0 < amount <= 1_000_000:
        raise FederationError("FED_E_PACKAGE_INVALID", "amount is 1 to 1,000,000 SIM")
    if not isinstance(reason, str) or len(reason.strip()) < 5:
        raise FederationError("FED_E_PACKAGE_INVALID", "a mint needs a reason")
    async with async_session_factory() as session:
        txn = await post(
            session,
            kind="mint",
            entries=[("mint", -amount), (ws(fid), amount)],
            reference=f"operator:{operator}: {reason.strip()}"[:500],
        )
        await session.commit()
    return {"txn_id": txn, **(await wallet(fid, limit=5))}


async def is_balanced() -> dict[str, Any]:
    """Whole-ledger check: every transaction and the sum of all accounts is zero."""
    async with async_session_factory() as session:
        unbalanced = await session.scalar(
            text(
                "SELECT count(*) FROM (SELECT txn_id FROM federation_ledger_entries "
                "GROUP BY txn_id HAVING sum(amount) <> 0) x"
            )
        )
        total = await session.scalar(
            text("SELECT COALESCE(sum(amount), 0) FROM federation_ledger_entries")
        )
        negative = await session.scalar(
            text(
                "SELECT count(*) FROM (SELECT account FROM federation_ledger_entries WHERE "
                "account <> 'mint' GROUP BY account HAVING sum(amount) < 0) x"
            )
        )
    return {
        "unbalanced_transactions": int(unbalanced or 0),
        "total": int(total or 0),
        "negative_accounts": int(negative or 0),
        "ok": not unbalanced and not total and not negative,
    }
