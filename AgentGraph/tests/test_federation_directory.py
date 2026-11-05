"""Federation directory and explainable ranking (federation P4-2,
docs/federation/ICD_P4_REPUTATION.md §8).

A-02: reputation moves a workspace in discovery and the rank explains itself.
A-03 (F-026): the directory does not leak private work.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.services.federation import delegations, directory, messaging, reputation
from app.services.federation.errors import FederationError
from tests import test_federation_delegations as _bridge
from tests.test_federation_delegations import RESULT, Tasks, _connect, _terms, _ws

pytestmark = pytest.mark.integration

federation_on = _bridge.federation_on


async def _code(awaitable) -> str:
    with pytest.raises(FederationError) as err:
        await awaitable
    return err.value.code


async def _job(req, prov, outcome: str, **terms) -> str:
    sent = await delegations.offer(
        req["id"], peer_workspace=prov["fid"], terms=_terms(**terms), actor=req["owner"]
    )
    job = sent["delegation"]["federation_job_id"]
    await delegations.act(
        prov["id"], job, "accept", actor=prov["owner"], create_local_task=Tasks().create
    )
    await delegations.act(prov["id"], job, "submit", actor=prov["agent"], result=RESULT)
    if outcome == "accepted":
        await delegations.act(req["id"], job, "accept_result", actor=req["owner"])
    else:
        await delegations.act(req["id"], job, "dispute", actor=req["owner"], note="Wrong.")
    return job


async def _list(ws, tag: str, headline: str, availability: str = "open") -> None:
    await directory.set_listing(
        ws["id"],
        listed=True,
        headline=headline,
        capabilities=[tag],
        availability=availability,
        actor=ws["owner"],
    )


def _fids(found) -> list[str]:
    return [item["fid"] for item in found["items"]]


async def test_reputation_moves_a_workspace_and_the_rank_explains_why(
    client, admin_headers, federation_on
):
    """A-02."""
    tag = f"t{uuid.uuid4().hex[:8]}"
    good = await _ws(client, admin_headers, "good")
    poor = await _ws(client, admin_headers, "poor")
    new = await _ws(client, admin_headers, "new")
    hidden = await _ws(client, admin_headers, "hidden")
    busy = await _ws(client, admin_headers, "busy")
    req = await _ws(client, admin_headers, "req")
    for prov in (good, poor, hidden):
        await _connect(req, prov)
    for _ in range(3):
        await _job(req, good, "accepted")
        await _job(req, poor, "disputed")
        await _job(req, hidden, "accepted")
    await reputation.issue_due()
    await _list(good, tag, "Grid demand research and summaries")
    await _list(poor, tag, "Research summaries")
    await _list(new, tag, "Fresh research team")
    await _list(busy, tag, "Research, fully booked", availability="busy")
    # hidden has the best record but never listed itself.
    assert (
        await _code(
            directory.set_listing(
                good["id"],
                listed=True,
                headline="x",
                capabilities=[tag],
                availability="open",
                actor=good["agent"],
            )
        )
        == "FED_E_APPROVAL_NOT_HUMAN"
    )

    found = await directory.search(capability=tag)
    assert hidden["fid"] not in _fids(found)
    order = _fids(found)
    assert order[0] == good["fid"]
    assert set(order) == {good["fid"], poor["fid"], new["fid"], busy["fid"]}
    by_fid = {item["fid"]: item for item in found["items"]}

    # Every result explains its place: factors, weights, contributions.
    for item in found["items"]:
        parts = {e["factor"]: e for e in item["rank"]["explanation"]}
        assert set(parts) == {"relevance", "evidence", "availability", "safety", "promotion"}
        total = sum(parts[f]["contribution"] for f in ("relevance", "evidence", "availability"))
        assert abs(total - item["rank"]["score"]) < 0.001
        assert parts["promotion"]["contribution"] == 0
        assert item["rank"]["position"] == order.index(item["fid"]) + 1
    good_ev = {e["factor"]: e for e in by_fid[good["fid"]]["rank"]["explanation"]}["evidence"]
    poor_ev = {e["factor"]: e for e in by_fid[poor["fid"]]["rank"]["explanation"]}["evidence"]
    new_ev = {e["factor"]: e for e in by_fid[new["fid"]]["rank"]["explanation"]}["evidence"]
    # Reputation moves the evidence factor; a newcomer is neutral, not penalised.
    assert good_ev["value"] > new_ev["value"] == 0.5 > poor_ev["value"]
    assert "3 signed proofs" in good_ev["why"] and "neutral" in new_ev["why"]
    # By evidence alone: the newcomers tie at the neutral 0.5 and the rank breaks the tie.
    assert _fids(await directory.search(capability=tag, sort="evidence")) == [
        good["fid"],
        new["fid"],
        busy["fid"],
        poor["fid"],
    ]
    assert found["formula"]["weights"] == {
        "relevance": 0.45,
        "evidence": 0.35,
        "availability": 0.20,
    }
    assert found["promoted"]["items"] == []

    # Filters: reputation, availability, price, words.
    assert _fids(await directory.search(capability=tag, min_acceptance=0.5)) == [good["fid"]]
    assert _fids(await directory.search(capability=tag, availability="busy")) == [busy["fid"]]
    assert (await directory.search(capability=tag, price="paid"))["items"] == []
    assert _fids(await directory.search(q=f"grid {tag}"))[0] == good["fid"]
    await _list(busy, tag, "Closed for now", availability="closed")
    assert busy["fid"] not in _fids(await directory.search(capability=tag))
    assert busy["fid"] in _fids(await directory.search(capability=tag, include_closed=True))
    page = await directory.entry(good["fid"])
    assert page["rank"]["explanation"][1]["factor"] == "evidence"
    assert page["reputation"]["acceptance"]["samples"] == 3


async def test_the_directory_does_not_leak_private_work(client, admin_headers, federation_on):
    """A-03 / F-026: a private job with a secret in it, then a proof and a listing."""
    prov = await _ws(client, admin_headers, "sp")
    req = await _ws(client, admin_headers, "sr")
    outsider = await _ws(client, admin_headers, "so")
    unlisted = await _ws(client, admin_headers, "su")
    await _connect(req, prov)
    secret = f"SECRET-PLAN-{uuid.uuid4().hex[:10]}"
    job = await _job(req, prov, "accepted", objective=f"Price the {secret} acquisition quietly.")
    await messaging.send(
        req["id"],
        peer_workspace=prov["fid"],
        text_body=f"Do not share {secret}.",
        actor=req["owner"],
    )
    await reputation.issue_due()
    tag = f"t{uuid.uuid4().hex[:8]}"
    await _list(prov, tag, "Valuations")

    found = await directory.search(capability=tag)
    entry = await directory.entry(prov["fid"], viewer_fid=outsider["fid"])
    for blob in (json.dumps(found), json.dumps(entry)):
        assert secret not in blob
        assert job not in blob
        # Who the provider worked for is not shown either, only how many.
        assert req["fid"] not in blob
    assert entry["reputation"]["cooperation"]["counterparts"] == 1

    # Proofs name both sides, so only the two sides see them.
    assert await reputation.proofs(prov["fid"], viewer_fid=outsider["fid"]) == []
    [mine] = await reputation.proofs(prov["fid"], viewer_fid=req["fid"])
    assert mine["proof"]["payload"]["federation_job_id"] == job
    assert secret not in json.dumps(mine)
    assert len(await reputation.proofs(prov["fid"], viewer_fid=prov["fid"])) == 1

    # An unlisted workspace's record is visible to itself and to those it is
    # connected with, not to strangers.
    await _connect(req, unlisted)
    assert await directory.can_view(unlisted["fid"], outsider["fid"]) is False
    assert await directory.can_view(unlisted["fid"], unlisted["fid"]) is True
    assert await directory.can_view(unlisted["fid"], req["fid"]) is True
    assert await _code(directory.entry(unlisted["fid"], viewer_fid=outsider["fid"])) == (
        "FED_E_NOT_LISTED"
    )
    # Unlisting takes it out.
    await directory.set_listing(
        prov["id"],
        listed=False,
        headline="",
        capabilities=[],
        availability="open",
        actor=prov["owner"],
    )
    assert prov["fid"] not in _fids(await directory.search(capability=tag))
    assert await _code(directory.entry(prov["fid"], viewer_fid=outsider["fid"])) == (
        "FED_E_NOT_LISTED"
    )
