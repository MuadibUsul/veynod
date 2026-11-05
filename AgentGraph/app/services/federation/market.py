"""Federation market: offers, contracts, escrow, settlement (P5-1).

Contract: docs/federation/ICD_P5_MARKET.md (vision §19.1, §19.2, §8, §14.2).

SIMULATED ONLY: prices are in SIM demonstration credits (see ``ledger``).

* One shared layer, Offer -> Contract -> Receipt, over three value streams,
  each with its own fulfilment states:

    task:  FUNDED -> DELIVERED -> ACCEPTED -> RELEASED   (or DISPUTED, CANCELLED -> REFUNDED)
    skill: FUNDED -> INSTALLED -> RELEASED                (or CANCELLED -> REFUNDED)
    tool:  FUNDED -> CALLED -> RELEASED                   (defined; not offered yet)

* Escrow: accepting an offer moves the price from the buyer into an escrow
  account in the same transaction that creates the contract. Nobody holds
  the credits in between; the seller is paid only on acceptance (task) or
  installation (skill). A dispute keeps them in escrow until an operator
  decides.
* Settlement is decoupled: a contract reaching ACCEPTED, INSTALLED, CANCELLED
  or a resolved dispute only queues one settlement instruction; the
  settlement step executes it as a balanced ledger transaction and marks the
  contract RELEASED or REFUNDED. Each contract settles at most once.
* Only people make offers, accept them, cancel and are paid; agents read.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.services.federation import ledger
from app.services.federation.canonical import sha256_hex
from app.services.federation.errors import FederationError
from app.services.federation.grants import Actor
from app.services.federation.ids import require_type

KINDS = ("task", "skill", "tool")
OFFERED_KINDS = ("task", "skill")  # tools are not federated yet (vision §4.1)
MACHINES: dict[str, dict[str, tuple[str, ...]]] = {
    "task": {
        "FUNDED": ("DELIVERED", "CANCELLED", "DISPUTED"),
        "DELIVERED": ("ACCEPTED", "DISPUTED"),
        "ACCEPTED": ("RELEASED",),
        "DISPUTED": ("RELEASED", "REFUNDED"),
        "CANCELLED": ("REFUNDED",),
    },
    "skill": {
        "FUNDED": ("INSTALLED", "CANCELLED"),
        "INSTALLED": ("RELEASED",),
        "CANCELLED": ("REFUNDED",),
    },
    "tool": {
        "FUNDED": ("CALLED", "CANCELLED"),
        "CALLED": ("RELEASED",),
        "CANCELLED": ("REFUNDED",),
    },
}
FINAL = ("RELEASED", "REFUNDED")
# Which state asks the settlement step to do what.
SETTLES = {
    "ACCEPTED": "release",
    "INSTALLED": "release",
    "CALLED": "release",
    "CANCELLED": "refund",
}
MAX_PRICE = 1_000_000
DEFAULT_OFFER_HOURS = 72
FEE_PERCENT = 0  # no platform fee: the fee rules are not decided (vision §19.3)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _person(actor: Actor, what: str) -> None:
    if actor.kind != "user":
        raise FederationError("FED_E_APPROVAL_NOT_HUMAN", f"a person {what}")


async def _fid(session: AsyncSession, workspace_id: str) -> str:
    from app.services.federation import grants

    return await grants._local_fid(session, workspace_id)


async def _workspace_of(session: AsyncSession, fid: str) -> str | None:
    value = await session.scalar(
        text(
            "SELECT workspace_id FROM federation_local_map WHERE federation_id = :f "
            "AND id_type = 'workspace'"
        ),
        {"f": fid},
    )
    return str(value) if value else None


def _offer_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "offer_id": row["offer_id"],
        "kind": row["kind"],
        "seller": row["seller_fid"],
        "buyer": row["buyer_fid"],
        "subject": row["subject"],
        "price": int(row["price"]),
        "unit": row["unit"],
        "terms_hash": row["terms_hash"],
        "state": "EXPIRED"
        if row["state"] == "OPEN" and row["expires_at"] <= _now()
        else row["state"],
        "expires_at": row["expires_at"].isoformat(),
        "simulated": True,
    }


async def create_offer(
    workspace_id: str,
    *,
    kind: str,
    subject: str,
    price: int,
    buyer: str | None = None,
    expires_hours: int = DEFAULT_OFFER_HOURS,
    actor: Actor,
) -> dict[str, Any]:
    """The seller's person puts a price on a task it was asked to do, or on
    a skill version it published."""
    _person(actor, "makes an offer")
    if kind not in KINDS:
        raise FederationError("FED_E_PACKAGE_INVALID", "kind is task, skill or tool")
    if kind not in OFFERED_KINDS:
        raise FederationError(
            "FED_E_NOT_AVAILABLE", "tools are not offered across workspaces yet", kind=kind
        )
    if not isinstance(price, int) or not 0 < price <= MAX_PRICE:
        raise FederationError("FED_E_PACKAGE_INVALID", f"price is 1 to {MAX_PRICE} SIM")
    if not 1 <= int(expires_hours) <= 24 * 30:
        raise FederationError("FED_E_PACKAGE_INVALID", "an offer lasts 1 hour to 30 days")
    if buyer is not None:
        require_type(buyer, "workspace")
    async with async_session_factory() as session:
        seller = await _fid(session, workspace_id)
        if kind == "task":
            # A price for work this buyer asked this seller to do.
            if buyer is None:
                raise FederationError("FED_E_PACKAGE_INVALID", "a task offer names its buyer")
            state = await session.scalar(
                text(
                    "SELECT state FROM federation_delegations WHERE workspace_id = "
                    "CAST(:w AS uuid) AND role = 'provider' AND peer_workspace_fid = :b "
                    "AND federation_job_id = :j"
                ),
                {"w": str(workspace_id), "b": buyer, "j": subject},
            )
            if state is None:
                raise FederationError(
                    "FED_E_JOB_UNKNOWN", "no delegation from that buyer with this job id"
                )
            if state not in ("OFFERED", "ACCEPTED", "RUNNING"):
                raise FederationError(
                    "FED_E_OFFER_STATE", "the work is already delivered or closed", state=state
                )
        else:
            name, _, version = subject.partition("@")
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT p.publisher_fid, v.state FROM registry_versions v JOIN "
                            "registry_packages p ON p.name = v.name WHERE v.name = :n AND "
                            "v.version = :v"
                        ),
                        {"n": name, "v": version},
                    )
                )
                .mappings()
                .first()
            )
            if row is None or row["publisher_fid"] != seller:
                raise FederationError(
                    "FED_E_PACKAGE_UNKNOWN", "offer a version of a skill you published"
                )
            if row["state"] != "published":
                raise FederationError("FED_E_YANKED", "this version was withdrawn")
        offer_id = ledger.new_id("fof")
        terms = {
            "kind": kind,
            "subject": subject,
            "price": price,
            "unit": ledger.UNIT,
            "seller": seller,
            "buyer": buyer,
            "fee_percent": FEE_PERCENT,
        }
        await session.execute(
            text(
                "INSERT INTO federation_offers (offer_id, kind, seller_fid, buyer_fid, subject, "
                "price, terms_hash, expires_at, created_by) VALUES (:o, :k, :s, :b, :sub, :p, "
                ":h, :e, :by)"
            ),
            {
                "o": offer_id,
                "k": kind,
                "s": seller,
                "b": buyer,
                "sub": subject,
                "p": price,
                "h": sha256_hex(terms),
                "e": _now() + timedelta(hours=int(expires_hours)),
                "by": actor.ref,
            },
        )
        await session.commit()
    return await get_offer(offer_id)


async def get_offer(offer_id: str) -> dict[str, Any]:
    async with async_session_factory() as session:
        row = (
            (
                await session.execute(
                    text("SELECT * FROM federation_offers WHERE offer_id = :o"), {"o": offer_id}
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise FederationError("FED_E_OFFER_UNKNOWN", "no such offer")
    return _offer_out(dict(row))


async def list_offers(workspace_id: str) -> list[dict[str, Any]]:
    """Offers this workspace made, and offers made to it (or to anyone)."""
    async with async_session_factory() as session:
        me = await _fid(session, workspace_id)
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM federation_offers WHERE seller_fid = :m OR buyer_fid = :m "
                        "OR (buyer_fid IS NULL AND kind = 'skill') ORDER BY created_at DESC "
                        "LIMIT 200"
                    ),
                    {"m": me},
                )
            )
            .mappings()
            .all()
        )
    return [_offer_out(dict(r)) for r in rows]


async def withdraw_offer(workspace_id: str, offer_id: str, *, actor: Actor) -> dict[str, Any]:
    _person(actor, "withdraws an offer")
    async with async_session_factory() as session:
        me = await _fid(session, workspace_id)
        done = await session.scalar(
            text(
                "UPDATE federation_offers SET state = 'WITHDRAWN' WHERE offer_id = :o AND "
                "seller_fid = :m AND state = 'OPEN' RETURNING offer_id"
            ),
            {"o": offer_id, "m": me},
        )
        if done is None:
            raise FederationError("FED_E_OFFER_STATE", "only an open offer of yours is withdrawn")
        await session.commit()
    return await get_offer(offer_id)


async def _history(
    session: AsyncSession,
    contract_id: str,
    frm: str | None,
    to: str,
    actor: str,
    note: str | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO federation_contract_history (contract_id, from_state, to_state, actor, "
            "note) VALUES (:c, :f, :t, :a, :n)"
        ),
        {"c": contract_id, "f": frm, "t": to, "a": actor, "n": note},
    )


async def accept_offer(workspace_id: str, offer_id: str, *, actor: Actor) -> dict[str, Any]:
    """The buyer's person accepts: the contract is created and funded in one
    transaction, the price moving from the buyer into escrow."""
    _person(actor, "accepts an offer")
    async with async_session_factory() as session:
        me = await _fid(session, workspace_id)
        await ledger.ensure_allowance(session, me)
        await ledger.lock(session, ledger.ws(me))
        row = (
            (
                await session.execute(
                    text("SELECT * FROM federation_offers WHERE offer_id = :o FOR UPDATE"),
                    {"o": offer_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise FederationError("FED_E_OFFER_UNKNOWN", "no such offer")
        offer = _offer_out(dict(row))
        if offer["state"] != "OPEN":
            raise FederationError(
                "FED_E_OFFER_STATE", "this offer is not open", state=offer["state"]
            )
        if offer["seller"] == me or (offer["buyer"] is not None and offer["buyer"] != me):
            raise FederationError("FED_E_OFFER_STATE", "this offer is not addressed to you")
        available = await ledger.balance(session, ledger.ws(me))
        if available < offer["price"]:
            raise FederationError(
                "FED_E_INSUFFICIENT_FUNDS",
                "not enough SIM credits",
                available=available,
                price=offer["price"],
            )
        contract_id = ledger.new_id("fct")
        await session.execute(
            text(
                "INSERT INTO federation_contracts (contract_id, offer_id, kind, seller_fid, "
                "buyer_fid, subject, price, terms_hash, state) VALUES (:c, :o, :k, :s, :b, "
                ":sub, :p, :h, 'FUNDED')"
            ),
            {
                "c": contract_id,
                "o": offer_id,
                "k": offer["kind"],
                "s": offer["seller"],
                "b": me,
                "sub": offer["subject"],
                "p": offer["price"],
                "h": offer["terms_hash"],
            },
        )
        await ledger.post(
            session,
            kind="escrow_hold",
            entries=[
                (ledger.ws(me), -offer["price"]),
                (ledger.escrow(contract_id), offer["price"]),
            ],
            reference=contract_id,
            idempotency_key=f"hold:{contract_id}",
        )
        await session.execute(
            text("UPDATE federation_offers SET state = 'ACCEPTED' WHERE offer_id = :o"),
            {"o": offer_id},
        )
        await _history(session, contract_id, None, "FUNDED", actor.ref, "price held in escrow")
        await session.commit()
    return await get_contract(contract_id)


async def _move(
    session: AsyncSession, row: dict[str, Any], to: str, actor: str, note: str | None = None
) -> None:
    allowed = MACHINES[row["kind"]].get(row["state"], ())
    if to not in allowed:
        raise FederationError(
            "FED_E_CONTRACT_STATE",
            f"a {row['kind']} contract cannot go from {row['state']} to {to}",
            allowed=list(allowed),
        )
    await session.execute(
        text(
            "UPDATE federation_contracts SET state = :t, version = version + 1, updated_at = "
            "NOW() WHERE contract_id = :c AND state = :f"
        ),
        {"t": to, "c": row["contract_id"], "f": row["state"]},
    )
    await _history(session, row["contract_id"], row["state"], to, actor, note)
    kind = SETTLES.get(to)
    if kind:
        await session.execute(
            text(
                "INSERT INTO federation_settlements (instruction_id, contract_id, kind, amount) "
                "VALUES (:i, :c, :k, :a) ON CONFLICT (contract_id) DO NOTHING"
            ),
            {"i": ledger.new_id("fst"), "c": row["contract_id"], "k": kind, "a": int(row["price"])},
        )
    row["state"] = to


async def _row(session: AsyncSession, contract_id: str, *, lock: bool = False) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                text(
                    "SELECT * FROM federation_contracts WHERE contract_id = :c"
                    + (" FOR UPDATE" if lock else "")
                ),
                {"c": contract_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise FederationError("FED_E_CONTRACT_UNKNOWN", "no such contract")
    return dict(row)


async def cancel(workspace_id: str, contract_id: str, *, actor: Actor) -> dict[str, Any]:
    """The buyer's person cancels before anything was delivered: refund."""
    _person(actor, "cancels a contract")
    async with async_session_factory() as session:
        me = await _fid(session, workspace_id)
        row = await _row(session, contract_id, lock=True)
        if row["buyer_fid"] != me:
            raise FederationError("FED_E_CONTRACT_STATE", "only the buyer cancels")
        await _move(session, row, "CANCELLED", actor.ref, "cancelled by the buyer")
        await session.commit()
    await settle_due()
    return await get_contract(contract_id)


