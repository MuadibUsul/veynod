"""Reputation integrity: self-dealing, collusion, cold start (federation P4-3,
docs/federation/ICD_P4_REPUTATION.md §10).

A-02 (F-025): two workspaces of one controller farm work between them; the
system flags it and their proofs stop counting. A-03: volume alone does not
easily buy credibility.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.database import async_session_factory
from app.services.federation import directory, identity, integrity, reputation
from app.services.federation.grants import Actor
from tests import test_federation_delegations as _bridge
from tests.test_federation_delegations import RESULT, Tasks, _connect, _terms
from tests.test_federation_delegations import _ws as _new_ws
from tests.test_federation_reputation import _ws, season

pytestmark = pytest.mark.integration

federation_on = _bridge.federation_on


async def _job(req, prov, outcome: str = "accepted") -> str:
    from app.services.federation import delegations

    sent = await delegations.offer(
        req["id"], peer_workspace=prov["fid"], terms=_terms(), actor=req["owner"]
    )
    job = sent["delegation"]["federation_job_id"]
    await delegations.act(
        prov["id"], job, "accept", actor=prov["owner"], create_local_task=Tasks().create
    )
    await delegations.act(prov["id"], job, "submit", actor=prov["agent"], result=RESULT)
    action = "accept_result" if outcome == "accepted" else "dispute"
    await delegations.act(req["id"], job, action, actor=req["owner"], note="ok")
    return job


async def _sibling(of: dict, name: str, *, seasoned: bool = True) -> dict:
    """Another workspace in the same tenant: the same controller."""
    from app.services.workspace import create_workspace

    async with async_session_factory() as session:
        tenant = await session.scalar(
            text("SELECT tenant_id FROM workspaces WHERE id = CAST(:w AS uuid)"), {"w": of["id"]}
        )
        created = await create_workspace(
            session, name=f"{name}-sibling", tier="free", tenant_id=tenant, operator_managed=True
        )
        await session.commit()
    workspace_id = str(created.workspace.id)
    view = await identity.enable_workspace(workspace_id, actor=f"user:owner-{name}")
    ws = {
        "id": workspace_id,
        "fid": view["federation_id"],
        "owner": Actor("user", f"owner-{name}", is_owner=True),
        "person": Actor("user", f"member-{name}"),
        "agent": Actor("agent", f"bot-{name}"),
    }
    if seasoned:
        await season(ws["fid"])
    return ws


def _by_fid(items, fid):
    return next(i for i in items if i["fid"] == fid)


async def test_one_controller_cannot_farm_its_own_reputation(client, admin_headers, federation_on):
    """A-02 / F-025."""
    provider = await _ws(client, admin_headers, "fa")
    puppet = await _sibling(provider, "fa")
    honest = await _ws(client, admin_headers, "fh")
    assert await integrity.related(provider["fid"], puppet["fid"]) == ["same_subject"]
    assert await integrity.related(provider["fid"], honest["fid"]) == []
    for req in (puppet, honest):
        await _connect(req, provider)
    for _ in range(4):
        await _job(puppet, provider)
    await _job(honest, provider)
    await reputation.issue_due()

    page = await reputation.profile(provider["fid"])
    check = page["integrity"]
    assert "self_dealing_excluded" in check["flags"]
    assert check["excluded"]["same_controller"] == 4
    # Only the honest job counts: one proof is not enough evidence for a rate.
    assert page["acceptance"]["samples"] == 1 and page["acceptance"]["rate"] is None
    assert page["acceptance"]["weighted_total"] == 0.5
    mine = await reputation.proofs(provider["fid"], viewer_fid=provider["fid"])
    farmed = [p for p in mine if p["proof"]["payload"]["requester"] == puppet["fid"]]
    assert len(farmed) == 4
    assert {p["counted_as"] for p in farmed} == {"same_controller"}
    assert {p["weight"] for p in farmed} == {0.0}

    # In the directory the farmed volume buys nothing, and the reason is shown.
    tag = f"t{provider['fid'][-8:]}"
    await directory.set_listing(
        provider["id"],
        listed=True,
        headline="Audits",
        capabilities=[tag],
        availability="open",
        actor=provider["owner"],
    )
    [card] = (await directory.search(capability=tag))["items"]
    evidence = next(e for e in card["rank"]["explanation"] if e["factor"] == "evidence")
    assert evidence["value"] < 0.55
    assert "4 proof(s) not counted" in evidence["why"]
    assert card["reputation"]["integrity"]["excluded"]["same_controller"] == 4

    # Different subjects that turn out to share a controller: work flowing
    # both ways is flagged and waits for an operator, who links them.
    c, d = await _ws(client, admin_headers, "fc"), await _ws(client, admin_headers, "fd")
    from app.services.federation import grants

    asked = await grants.request(
        c["id"],
        peer_workspace=d["fid"],
        outbound_scopes=["chat", "delegate"],
        inbound_scopes=["chat", "delegate"],
        reason="two-way work",
        actor=c["owner"],
    )
    await grants.approve(d["id"], asked["connection_id"], actor=d["owner"])
    for _ in range(2):
        await _job(c, d)
        await _job(d, c)
    await reputation.issue_due()
    page_d = await reputation.profile(d["fid"])
    assert page_d["integrity"]["excluded"]["pending_verification"] == 2
    assert "pending_verification" in page_d["integrity"]["flags"]
    queue = await integrity.anomalies()
    flagged = {(a["provider"], a["requester"]): a for a in queue}
    assert "two_way_flow" in flagged[(d["fid"], c["fid"])]["flags"]
    assert "two_way_flow" in flagged[(c["fid"], d["fid"])]["flags"]
    await integrity.review_pair(
        c["fid"],
        d["fid"],
        verdict="collusive",
        note="Same founder, mutual farming.",
        operator="ops-anna",
    )
    assert await integrity.related(c["fid"], d["fid"]) == ["operator_linked"]
    page_d = await reputation.profile(d["fid"])
    assert page_d["integrity"]["excluded"]["same_controller"] == 2
    # Linked both ways: the other direction stops counting too.
    page_c = await reputation.profile(c["fid"])
    assert page_c["integrity"]["excluded"]["same_controller"] == 2


async def test_volume_does_not_easily_buy_credibility(client, admin_headers, federation_on):
    """A-03."""
    farmed = await _ws(client, admin_headers, "vf")
    earned = await _ws(client, admin_headers, "ve")
    # Five brand-new addresses vouch for one provider; five established ones
    # for the other. Same volume, same outcomes.
    fresh = [await _new_ws(client, admin_headers, f"n{i}") for i in range(5)]
    known = [await _ws(client, admin_headers, f"k{i}") for i in range(5)]
    for req in fresh:
        await _connect(req, farmed)
        await _job(req, farmed)
    for req in known:
        await _connect(req, earned)
        await _job(req, earned)
    await reputation.issue_due()
    page_f = await reputation.profile(farmed["fid"])
    page_e = await reputation.profile(earned["fid"])
    assert page_f["integrity"]["discounted"]["fresh_requester"] == 5
    assert page_e["integrity"]["discounted"]["fresh_requester"] == 0
    assert page_f["completion"]["weighted_total"] == 0.625  # 5 x 0.5 x 0.25
    assert page_e["completion"]["weighted_total"] == 2.5
    ev_f, _ = directory._evidence(page_f)
    ev_e, _ = directory._evidence(page_e)
    assert ev_f < ev_e

    # Many quick jobs from one client wait for an operator before they count.
    quick = await _ws(client, admin_headers, "vq")
    await _connect(quick, earned)
    for _ in range(3):
        await _job(quick, earned)
    await reputation.issue_due()
    page = await reputation.profile(earned["fid"])
    assert page["integrity"]["excluded"]["pending_verification"] == 3
    assert any(
        a["provider"] == earned["fid"]
        and a["requester"] == quick["fid"]
        and "implausibly_fast" in a["flags"]
        for a in await integrity.anomalies()
    )
    before = page["completion"]["weighted_total"]
    await integrity.review_pair(
        quick["fid"],
        earned["fid"],
        verdict="genuine",
        note="Batch of small jobs, checked.",
        operator="ops-ben",
    )
    page = await reputation.profile(earned["fid"])
    assert page["integrity"]["excluded"]["pending_verification"] == 0
    assert page["completion"]["weighted_total"] > before

    # A brand-new provider is in its cold start: shown, but neutral in ranking.
    newcomer = await _new_ws(client, admin_headers, "vn")
    helper = await _ws(client, admin_headers, "vh")
    await _connect(helper, newcomer)
    for _ in range(3):
        await _job(helper, newcomer)
    await reputation.issue_due()
    await integrity.review_pair(
        helper["fid"],
        newcomer["fid"],
        verdict="genuine",
        note="Checked by hand.",
        operator="ops-ben",
    )
    page_n = await reputation.profile(newcomer["fid"])
    assert page_n["integrity"]["cold_start"] is True and page_n["integrity"]["cold_start_ends"]
    assert page_n["acceptance"]["rate"] == 1.0
    value, why = directory._evidence(page_n)
    assert value == directory.PRIOR and "cold start" in why

    # Several workspaces created together under one subject are flagged.
    base = await _ws(client, admin_headers, "vb")
    batch = [await _sibling(base, f"vb{i}", seasoned=False) for i in range(5)]
    page_b = await reputation.profile(batch[0]["fid"])
    assert "created_in_a_batch" in page_b["integrity"]["flags"]
