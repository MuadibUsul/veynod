"""Simulated market: offers, contracts, escrow, settlement (federation P5-1,
docs/federation/ICD_P5_MARKET.md).

A-02: a paid cross-workspace task runs end to end (delegation, escrow,
acceptance, release), in SIM demonstration credits, never real money.
"""

from __future__ import annotations

import asyncpg
import pytest

from app.services.federation import delegations, ledger, market, registry
from app.services.federation.errors import FederationError
from tests import test_federation_delegations as _bridge
from tests.test_federation_delegations import RESULT, Tasks, _connect, _terms, _ws
from tests.test_federation_p1_gate import db_dsn

pytestmark = pytest.mark.integration

federation_on = _bridge.federation_on


async def _code(awaitable) -> str:
    with pytest.raises(FederationError) as err:
        await awaitable
    return err.value.code


async def _available(ws) -> int:
    return (await ledger.wallet(ws["fid"]))["available"]


async def _delegation(req, prov) -> str:
    sent = await delegations.offer(
        req["id"], peer_workspace=prov["fid"], terms=_terms(), actor=req["owner"]
    )
    return sent["delegation"]["federation_job_id"]


async def test_a_paid_task_runs_from_escrow_to_release(client, admin_headers, federation_on):
    """A-02: delegate, quote, fund escrow, deliver, accept, release."""
    req, prov = await _ws(client, admin_headers, "mr"), await _ws(client, admin_headers, "mp")
    await _connect(req, prov)
    start_req, start_prov = await _available(req), await _available(prov)
    assert start_req == start_prov == 1000  # the one-time demonstration allowance
    wallet = await ledger.wallet(req["fid"])
    assert (
        wallet["simulated"] is True and wallet["unit"] == "SIM" and "not money" in wallet["notice"]
    )

    job = await _delegation(req, prov)
    # The provider's person quotes; an agent cannot.
    assert (
        await _code(
            market.create_offer(
                prov["id"],
                kind="task",
                subject=job,
                price=300,
                buyer=req["fid"],
                actor=prov["agent"],
            )
        )
        == "FED_E_APPROVAL_NOT_HUMAN"
    )
    offer = await market.create_offer(
        prov["id"], kind="task", subject=job, price=300, buyer=req["fid"], actor=prov["owner"]
    )
    assert offer["state"] == "OPEN" and offer["simulated"] is True
    # Only the named buyer accepts, as a person.
    assert await _code(market.accept_offer(prov["id"], offer["offer_id"], actor=prov["owner"])) == (
        "FED_E_OFFER_STATE"
    )
    contract = await market.accept_offer(req["id"], offer["offer_id"], actor=req["owner"])
    cid = contract["contract_id"]
    assert contract["state"] == "FUNDED" and contract["in_escrow"] == 300
    assert await _available(req) == start_req - 300  # held, not paid
    assert await _available(prov) == start_prov  # nothing for the seller yet
    assert await _code(market.accept_offer(req["id"], offer["offer_id"], actor=req["owner"])) == (
        "FED_E_OFFER_STATE"
    )

    # The work happens on the existing delegation bridge.
    await delegations.act(
        prov["id"], job, "accept", actor=prov["owner"], create_local_task=Tasks().create
    )
    await delegations.act(prov["id"], job, "submit", actor=prov["agent"], result=RESULT)
    assert (await market.sync(cid))["state"] == "DELIVERED"
    assert await _available(prov) == start_prov  # delivered is not accepted
    await delegations.act(req["id"], job, "accept_result", actor=req["owner"])
    synced = await market.sync(cid)
    # Accepting only queues the release; the settlement step pays.
    assert synced["state"] == "ACCEPTED" and synced["settlement"]["state"] == "pending"
    assert await market.settle_due() >= 1
    done = await market.get_contract(cid)
    assert done["state"] == "RELEASED" and done["in_escrow"] == 0
    assert done["receipt"]["paid_to"] == prov["fid"] and done["receipt"]["amount"] == 300
    assert await _available(prov) == start_prov + 300
    assert await _available(req) == start_req - 300
    assert [h["to"] for h in done["history"]] == ["FUNDED", "DELIVERED", "ACCEPTED", "RELEASED"]
    # Settling again changes nothing; the ledger balances.
    await market.settle_due()
    assert await _available(prov) == start_prov + 300
    check = await ledger.is_balanced()
    assert check["unbalanced_transactions"] == 0 and check["negative_accounts"] == 0