async def resolve(contract_id: str, *, outcome: str, note: str, operator: str) -> dict[str, Any]:
    """An operator decides a disputed contract: release to the seller or
    refund the buyer."""
    if outcome not in ("release", "refund"):
        raise FederationError("FED_E_PACKAGE_INVALID", "outcome is release or refund")
    if not isinstance(note, str) or len(note.strip()) < 5:
        raise FederationError("FED_E_PACKAGE_INVALID", "a resolution needs a note")
    async with async_session_factory() as session:
        row = await _row(session, contract_id, lock=True)
        if row["state"] != "DISPUTED":
            raise FederationError("FED_E_CONTRACT_STATE", "only a disputed contract is resolved")
        await session.execute(
            text(
                "INSERT INTO federation_settlements (instruction_id, contract_id, kind, amount) "
                "VALUES (:i, :c, :k, :a)"
            ),
            {"i": ledger.new_id("fst"), "c": contract_id, "k": outcome, "a": int(row["price"])},
        )
        await _history(
            session,
            contract_id,
            "DISPUTED",
            "DISPUTED",
            f"operator:{operator}",
            f"resolved: {outcome}. {note.strip()}"[:2000],
        )
        await session.commit()
    await settle_due()
    return await get_contract(contract_id)


async def _task_state(session: AsyncSession, row: dict[str, Any]) -> str | None:
    buyer_ws = await _workspace_of(session, row["buyer_fid"])
    if buyer_ws is None:
        return None
    return await session.scalar(
        text(
            "SELECT state FROM federation_delegations WHERE workspace_id = CAST(:w AS uuid) "
            "AND role = 'requester' AND peer_workspace_fid = :s AND federation_job_id = :j"
        ),
        {"w": buyer_ws, "s": row["seller_fid"], "j": row["subject"]},
    )


