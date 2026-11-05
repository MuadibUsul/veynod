"""Fulfilment proofs and multi-dimensional reputation (federation P4-1,
docs/federation/ICD_P4_REPUTATION.md).

A-02: fulfilment proofs can be verified and every dimension is there.
A-03: weights follow the depth of cooperation.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from app.services.federation import delegations, reputation
from app.services.federation.canonical import sha256_hex
from app.services.federation.errors import FederationError
from tests import test_federation_delegations as _bridge
from tests.test_federation_delegations import (
    RESULT,
    Tasks,
    _connect,
    _terms,
)
from tests.test_federation_delegations import _ws as _new_ws
from tests.test_federation_p1_gate import db_dsn

pytestmark = pytest.mark.integration

federation_on = _bridge.federation_on


async def season(fid: str, days: int = 60) -> None:
    """Age an identity so the P4-3 cold-start and new-requester rules do not
    apply: these tests are about the measures themselves."""
    conn = await asyncpg.connect(db_dsn())
    try:
        await conn.execute(
            "UPDATE federation_identities SET created_at = created_at - make_interval(days => $2) "
            "WHERE federation_id = $1",
            fid,
            days,
        )
    finally:
        await conn.close()


async def _ws(client, admin_headers, name: str) -> dict:
    ws = await _new_ws(client, admin_headers, name)
    await season(ws["fid"])
    return ws


async def _code(awaitable) -> str:
    with pytest.raises(FederationError) as err:
        await awaitable
    return err.value.code


async def _job(req, prov, outcome: str, *, revise: bool = False) -> str:
    sent = await delegations.offer(
        req["id"], peer_workspace=prov["fid"], terms=_terms(), actor=req["owner"]
    )
    job = sent["delegation"]["federation_job_id"]
    if outcome == "declined":
        await delegations.act(prov["id"], job, "reject", actor=prov["owner"])
        return job
    await delegations.act(
        prov["id"], job, "accept", actor=prov["owner"], create_local_task=Tasks().create
    )
    await delegations.act(prov["id"], job, "submit", actor=prov["agent"], result=RESULT)
    if revise:
        await delegations.act(req["id"], job, "revise", actor=req["owner"], note="Add sources.")
        await delegations.act(prov["id"], job, "submit", actor=prov["agent"], result=RESULT)
    if outcome == "accepted":
        await delegations.act(req["id"], job, "accept_result", actor=req["owner"])
    else:
        await delegations.act(req["id"], job, "dispute", actor=req["owner"], note="Wrong totals.")
    return job


async def _age_proofs(provider_fid: str, requester_fid: str, days: int) -> None:
    """Move a pair's first job back in time (the proofs are immutable, so the
    guard is lifted for this test only)."""
    conn = await asyncpg.connect(db_dsn())
    try:
        async with conn.transaction():
            await conn.execute(
                "ALTER TABLE federation_proofs DISABLE TRIGGER trg_federation_proofs_guard"
            )
            await conn.execute(
                "UPDATE federation_proofs SET closed_at = closed_at - make_interval(days => $3) "
                "WHERE provider_fid = $1 AND requester_fid = $2 AND closed_at = ("
                "SELECT min(closed_at) FROM federation_proofs WHERE provider_fid = $1 AND "
                "requester_fid = $2)",
                provider_fid,
                requester_fid,
                days,
            )
            await conn.execute(
                "ALTER TABLE federation_proofs ENABLE TRIGGER trg_federation_proofs_guard"
            )
    finally:
        await conn.close()


async def test_fulfilment_proofs_verify_and_every_dimension_is_there(
    client, admin_headers, federation_on
):
    """A-02."""
    prov = await _ws(client, admin_headers, "pv")
    r1, r2 = await _ws(client, admin_headers, "r1"), await _ws(client, admin_headers, "r2")
    for req in (r1, r2):
        await _connect(req, prov)
    await _job(r1, prov, "accepted")
    await _job(r1, prov, "accepted", revise=True)
    await _job(r2, prov, "disputed")
    await _job(r2, prov, "accepted")
    await _job(r2, prov, "declined")  # never taken on: no proof
    assert await reputation.issue_due() >= 4

    items = await reputation.proofs(prov["fid"])
    assert len(items) == 4
    for item in items:
        checked = await reputation.verify_proof(item["proof"])
        assert checked["valid"] and checked["provider"] == prov["fid"]
        payload = item["proof"]["payload"]
        assert payload["requester"] in (r1["fid"], r2["fid"])
        # No content crosses into a proof: ids, hashes, times, outcome.
        assert set(payload) == {
            "v",
            "federation_job_id",
            "requester",
            "provider",
            "outcome",
            "data_classification",
            "external_side_effects",
            "acceptance_hash",
            "result_sha256",
            "revisions",
            "offered_at",
            "accepted_at",
            "submitted_at",
            "closed_at",
        }
        assert "Summarise" not in str(payload) and "Demand grows" not in str(payload)

    # Tampering is caught: an outcome flipped, a signature from someone else.
    forged = copy.deepcopy(items[0]["proof"])
    forged["payload"]["outcome"] = (
        "accepted" if forged["payload"]["outcome"] == "disputed" else "disputed"
    )
    assert await _code(reputation.verify_proof(forged)) == "FED_E_PROOF_INVALID"
    forged["payload_sha256"] = sha256_hex(forged["payload"])
    assert await _code(reputation.verify_proof(forged)) == "FED_E_BAD_SIGNATURE"
    assert await _code(reputation.verify_proof({"type": "chat.message"})) == ("FED_E_PROOF_INVALID")

    page = await reputation.profile(prov["fid"])
    assert page["single_score"] is None
    assert page["completion"]["counts"]["accepted"] == 3
    assert page["completion"]["counts"]["disputed"] == 1
    assert page["completion"]["samples"] == 4 and page["completion"]["rate"] == 1.0
    assert page["acceptance"]["samples"] == 4 and page["acceptance"]["enough_evidence"]
    # Weighted: r1's 2 jobs (0.5 + 1.0) accepted; r2's disputed first (0.5), accepted second (1.0).
    assert page["acceptance"]["weighted_hits"] == 2.5
    assert page["acceptance"]["weighted_total"] == 3.0
    assert page["acceptance"]["rate"] == round(2.5 / 3.0, 3)
    # First-pass: the revised one does not count.
    assert page["acceptance"]["first_pass"]["weighted_hits"] == 1.5
    assert page["response"]["samples"] == 5 and page["response"]["enough_evidence"]
    assert page["skills"]["published"] == 0 and page["skills"]["average"] is None
    assert {k: page["governance"][k] for k in ("sanctions_in_force", "sanctions_ever")} == {
        "sanctions_in_force": 0,
        "sanctions_ever": 0,
    }
    assert page["governance"]["open_reports"] == "not shown"
    assert page["governance"]["complaints"]["resolved"] == 0
    assert page["cooperation"]["counterparts"] == 2
    assert page["cooperation"]["repeat_counterparts"] == 2
    assert set(page["evidence"]) == {
        "signed_by_counterparts",
        "measured_by_platform",
        "from_other_workspaces",
        "platform_decisions",
    }

    # Too little evidence reads as such, not as a weak number.
    fresh = await _ws(client, admin_headers, "fr")
    empty = await reputation.profile(fresh["fid"])
    assert empty["completion"]["rate"] is None and not empty["completion"]["enough_evidence"]
    assert empty["acceptance"]["rate"] is None and empty["proofs"] == 0


async def test_weights_follow_the_depth_of_cooperation(client, admin_headers, federation_on):
    """A-03."""
    prov = await _ws(client, admin_headers, "dp")
    deep, once = await _ws(client, admin_headers, "dd"), await _ws(client, admin_headers, "do")
    for req in (deep, once):
        await _connect(req, prov)
    for _ in range(6):
        await _job(deep, prov, "accepted")
    await _job(once, prov, "disputed")
    await reputation.issue_due()
    # Six jobs closed within seconds look like farming (P4-3); an operator
    # confirms this pair as genuine so the depth rules can be seen alone.
    from app.services.federation import integrity

    await integrity.review_pair(
        deep["fid"], prov["fid"], verdict="genuine", note="Long-standing client.", operator="ops"
    )
    # The deep pair has worked together for more than 30 days.
    await _age_proofs(prov["fid"], deep["fid"], 40)

    items = await reputation.proofs(prov["fid"])
    by_requester: dict[str, list[float]] = {}
    for item in reversed(items):  # oldest first
        by_requester.setdefault(item["proof"]["payload"]["requester"], []).append(item["weight"])
    # A single job counts half.
    assert by_requester[once["fid"]] == [0.5]
    # Deeper cooperation counts more, but one requester is capped at 5.0 in total:
    # raw 0.5 + 1 + 1 + 1 + 1.5 + 1.5 = 6.5, scaled to 5.0.
    deep_weights = by_requester[deep["fid"]]
    assert len(deep_weights) == 6
    assert round(sum(deep_weights), 2) == 5.0
    assert deep_weights[0] < deep_weights[1] < deep_weights[4]
    assert deep_weights[4] == deep_weights[5]

    page = await reputation.profile(prov["fid"])
    # One dispute from a one-off requester weighs less than six accepted jobs
    # from a long partner: 5.0 / 5.5, not 6 / 7.
    assert page["acceptance"]["weighted_total"] == 5.5
    assert page["acceptance"]["rate"] == round(5.0 / 5.5, 3)
    assert page["cooperation"]["longest_partnership_days"] >= 40
    assert page["weighting"]["cap_per_requester"] == 5.0

    # The pure function, for the record of the rules.
    now = datetime.now(timezone.utc)
    rows = [{"requester_fid": "a", "closed_at": now + timedelta(days=i * 10)} for i in range(6)] + [
        {"requester_fid": "b", "closed_at": now}
    ]
    weights = reputation.weigh(rows)
    assert weights[-1] == 0.5
    assert weights[0] < weights[1]
