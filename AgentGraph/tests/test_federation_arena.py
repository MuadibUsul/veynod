"""Simulated resource rights and the Agent Poker Arena (federation P6, R1-R3;
docs/federation/ICD_P6_RESOURCE_RIGHTS_ARENA.md). NO ECONOMIC VALUE.

Task acceptance covered here (fed-resource-rights-poker-arena-sim-v1):
RR-02 isolated DEMO_M ledger, voucher lifecycle, concurrent reserve without
double spend; RR-03 no unit conversion, self-reported quota never escrowed;
RR-04 matching, commit/reveal, signed result, replay, duplicate and stale
actions, timeouts; RR-05 an unfulfilled obligation only restricts new arena
matches, with review and recovery, and a report alone restricts nothing;
RR-06 the bridge, grants and metering are reused (a revoked connection
refuses, credentials never travel, settlement happens once); RR-07 the
features are off by default.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.config import get_settings
from app.database import async_session_factory
from app.services.federation import (
    arena,
    delegations,
    governance,
    grants,
    integrity,
    ledger,
    metering,
    rights,
)
from app.services.federation import arena_engine as engine
from app.services.federation.errors import FederationError
from tests import test_federation_delegations as _bridge
from tests.conftest import run_server
from tests.test_federation_delegations import RESULT, Tasks, _connect, _terms, _ws

pytestmark = pytest.mark.integration

M = rights.TOKENS_PER_M
REPORT_CATEGORY = "fraud"


@pytest.fixture
def arena_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "federation_enabled", True)
    monkeypatch.setattr(settings, "federation_kek", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setattr(settings, "federation_resource_rights_enabled", True)
    monkeypatch.setattr(settings, "federation_arena_enabled", True)
    monkeypatch.setattr(settings, "federation_arena_dispute_minutes", 0)
    return settings


federation_on = _bridge.federation_on


async def _code(awaitable) -> str:
    with pytest.raises(FederationError) as err:
        await awaitable
    return err.value.code


async def _mock_spec(ws, tokens: int = 10 * M) -> dict:
    return await rights.declare_spec(
        ws["id"],
        resource_class="MOCK_DEMO",
        model_family="mock-model",
        allowed_use="arena and demo delegations only",
        declared_tokens=tokens,
        measured_source="mock",
        actor=ws["owner"],
    )


async def _unreserved(spec_id: str) -> int:
    async with async_session_factory() as session:
        return await rights.balance(session, rights.cap(spec_id))


async def _sql(query: str, **params) -> None:
    async with async_session_factory() as session:
        await session.execute(text(query), params)
        await session.commit()


# ── R1: resource rights ───────────────────────────────────────────────────


async def test_off_by_default(client, admin_headers, federation_on):
    """RR-07: federation on, resource rights and arena still off."""
    a = await _ws(client, admin_headers, "off")
    assert await _code(_mock_spec(a)) == "FED_E_RR_DISABLED"
    assert (
        await _code(arena.join_queue(a["id"], spec_id="x", stake=M, actor=a["owner"]))
        == "FED_E_ARENA_DISABLED"
    )
    assert await rights.advance() == 0 and await arena.advance() == 0


async def test_units_classes_and_capacity_are_kept_apart(client, admin_headers, arena_on):
    """RR-02/RR-03: only MOCK_DEMO is escrowable; no conversion, no floats."""
    a, b = await _ws(client, admin_headers, "ua"), await _ws(client, admin_headers, "ub")
    # Real classes wait for the R4 gate.
    for cls in ("CONTROLLED_PREPAID", "AUTHORIZED_PROVIDER_SERVICE"):
        assert (
            await _code(
                rights.declare_spec(
                    a["id"],
                    resource_class=cls,
                    model_family="x",
                    allowed_use="x",
                    declared_tokens=M,
                    measured_source="adapter",
                    actor=a["owner"],
                )
            )
            == "FED_E_NOT_AVAILABLE"
        )
    # A subscription's remaining quota is only shown, never escrowed or staked.
    sub = await rights.declare_spec(
        a["id"],
        resource_class="SELF_REPORTED_SUBSCRIPTION",
        model_family="consumer-plan",
        allowed_use="shown only",
        declared_tokens=5 * M,
        measured_source="self_reported",
        actor=a["owner"],
    )
    assert sub["escrowable"] is False and "never escrowed" in sub["warning"]
    assert "unreserved_tokens" not in sub
    assert (
        await _code(
            rights.issue_voucher(
                a["id"],
                spec_id=sub["spec_id"],
                beneficiary=b["fid"],
                amount=M,
                usage_scope="try a subscription",
                actor=a["owner"],
            )
        )
        == "FED_E_RESOURCE_NOT_ESCROWABLE"
    )
    assert (
        await _code(arena.join_queue(a["id"], spec_id=sub["spec_id"], stake=M, actor=a["owner"]))
        == "FED_E_RESOURCE_NOT_ESCROWABLE"
    )
    # The schema itself refuses an escrowable subscription.
    with pytest.raises(DBAPIError):
        await _sql(
            "UPDATE federation_rr_specs SET escrowable = true WHERE spec_id = :s",
            s=sub["spec_id"],
        )
    # Only an owner declares; only people issue.
    assert (
        await _code(
            rights.declare_spec(
                a["id"],
                resource_class="MOCK_DEMO",
                model_family="m",
                allowed_use="u",
                declared_tokens=M,
                measured_source="mock",
                actor=a["person"],
            )
        )
        == "FED_E_APPROVAL_NOT_HUMAN"
    )
    spec = await _mock_spec(a, 5 * M)
    assert spec["unreserved_tokens"] == 5 * M
    # The allowance bounds simulated capacity.
    assert await _code(_mock_spec(a, 16 * M)) == "FED_E_CAPACITY_EXCEEDED"
    # No other unit, no float, no string amounts.
    for unit in ("SIM", "ARENA_CHIP", "M", "model_tokens"):
        assert (
            await _code(
                rights.issue_voucher(
                    a["id"],
                    spec_id=spec["spec_id"],
                    beneficiary=b["fid"],
                    amount=M,
                    unit=unit,
                    usage_scope="wrong unit",
                    actor=a["owner"],
                )
            )
            == "FED_E_UNIT_MISMATCH"
        )
    for amount in (1.5, "1000", True, 0, -5):
        assert (
            await _code(
                rights.issue_voucher(
                    a["id"],
                    spec_id=spec["spec_id"],
                    beneficiary=b["fid"],
                    amount=amount,
                    usage_scope="bad amount",
                    actor=a["owner"],
                )
            )
            == "FED_E_PACKAGE_INVALID"
        )
    # A credential in a declaration never gets stored.
    assert (
        await _code(
            rights.declare_spec(
                a["id"],
                resource_class="MOCK_DEMO",
                model_family="m",
                allowed_use="use key sk-ant-abcdefghijklmnopqrstu",
                declared_tokens=M,
                measured_source="mock",
                actor=a["owner"],
            )
        )
        == "FED_E_CREDENTIAL_IN_PAYLOAD"
    )
    # The SIM wallet is untouched by any of this; the DEMO_M ledger balances.
    assert (await ledger.wallet(a["fid"]))["available"] == get_settings().federation_sim_allowance
    assert (await rights.is_balanced())["ok"]
    w = await rights.wallet(a["fid"])
    assert w["redeemable"] is False and "no economic value" in w["notice"]


async def _bridge_job(req, prov, amount: int) -> str:
    sent = await delegations.offer(
        req["id"],
        peer_workspace=prov["fid"],
        terms=_terms(budget_cap={"amount": amount, "unit": "model_tokens"}),
        actor=req["owner"],
    )
    return sent["delegation"]["federation_job_id"]


async def test_concurrent_reservations_never_overspend(client, admin_headers, arena_on):
    """RR-02/RR-04: parallel reserves on one voucher cannot exceed it; a
    retry is replayed, a job cannot draw twice, the ledger balances."""
    a, b = await _ws(client, admin_headers, "ca"), await _ws(client, admin_headers, "cb")
    await _connect(b, a)  # b may delegate to a
    spec = await _mock_spec(a, 5 * M)
    voucher = await rights.issue_voucher(
        a["id"],
        spec_id=spec["spec_id"],
        beneficiary=b["fid"],
        amount=3 * M,
        usage_scope="demo research help",
        actor=a["owner"],
    )
    vid = voucher["voucher_id"]
    assert voucher["available_amount"] == 3 * M and await _unreserved(spec["spec_id"]) == 2 * M
    jobs = [await _bridge_job(b, a, M) for _ in range(5)]
    results = await asyncio.gather(
        *[
            rights.reserve(
                b["id"],
                voucher_id=vid,
                federation_job_id=j,
                amount=M,
                idempotency_key=f"key-{j}",
                actor=b["agent"],
            )
            for j in jobs
        ],
        return_exceptions=True,
    )
    ok = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, FederationError)]
    assert len(ok) == 3 and len(refused) == 2
    assert {e.code for e in refused} == {"FED_E_CAPACITY_EXCEEDED"}
    after = await rights.get_voucher(vid)
    assert after["available_amount"] == 0 and after["reserved_amount"] == 3 * M
    # The same request again is replayed, not booked again.
    winner_job = ok[0]["reservations"][0]["federation_job_id"]
    first = next(r for r in after["reservations"] if r["federation_job_id"] == winner_job)
    again = await rights.reserve(
        b["id"],
        voucher_id=vid,
        federation_job_id=winner_job,
        amount=M,
        idempotency_key=f"key-{winner_job}",
        actor=b["agent"],
    )
    assert again["replayed"] and again["reservation_id"] == first["reservation_id"]
    # The issuer cannot reserve on someone else's voucher; others cannot see it.
    assert (
        await _code(
            rights.reserve(
                a["id"],
                voucher_id=vid,
                federation_job_id=jobs[0],
                amount=M,
                idempotency_key="xxxxxxxxxx",
                actor=a["owner"],
            )
        )
        == "FED_E_VOUCHER_UNKNOWN"
    )
    c = await _ws(client, admin_headers, "cc")
    assert await _code(rights.get_voucher_for(c["id"], vid)) == "FED_E_VOUCHER_UNKNOWN"
    # A ledger row cannot be edited; an overdraft cannot be committed.
    with pytest.raises(DBAPIError):
        await _sql("UPDATE federation_rr_entries SET amount = amount + 1")
    with pytest.raises(DBAPIError):
        async with async_session_factory() as session:
            await rights.post(
                session,
                kind="issue",
                entries=[(rights.voucher_account(vid), -1), (rights.cap(spec["spec_id"]), 1)],
            )
            await session.commit()
    assert (await rights.is_balanced())["ok"]


async def test_a_reservation_runs_on_the_bridge_and_is_metered(client, admin_headers, arena_on):
    """RR-06: the voucher is used only through a delegation; the enforced cap
    equals the reservation; what was used is consumed, the rest released."""
    a, b = await _ws(client, admin_headers, "ma"), await _ws(client, admin_headers, "mb")
    connection = await _connect(b, a)
    spec = await _mock_spec(a, 4 * M)
    vid = (
        await rights.issue_voucher(
            a["id"],
            spec_id=spec["spec_id"],
            beneficiary=b["fid"],
            amount=2 * M,
            usage_scope="demo research help",
            actor=a["owner"],
        )
    )["voucher_id"]
    # A delegation without the matching enforced cap is refused.
    loose = (
        await delegations.offer(b["id"], peer_workspace=a["fid"], terms=_terms(), actor=b["owner"])
    )["delegation"]["federation_job_id"]
    assert (
        await _code(
            rights.reserve(
                b["id"],
                voucher_id=vid,
                federation_job_id=loose,
                amount=M,
                idempotency_key="loose-cap-1",
                actor=b["owner"],
            )
        )
        == "FED_E_RESERVATION_INVALID"
    )
    job = await _bridge_job(b, a, M)
    await rights.reserve(
        b["id"],
        voucher_id=vid,
        federation_job_id=job,
        amount=M,
        idempotency_key="run-1-key",
        actor=b["agent"],
    )
    assert await rights.settle_reservations() == 0  # still open
    await delegations.act(
        a["id"], job, "accept", actor=a["owner"], create_local_task=Tasks().create
    )
    await metering.use(
        a["id"],
        job,
        kind="record",
        unit="model_tokens",
        amount=400_000,
        actor_ref=a["agent"].ref,
        source="adapter",
        proof_ref="mock-adapter://run/1",
    )
    # The provider cannot start a run past the reservation.
    assert (
        await _code(
            metering.use(
                a["id"],
                job,
                kind="reserve",
                unit="model_tokens",
                amount=700_000,
                actor_ref=a["agent"].ref,
            )
        )
        == "FED_E_BUDGET_EXCEEDED"
    )
    plain = {k: v for k, v in RESULT.items() if k != "cost_receipt"}
    await delegations.act(a["id"], job, "submit", actor=a["agent"], result=plain)
    await delegations.act(b["id"], job, "accept_result", actor=b["owner"])
    assert await rights.settle_reservations() == 1
    assert await rights.settle_reservations() == 0  # once only
    v = await rights.get_voucher(vid)
    res = v["reservations"][0]
    assert res["state"] == "CONSUMED" and res["consumed"] == 400_000
    assert res["consumed_by_source"] == {"adapter": 400_000}  # never relabelled
    assert v["consumed_amount"] == 400_000 and v["available_amount"] == 2 * M - 400_000
    # A revoked connection refuses any new reservation (RR-06).
    await grants.revoke(b["id"], connection, actor=b["owner"])
    job2_code = await _code(
        rights.reserve(
            b["id"],
            voucher_id=vid,
            federation_job_id=job,
            amount=M,
            idempotency_key="after-revoke",
            actor=b["owner"],
        )
    )
    assert job2_code in ("FED_E_GRANT_INACTIVE", "FED_E_RESERVATION_INVALID")
    # Issuer revocation returns only the unused part; arena vouchers are not revocable.
    revoked = await rights.revoke_voucher(a["id"], vid, actor=a["owner"])
    assert revoked["status"] == "REVOKED" and revoked["available_amount"] == 0
    assert await _unreserved(spec["spec_id"]) == 4 * M - 400_000
    assert (await rights.is_balanced())["ok"]


async def test_vouchers_expire_back_to_the_issuer(client, admin_headers, arena_on):
    a, b = await _ws(client, admin_headers, "ea"), await _ws(client, admin_headers, "eb")
    spec = await _mock_spec(a, 2 * M)
    vid = (
        await rights.issue_voucher(
            a["id"],
            spec_id=spec["spec_id"],
            beneficiary=b["fid"],
            amount=M,
            usage_scope="short lived demo",
            actor=a["owner"],
        )
    )["voucher_id"]
    await _sql(
        "UPDATE federation_rr_vouchers SET expires_at = NOW() - interval '1 minute' "
        "WHERE voucher_id = :v",
        v=vid,
    )
    assert await rights.expire_due() >= 1
    assert (await rights.get_voucher(vid))["status"] == "EXPIRED"
    assert await _unreserved(spec["spec_id"]) == 2 * M


# ── R2: the arena ─────────────────────────────────────────────────────────


async def _seated(client, admin_headers, tag: str, stake: int = M):
    a, b = await _ws(client, admin_headers, f"{tag}a"), await _ws(client, admin_headers, f"{tag}b")
    sa, sb = await _mock_spec(a, 5 * M), await _mock_spec(b, 5 * M)
    first = await arena.join_queue(a["id"], spec_id=sa["spec_id"], stake=stake, actor=a["person"])
    assert first["state"] == "WAITING" and first["match"] is None
    second = await arena.join_queue(b["id"], spec_id=sb["spec_id"], stake=stake, actor=b["person"])
    assert second["state"] == "MATCHED"
    match = second["match"]
    assert match["seats"][0]["fid"] == a["fid"] and match["seats"][1]["fid"] == b["fid"]
    return a, b, sa, sb, match["match_id"], match


async def _ready(a, b, match_id: str, contract_hash: str, *, agent_play=False):
    for ws in (a, b):
        await arena.authorize(
            ws["id"],
            match_id,
            contract_hash=contract_hash,
            agent_play=agent_play,
            actor=ws["owner"],
        )
    seeds = {}
    for ws in (a, b):
        seeds[ws["fid"]] = secrets.token_hex(32)
        await arena.commit_seed(
            ws["id"],
            match_id,
            commitment=engine.seed_commitment(seeds[ws["fid"]]),
            actor=ws["owner"],
        )
    for ws in (a, b):
        await arena.reveal_seed(ws["id"], match_id, seed=seeds[ws["fid"]], actor=ws["owner"])
    return seeds


async def _play_out(a, b, match_id: str, *, actor_key="owner") -> dict:
    """Shove every hand: all in or call, until the match ends."""
    by_seat = {0: a, 1: b}
    for _ in range(500):
        view = await arena.get_match(a["id"], match_id)
        if view["state"] != "PLAYING":
            return view
        to_act = view["game"]["hand"]["to_act"]
        ws = by_seat[to_act]
        mine = await arena.get_match(ws["id"], match_id)
        legal = {x["type"] for x in mine["game"]["hand"]["legal"]}
        action = {"type": "all_in"} if "all_in" in legal else {"type": "call"}
        if "all_in" not in legal and "call" not in legal:
            action = {"type": "check"}
        await arena.act(
            ws["id"], match_id, seq=mine["action_seq"], action=action, actor=ws[actor_key]
        )
    raise AssertionError("the match did not end")


async def test_a_match_from_queue_to_settlement(client, admin_headers, arena_on):
    """RR-04 and RR-09: matched, authorized, fair seeds, played, verified,
    settled once; the winner nets exactly the stake, nothing is created."""
    a, b, sa, sb, mid, match = await _seated(client, admin_headers, "pm")
    assert match["state"] == "MATCHED" and match["server_seed"] is None
    assert "no economic value" in match["notice"]
    # Only an owner authorizes, and only the contract as shown.
    assert (
        await _code(
            arena.authorize(a["id"], mid, contract_hash=match["contract_hash"], actor=a["person"])
        )
        == "FED_E_APPROVAL_NOT_HUMAN"
    )
    assert (
        await _code(arena.authorize(a["id"], mid, contract_hash="0" * 64, actor=a["owner"]))
        == "FED_E_MATCH_STATE"
    )
    # Someone not seated sees nothing.
    c = await _ws(client, admin_headers, "pmc")
    assert await _code(arena.get_match(c["id"], mid)) == "FED_E_MATCH_UNKNOWN"

    await arena.authorize(a["id"], mid, contract_hash=match["contract_hash"], actor=a["owner"])
    assert await _unreserved(sa["spec_id"]) == 4 * M  # the stake is held
    await arena.authorize(b["id"], mid, contract_hash=match["contract_hash"], actor=b["owner"])
    view = await arena.get_match(a["id"], mid)
    assert view["state"] == "DEMO_RESERVED"
    assert [h["to"] for h in view["history"]] == ["MATCHED", "OWNER_AUTHORIZED", "DEMO_RESERVED"]
    # Commit, then a wrong reveal is refused.
    seeds = {ws["fid"]: secrets.token_hex(32) for ws in (a, b)}
    # An agent may not play unless its owner allowed it.
    assert (
        await _code(arena.commit_seed(a["id"], mid, commitment="ab" * 32, actor=a["agent"]))
        == "FED_E_APPROVAL_NOT_HUMAN"
    )
    for ws in (a, b):
        await arena.commit_seed(
            ws["id"], mid, commitment=engine.seed_commitment(seeds[ws["fid"]]), actor=ws["owner"]
        )
    assert (
        await _code(arena.reveal_seed(a["id"], mid, seed="cd" * 32, actor=a["owner"]))
        == "FED_E_ARENA_ILLEGAL"
    )
    for ws in (a, b):
        await arena.reveal_seed(ws["id"], mid, seed=seeds[ws["fid"]], actor=ws["owner"])
    view_a = await arena.get_match(a["id"], mid)
    view_b = await arena.get_match(b["id"], mid)
    assert view_a["state"] == "PLAYING" and view_a["server_seed"] is None
    assert view_a["game"]["hand"]["my_cards"] != view_b["game"]["hand"]["my_cards"]
    assert set(view_a["game"]["hand"]["my_cards"]).isdisjoint(view_b["game"]["hand"]["my_cards"])

    # Duplicate and stale actions (RR-08).
    first_seat = view_a["game"]["hand"]["to_act"]
    actor_ws = a if first_seat == 0 else b
    other = b if first_seat == 0 else a
    assert (
        await _code(
            arena.act(other["id"], mid, seq=0, action={"type": "call"}, actor=other["owner"])
        )
        == "FED_E_ARENA_ILLEGAL"  # not their turn
    )
    assert (
        await _code(
            arena.act(
                actor_ws["id"],
                mid,
                seq=0,
                action={"type": "raise", "amount": 3},
                actor=actor_ws["owner"],
            )
        )
        == "FED_E_ARENA_ILLEGAL"
    )
    done = await arena.act(
        actor_ws["id"], mid, seq=0, action={"type": "call"}, actor=actor_ws["owner"]
    )
    assert done["replayed"] is False and done["action_seq"] == 1
    again = await arena.act(
        actor_ws["id"], mid, seq=0, action={"type": "call"}, actor=actor_ws["owner"]
    )
    assert again["replayed"] is True and again["action_seq"] == 1
    assert (
        await _code(
            arena.act(actor_ws["id"], mid, seq=0, action={"type": "fold"}, actor=actor_ws["owner"])
        )
        == "FED_E_ARENA_STALE"
    )
    assert (
        await _code(
            arena.act(other["id"], mid, seq=5, action={"type": "check"}, actor=other["owner"])
        )
        == "FED_E_ARENA_STALE"
    )

    final = await _play_out(a, b, mid)
    assert final["state"] in ("RESULT_VERIFIED", "CLOSED")
    # Settle (dispute window is 0 here) - and only once.
    await arena.settle_due()
    await arena.settle_due()
    final = await arena.get_match(a["id"], mid)
    assert final["state"] == "CLOSED" and final["server_seed"] is not None
    states = [h["to"] for h in final["history"]]
    assert states[-4:] == ["PLAYING", "RESULT_VERIFIED", "DEMO_SETTLED", "CLOSED"]
    proof = final["result"]["proof"]
    assert proof["economic_value"] == "none"
    # Anyone with the record can replay it: seeds, commitments, signatures.
    check = await arena.verify(mid)
    assert check["valid"], check["checks"]
    assert len(final["result"]["signatures"]) == 2

    winner = proof["winner_seat"]
    assert winner in (0, 1)  # all-in every hand: someone busts within the hand limit
    w, lo = (a, b) if winner == 0 else (b, a)
    ws_spec = sa if winner == 0 else sb
    lo_spec = sb if winner == 0 else sa
    # RR-09: the winner has its own stake back plus a voucher for exactly the stake.
    assert await _unreserved(ws_spec["spec_id"]) == 5 * M
    assert await _unreserved(lo_spec["spec_id"]) == 4 * M
    held = (await rights.list_vouchers(w["id"]))["held"]
    assert len(held) == 1 and held[0]["face_amount"] == M and held[0]["origin"] == "arena"
    assert held[0]["issuer"] == lo["fid"] and held[0]["redeemable"] is False
    assert (await rights.wallet(w["fid"]))["voucher_tokens_held"] == M
    # An arena voucher is an obligation: its issuer cannot revoke it.
    assert (
        await _code(rights.revoke_voucher(lo["id"], held[0]["voucher_id"], actor=lo["owner"]))
        == "FED_E_VOUCHER_STATE"
    )
    obligations = await arena.list_obligations(lo["id"])
    assert obligations["owed_by_me"][0]["state"] == "PENDING"
    assert obligations["owed_by_me"][0]["legal_debt"] is False
    async with async_session_factory() as session:
        settled = await session.scalar(
            text("SELECT count(*) FROM federation_arena_settlements WHERE match_id = :m"),
            {"m": mid},
        )
    assert settled == 1
    assert (await rights.is_balanced())["ok"]
    # Chips never reached any ledger: SIM is untouched.
    assert (await ledger.wallet(a["fid"]))["available"] == get_settings().federation_sim_allowance


async def test_one_controller_is_never_matched_with_itself(client, admin_headers, arena_on):
    """RR-11: linked workspaces wait instead of being matched."""
    a, b = await _ws(client, admin_headers, "la"), await _ws(client, admin_headers, "lb")
    await integrity.link(a["fid"], b["fid"], reason="same team, two workspaces", operator="ops")
    sa, sb = await _mock_spec(a), await _mock_spec(b)
    stake = 123_457  # a stake nobody else in this suite uses
    await arena.join_queue(a["id"], spec_id=sa["spec_id"], stake=stake, actor=a["person"])
    again = await arena.join_queue(b["id"], spec_id=sb["spec_id"], stake=stake, actor=b["person"])
    assert again["state"] == "WAITING"
    assert (
        await _code(
            arena.join_queue(a["id"], spec_id=sa["spec_id"], stake=stake, actor=a["person"])
        )
        == "FED_E_MATCH_STATE"
    )
    await arena.leave_queue(a["id"], actor=a["person"])
    await arena.leave_queue(b["id"], actor=b["person"])


async def test_timeouts_aborts_disputes_and_voids(client, admin_headers, arena_on):
    """RR-08: a turn timeout acts for the seat; a setup that stalls times
    out with the stakes back; a dispute holds settlement; a void returns."""
    a, b, sa, sb, mid, match = await _seated(client, admin_headers, "to", stake=2 * M)
    await _ready(a, b, mid, match["contract_hash"])
    await _sql(
        "UPDATE federation_arena_matches SET turn_deadline = NOW() - interval '1 second' "
        "WHERE match_id = :m",
        m=mid,
    )
    assert await arena.tick() >= 1
    view = await arena.get_match(a["id"], mid)
    assert view["action_seq"] == 1 and view["game"]["timeouts"] in ([1, 0], [0, 1])
    # A concession ends it; the dispute holds the result.
    await arena.leave(b["id"], mid, actor=b["owner"])
    view = await arena.get_match(a["id"], mid)
    assert view["state"] == "RESULT_VERIFIED" and view["result"]["proof"]["reason"] == "conceded"
    await arena.dispute(b["id"], mid, reason="the timeout fired too early", actor=b["owner"])
    await arena.settle_due()
    assert (await arena.get_match(a["id"], mid))["state"] == "DISPUTED"
    voided = await arena.resolve(mid, outcome="void", note="host clock issue", operator="ops")
    assert voided["state"] == "ABORTED"
    assert await _unreserved(sa["spec_id"]) == 5 * M and await _unreserved(sb["spec_id"]) == 5 * M

    # A setup that never finishes times out and returns what was held.
    c, d, sc, sd, mid2, match2 = await _seated(client, admin_headers, "su", stake=3 * M)
    await arena.authorize(c["id"], mid2, contract_hash=match2["contract_hash"], actor=c["owner"])
    assert await _unreserved(sc["spec_id"]) == 2 * M
    await _sql(
        "UPDATE federation_arena_matches SET setup_deadline = NOW() - interval '1 second' "
        "WHERE match_id = :m",
        m=mid2,
    )
    await arena.tick()
    view = await arena.get_match(c["id"], mid2)
    assert view["state"] == "TIMED_OUT" and "waiting on seats [1]" in view["history"][-1]["note"]
    assert await _unreserved(sc["spec_id"]) == 5 * M
    # Leaving before play aborts.
    e_, f_, se, sf, mid3, match3 = await _seated(client, admin_headers, "lv", stake=4 * M)
    await arena.leave(f_["id"], mid3, actor=f_["person"])
    assert (await arena.get_match(e_["id"], mid3))["state"] == "ABORTED"
    assert (await rights.is_balanced())["ok"]


async def test_agents_play_when_the_owner_allowed_it(client, admin_headers, arena_on):
    a, b, sa, sb, mid, match = await _seated(client, admin_headers, "ag", stake=5 * M)
    await _ready(a, b, mid, match["contract_hash"], agent_play=True)
    final = await _play_out(a, b, mid, actor_key="agent")
    assert final["state"] in ("RESULT_VERIFIED", "CLOSED")
    actors = set()
    async with async_session_factory() as session:
        for r in (
            await session.execute(
                text("SELECT actor FROM federation_arena_actions WHERE match_id = :m"), {"m": mid}
            )
        ).all():
            actors.add(r[0].split(":")[0])
    assert actors == {"agent"}


# ── R3: obligations and the restriction ───────────────────────────────────


async def test_an_unfulfilled_obligation_restricts_only_new_matches(
    client, admin_headers, arena_on
):
    """RR-05/RR-10: facts -> review (no restriction) -> excuse or confirm ->
    restricted from new matches only -> compensate -> restored."""
    a, b, sa, sb, mid, match = await _seated(client, admin_headers, "ob")
    await _ready(a, b, mid, match["contract_hash"])
    final = await _play_out(a, b, mid)
    await arena.settle_due()
    winner = final["result"]["proof"]["winner_seat"]
    w, lo = (a, b) if winner == 0 else (b, a)
    lo_spec = sb if winner == 0 else sa
    await _connect(w, lo)  # the winner may delegate to the loser
    held = (await rights.list_vouchers(w["id"]))["held"][0]
    ob_id = (await arena.list_obligations(lo["id"]))["owed_by_me"][0]["obligation_id"]

    async def _refused_request(key: str) -> None:
        job = await _bridge_job(w, lo, 250_000)
        await rights.reserve(
            w["id"],
            voucher_id=held["voucher_id"],
            federation_job_id=job,
            amount=250_000,
            idempotency_key=key,
            actor=w["agent"],
        )
        await arena.obligations_advance()
        assert (await arena.get_obligation(ob_id))["state"] == "DUE"
        await delegations.act(lo["id"], job, "reject", actor=lo["owner"])
        await rights.settle_reservations()
        await arena.obligations_advance()

    # 1. A refused delegation is a fact: review, not a restriction.
    await _refused_request("first-request")
    ob = await arena.get_obligation(ob_id)
    assert ob["state"] == "OVERDUE_REVIEW" and ob["facts"][0]["fact"] == "delegation REJECTED"
    assert ob["restriction"] is None
    # A report filed against the loser restricts nothing by itself either.
    await governance.file_report(
        w["id"],
        accused=lo["fid"],
        category=REPORT_CATEGORY,
        statement="they refused the work they owe",
        actor=w["owner"],
    )
    sx = await arena.join_queue(
        lo["id"], spec_id=lo_spec["spec_id"], stake=200_001, actor=lo["person"]
    )
    assert sx["state"] == "WAITING"
    await arena.leave_queue(lo["id"], actor=lo["person"])
    # The operator excuses it (an outage): back to pending, nothing recorded against them.
    await arena.decide(ob_id, decision="excuse", note="provider outage that day", operator="ops")
    assert (await arena.get_obligation(ob_id))["state"] == "PENDING"

    # 2. Refused again; the obligor answers; the operator confirms the default.
    await _refused_request("second-request")
    assert (await arena.get_obligation(ob_id))["state"] == "OVERDUE_REVIEW"
    await arena.state_case(lo["id"], ob_id, statement="we were over capacity", actor=lo["person"])
    ob = await arena.decide(ob_id, decision="confirm", note="refused twice", operator="ops")
    assert ob["state"] == "RESTRICTED" and ob["restriction"]["state"] == "ACTIVE"
    assert ob["restriction"]["scope"] == "new arena matches only"
    assert [h["to"] for h in ob["history"]][-2:] == ["CONFIRMED_UNFULFILLED", "RESTRICTED"]
    assert (
        await _code(
            arena.join_queue(
                lo["id"], spec_id=lo_spec["spec_id"], stake=200_002, actor=lo["person"]
            )
        )
        == "FED_E_ARENA_RESTRICTED"
    )
    credit = await arena.credit(lo["fid"])
    assert credit["restricted"] and credit["confirmed_defaults_ever"] == 1
    assert credit["score"] is None
    # Everything else still works: the restricted workspace can delegate and chat.
    other = await _ws(client, admin_headers, "obx")
    await _connect(lo, other)
    sent = await delegations.offer(
        lo["id"], peer_workspace=other["fid"], terms=_terms(), actor=lo["owner"]
    )
    assert sent["delegation"]["state"] == "OFFERED"

    # 3. Equivalent compensation restores eligibility, automatically.
    ob = await arena.compensate(lo["id"], ob_id, spec_id=lo_spec["spec_id"], actor=lo["owner"])
    assert ob["state"] == "REMEDIATED" and ob["restriction"]["state"] == "LIFTED"
    back = await arena.join_queue(
        lo["id"], spec_id=lo_spec["spec_id"], stake=200_003, actor=lo["person"]
    )
    assert back["state"] == "WAITING"
    await arena.leave_queue(lo["id"], actor=lo["person"])
    comp = [v for v in (await rights.list_vouchers(w["id"]))["held"] if v["origin"] == "compensate"]
    assert len(comp) == 1 and comp[0]["face_amount"] == 250_000
    assert (await arena.credit(lo["fid"]))["restricted"] is False
    assert (await rights.is_balanced())["ok"]


async def test_an_unused_voucher_is_not_the_obligors_fault(client, admin_headers, arena_on):
    a, b, sa, sb, mid, match = await _seated(client, admin_headers, "uv")
    await _ready(a, b, mid, match["contract_hash"])
    final = await _play_out(a, b, mid)
    await arena.settle_due()
    winner = final["result"]["proof"]["winner_seat"]
    w, lo = (a, b) if winner == 0 else (b, a)
    held = (await rights.list_vouchers(w["id"]))["held"][0]
    await rights.waive_voucher(w["id"], held["voucher_id"], actor=w["owner"])
    await arena.obligations_advance()
    ob = (await arena.list_obligations(lo["id"]))["owed_by_me"][0]
    assert ob["state"] == "RESOLVED" and "not the obligor's fault" in ob["history"][-1]["note"]


# ── HTTP ──────────────────────────────────────────────────────────────────


ADMIN = {"Authorization": "Bearer test-admin"}


@pytest.fixture(scope="module")
async def arena_http(api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url
    env = {
        "FEDERATION_ENABLED": "true",
        "FEDERATION_RESOURCE_RIGHTS_ENABLED": "true",
        "FEDERATION_ARENA_ENABLED": "true",
        "AUTH_IP_MAX_ATTEMPTS_PER_MINUTE": "1000",
        "FEDERATION_KEK": base64.b64encode(os.urandom(32)).decode(),
    }
    async with run_server(env, ".server-arena.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as http:
            yield http


async def test_disabled_routes_answer_404(client):
    """The default deployment: every new route is closed."""
    for method, path in (
        ("GET", "/v1/federation/rights/wallet"),
        ("POST", "/v1/federation/arena/queue"),
        ("GET", "/v1/admin/federation/rights/ledger/check"),
    ):
        response = await client.request(method, path, headers=ADMIN, json={})
        assert response.status_code in (401, 404), response.text
        if response.status_code == 404:
            assert response.json()["error"]["code"] == "federation_disabled"


async def test_resource_rights_and_arena_over_http(arena_http):
    from tests.test_federation_api import _workspace

    a, b = await _workspace(arena_http, "ha"), await _workspace(arena_http, "hb")
    fids = []
    for ws in (a, b):
        r = await arena_http.post("/v1/federation/enable", headers=ws["owner"])
        assert r.status_code in (200, 201), r.text
        fids.append(r.json()["identity"]["federation_id"])
    specs = []
    for ws in (a, b):
        r = await arena_http.post(
            "/v1/federation/rights/specs",
            headers=ws["owner"],
            json={
                "resource_class": "MOCK_DEMO",
                "model_family": "mock",
                "allowed_use": "demo",
                "declared_tokens": 3 * M,
                "measured_source": "mock",
            },
        )
        assert r.status_code == 201, r.text
        specs.append(r.json()["spec_id"])
    # An agent key cannot declare; a float amount is refused at the edge.
    r = await arena_http.post(
        "/v1/federation/rights/specs",
        headers=a["key"],
        json={
            "resource_class": "MOCK_DEMO",
            "model_family": "m",
            "allowed_use": "u",
            "declared_tokens": M,
            "measured_source": "mock",
        },
    )
    assert r.status_code == 403 and r.json()["error"]["details"]["federation_code"] == (
        "FED_E_APPROVAL_NOT_HUMAN"
    )
    r = await arena_http.post(
        "/v1/federation/rights/vouchers",
        headers=a["owner"],
        json={
            "spec_id": specs[0],
            "beneficiary": fids[1],
            "amount": 1.5,
            "usage_scope": "float amount",
        },
    )
    assert r.status_code == 422
    wallet = (await arena_http.get("/v1/federation/rights/wallet", headers=a["key"])).json()
    assert wallet["redeemable"] is False and wallet["capacity"][0]["unreserved_tokens"] == 3 * M
    stake = 777_777
    r1 = await arena_http.post(
        "/v1/federation/arena/queue", headers=a["owner"], json={"spec_id": specs[0], "stake": stake}
    )
    r2 = await arena_http.post(
        "/v1/federation/arena/queue", headers=b["owner"], json={"spec_id": specs[1], "stake": stake}
    )
    assert r1.json()["state"] == "WAITING" and r2.json()["state"] == "MATCHED", r2.text
    match = r2.json()["match"]
    mid = match["match_id"]
    for ws in (a, b):
        r = await arena_http.post(
            f"/v1/federation/arena/matches/{mid}/authorize",
            headers=ws["owner"],
            json={"contract_hash": match["contract_hash"]},
        )
        assert r.status_code == 200, r.text
    seeds = [secrets.token_hex(32), secrets.token_hex(32)]
    for ws, seed in zip((a, b), seeds, strict=True):
        r = await arena_http.post(
            f"/v1/federation/arena/matches/{mid}/commit",
            headers=ws["owner"],
            json={"commitment": engine.seed_commitment(seed)},
        )
        assert r.status_code == 200, r.text
    for ws, seed in zip((a, b), seeds, strict=True):
        r = await arena_http.post(
            f"/v1/federation/arena/matches/{mid}/reveal", headers=ws["owner"], json={"seed": seed}
        )
        assert r.status_code == 200, r.text
    assert r.json()["state"] == "PLAYING"
    r = await arena_http.post(f"/v1/federation/arena/matches/{mid}/leave", headers=a["owner"])
    assert r.json()["state"] == "RESULT_VERIFIED"
    v = (
        await arena_http.get(f"/v1/federation/arena/matches/{mid}/verify", headers=b["owner"])
    ).json()
    assert v["valid"] is True, v
    check = await arena_http.get("/v1/admin/federation/rights/ledger/check", headers=ADMIN)
    assert check.json()["ok"] is True
    # Nobody else can read a match (404, not 403: it does not exist for them).
    c = await _workspace(arena_http, "hc")
    await arena_http.post("/v1/federation/enable", headers=c["owner"])
    r = await arena_http.get(f"/v1/federation/arena/matches/{mid}", headers=c["owner"])
    assert r.status_code == 404
    _ = uuid  # keep the import for readers copying the helper