async def sync(contract_id: str) -> dict[str, Any]:
    """Bring a contract up to date with the work it pays for."""
    async with async_session_factory() as session:
        row = await _row(session, contract_id, lock=True)
        if row["kind"] == "task":
            state = await _task_state(session, row)
            if state in ("SUBMITTED", "ACCEPTED_RESULT", "DISPUTED") and row["state"] == "FUNDED":
                await _move(session, row, "DELIVERED", "system", "result submitted")
            if state == "ACCEPTED_RESULT" and row["state"] == "DELIVERED":
                await _move(session, row, "ACCEPTED", "system", "the requester accepted the result")
            if state == "DISPUTED" and row["state"] in ("FUNDED", "DELIVERED"):
                await _move(session, row, "DISPUTED", "system", "the requester disputed the result")
            if (
                state in ("REJECTED", "EXPIRED", "CANCELLED", "STOP_NOT_CONFIRMED", "FAILED")
                and row["state"] == "FUNDED"
            ):
                await _move(session, row, "CANCELLED", "system", f"the work ended: {state}")
        elif row["kind"] == "skill" and row["state"] == "FUNDED":
            name, _, version = row["subject"].partition("@")
            buyer_ws = await _workspace_of(session, row["buyer_fid"])
            installed = buyer_ws and await session.scalar(
                text(
                    "SELECT 1 FROM registry_downloads WHERE workspace_id = CAST(:w AS uuid) "
                    "AND name = :n AND version = :v"
                ),
                {"w": buyer_ws, "n": name, "v": version},
            )
            if installed:
                await _move(session, row, "INSTALLED", "system", "the buyer installed it")
        await session.commit()
    return await get_contract(contract_id)