async def test_disputes_cancellations_skills_and_limits(client, admin_headers, federation_on):
    req, prov = await _ws(client, admin_headers, "dr"), await _ws(client, admin_headers, "dp")
    await _connect(req, prov)
    base = await _available(req)

    # A disputed result stays in escrow until an operator decides.
    job = await _delegation(req, prov)
    offer = await market.create_offer(
        prov["id"], kind="task", subject=job, price=200, buyer=req["fid"], actor=prov["owner"]
    )
    cid = (await market.accept_offer(req["id"], offer["offer_id"], actor=req["owner"]))[
        "contract_id"
    ]
    await delegations.act(
        prov["id"], job, "accept", actor=prov["owner"], create_local_task=Tasks().create
    )
    await delegations.act(prov["id"], job, "submit", actor=prov["agent"], result=RESULT)
    await delegations.act(req["id"], job, "dispute", actor=req["owner"], note="Wrong totals.")
    disputed = await market.sync(cid)
    assert disputed["state"] == "DISPUTED" and disputed["in_escrow"] == 200
    await market.settle_due()
    assert (await market.get_contract(cid))["in_escrow"] == 200
    refunded = await market.resolve(
        cid, outcome="refund", note="Totals were wrong.", operator="ops-anna"
    )
    assert refunded["state"] == "REFUNDED" and refunded["receipt"]["paid_to"] == req["fid"]
    assert await _available(req) == base

    # Cancelled before delivery: refunded; after delivery: refused.
    job2 = await _delegation(req, prov)
    offer2 = await market.create_offer(
        prov["id"], kind="task", subject=job2, price=100, buyer=req["fid"], actor=prov["owner"]
    )
    cid2 = (await market.accept_offer(req["id"], offer2["offer_id"], actor=req["owner"]))[
        "contract_id"
    ]
    assert await _code(market.cancel(prov["id"], cid2, actor=prov["owner"])) == (
        "FED_E_CONTRACT_STATE"
    )
    cancelled = await market.cancel(req["id"], cid2, actor=req["owner"])
    assert cancelled["state"] == "REFUNDED" and await _available(req) == base

    # Not enough credits: refused, nothing moves.
    job3 = await _delegation(req, prov)
    big = await market.create_offer(
        prov["id"], kind="task", subject=job3, price=5000, buyer=req["fid"], actor=prov["owner"]
    )
    assert await _code(market.accept_offer(req["id"], big["offer_id"], actor=req["owner"])) == (
        "FED_E_INSUFFICIENT_FUNDS"
    )
    assert await _available(req) == base
    await market.withdraw_offer(prov["id"], big["offer_id"], actor=prov["owner"])
    assert (await market.get_offer(big["offer_id"]))["state"] == "WITHDRAWN"

    # A skill sale: released when the buyer installs that version.
    name = f"paid-{prov['fid'][-8:]}"
    await registry.publish(
        prov["id"],
        manifest={"name": name, "version": "1.0.0", "entry": "SKILL.md"},
        files=[{"path": "SKILL.md", "content": "# Steps"}],
        actor=prov["owner"],
    )
    sale = await market.create_offer(
        prov["id"], kind="skill", subject=f"{name}@1.0.0", price=50, actor=prov["owner"]
    )
    cid3 = (await market.accept_offer(req["id"], sale["offer_id"], actor=req["owner"]))[
        "contract_id"
    ]
    seller_before = await _available(prov)
    await registry.install(req["id"], name, "1.0.0", actor=req["owner"])
    await market.advance()
    sold = await market.get_contract(cid3)
    assert sold["state"] == "RELEASED" and await _available(prov) == seller_before + 50
    assert [h["to"] for h in sold["history"]] == ["FUNDED", "INSTALLED", "RELEASED"]

    # Tools have a state machine but are not offered yet; prices are bounded.
    assert (
        await _code(
            market.create_offer(
                prov["id"], kind="tool", subject="search", price=5, actor=prov["owner"]
            )
        )
        == "FED_E_NOT_AVAILABLE"
    )
    assert "tool" in market.machines()["machines"]
    assert (
        await _code(
            market.create_offer(
                prov["id"], kind="skill", subject=f"{name}@1.0.0", price=0, actor=prov["owner"]
            )
        )
        == "FED_E_PACKAGE_INVALID"
    )

    # The ledger cannot be edited, and an unbalanced transaction is refused.
    conn = await asyncpg.connect(db_dsn())
    try:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute("UPDATE federation_ledger_entries SET amount = amount + 1")
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO federation_ledger_txns (txn_id, kind) VALUES "
                    "('ftx_" + "a" * 26 + "', 'mint')"
                )
                await conn.execute(
                    "INSERT INTO federation_ledger_entries (txn_id, account, amount) VALUES "
                    "('ftx_" + "a" * 26 + "', 'mint', -5)"
                )
    finally:
        await conn.close()
    assert (await ledger.is_balanced())["ok"] is True