async def settle_due(limit: int = 200) -> int:
    """Execute pending settlement instructions as ledger transactions."""
    async with async_session_factory() as session:
        pending = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM federation_settlements WHERE state = 'pending' "
                        "ORDER BY created_at LIMIT :l"
                    ),
                    {"l": limit},
                )
            )
            .mappings()
            .all()
        )
    done = 0
    for item in pending:
        async with async_session_factory() as session:
            row = await _row(session, item["contract_id"], lock=True)
            state = await session.scalar(
                text(
                    "SELECT state FROM federation_settlements WHERE instruction_id = :i FOR UPDATE"
                ),
                {"i": item["instruction_id"]},
            )
            if state != "pending":
                continue
            to = row["seller_fid"] if item["kind"] == "release" else row["buyer_fid"]
            txn = await ledger.post(
                session,
                kind=item["kind"],
                entries=[
                    (ledger.escrow(row["contract_id"]), -int(item["amount"])),
                    (ledger.ws(to), int(item["amount"])),
                ],
                reference=row["contract_id"],
                idempotency_key=f"settle:{item['instruction_id']}",
            )
            await session.execute(
                text(
                    "UPDATE federation_settlements SET state = 'done', txn_id = :t, settled_at = "
                    "NOW() WHERE instruction_id = :i"
                ),
                {"t": txn, "i": item["instruction_id"]},
            )
            final = "RELEASED" if item["kind"] == "release" else "REFUNDED"
            await session.execute(
                text(
                    "UPDATE federation_contracts SET state = :t, version = version + 1, "
                    "updated_at = NOW() WHERE contract_id = :c"
                ),
                {"t": final, "c": row["contract_id"]},
            )
            await _history(
                session,
                row["contract_id"],
                row["state"],
                final,
                "settlement",
                f"{item['amount']} SIM to {to}",
            )
            await session.commit()
            done += 1
    return done


async def advance(limit: int = 200) -> int:
    """Background step: sync open contracts with their work, then settle."""
    async with async_session_factory() as session:
        open_ = [
            r[0]
            for r in (
                await session.execute(
                    text(
                        "SELECT contract_id FROM federation_contracts WHERE state NOT IN "
                        "('RELEASED', 'REFUNDED') ORDER BY updated_at LIMIT :l"
                    ),
                    {"l": limit},
                )
            ).all()
        ]
    for contract_id in open_:
        try:
            await sync(contract_id)
        except FederationError:
            continue
    return await settle_due()


async def get_contract(contract_id: str) -> dict[str, Any]:
    async with async_session_factory() as session:
        row = await _row(session, contract_id)
        history = (
            (
                await session.execute(
                    text(
                        "SELECT from_state, to_state, actor, note, created_at FROM "
                        "federation_contract_history WHERE contract_id = :c ORDER BY id"
                    ),
                    {"c": contract_id},
                )
            )
            .mappings()
            .all()
        )
        settlement = (
            (
                await session.execute(
                    text("SELECT * FROM federation_settlements WHERE contract_id = :c"),
                    {"c": contract_id},
                )
            )
            .mappings()
            .first()
        )
        held = await ledger.balance(session, ledger.escrow(contract_id))
    receipt = None
    if settlement is not None and settlement["state"] == "done":
        receipt = {
            "receipt_id": settlement["instruction_id"],
            "kind": settlement["kind"],
            "amount": int(settlement["amount"]),
            "unit": ledger.UNIT,
            "paid_to": row["seller_fid"] if settlement["kind"] == "release" else row["buyer_fid"],
            "ledger_txn": settlement["txn_id"],
            "at": settlement["settled_at"].isoformat(),
        }
    return {
        "contract_id": contract_id,
        "offer_id": row["offer_id"],
        "kind": row["kind"],
        "seller": row["seller_fid"],
        "buyer": row["buyer_fid"],
        "subject": row["subject"],
        "price": int(row["price"]),
        "unit": row["unit"],
        "terms_hash": row["terms_hash"],
        "state": row["state"],
        "next": list(MACHINES[row["kind"]].get(row["state"], ())),
        "in_escrow": held,
        "settlement": {
            "instruction_id": settlement["instruction_id"],
            "kind": settlement["kind"],
            "state": settlement["state"],
        }
        if settlement
        else None,
        "receipt": receipt,
        "history": [
            {
                "from": h["from_state"],
                "to": h["to_state"],
                "actor": h["actor"],
                "note": h["note"],
                "at": h["created_at"].isoformat(),
            }
            for h in history
        ],
        "simulated": True,
        "notice": ledger.NOTICE,
    }


async def list_contracts(workspace_id: str) -> list[dict[str, Any]]:
    async with async_session_factory() as session:
        me = await _fid(session, workspace_id)
        ids = [
            r[0]
            for r in (
                await session.execute(
                    text(
                        "SELECT contract_id FROM federation_contracts WHERE buyer_fid = :m OR "
                        "seller_fid = :m ORDER BY created_at DESC LIMIT 200"
                    ),
                    {"m": me},
                )
            ).all()
        ]
    return [await get_contract(c) for c in ids]


def machines() -> dict[str, Any]:
    return {"machines": json.loads(json.dumps(MACHINES)), "offered": list(OFFERED_KINDS)}
