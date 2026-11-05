"""Directory privacy boundary and the public complaint record (federation P4-4,
docs/federation/PRIVACY.md).

A-02 (F-026): a private chat holding a secret, and a job that produced a
fulfilment proof: the directory shows only the permitted summary and nothing
leads back to the chat or the job. A-03: the privacy policy is complete:
disclosure choices, what is never shown, and resolved complaints only.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.api import federation_routes as routes
from app.services.federation import (
    delegations,
    directory,
    governance,
    integrity,
    messaging,
    privacy,
    reputation,
)
from app.services.federation.errors import FederationError
from tests import test_federation_delegations as _bridge
from tests.test_federation_delegations import Tasks, _connect, _terms
from tests.test_federation_reputation import _ws

pytestmark = pytest.mark.integration

federation_on = _bridge.federation_on


async def _code(awaitable) -> str:
    with pytest.raises(FederationError) as err:
        await awaitable
    return err.value.code


async def _job(req, prov, *, secret: str = "") -> str:
    sent = await delegations.offer(
        req["id"],
        peer_workspace=prov["fid"],
        terms=_terms(objective=f"Value the {secret or 'public'} portfolio for the board."),
        actor=req["owner"],
    )
    job = sent["delegation"]["federation_job_id"]
    await delegations.act(
        prov["id"], job, "accept", actor=prov["owner"], create_local_task=Tasks().create
    )
    result = {
        "summary": f"Valuation done for {secret or 'the portfolio'}.",
        "artifacts": [{"name": "valuation.md", "content": f"# {secret}\n\n..."}],
    }
    await delegations.act(prov["id"], job, "submit", actor=prov["agent"], result=result)
    await delegations.act(req["id"], job, "accept_result", actor=req["owner"])
    return job


async def _list(ws, tag: str, **disclosure) -> None:
    await directory.set_listing(
        ws["id"],
        listed=True,
        headline="Valuations",
        capabilities=[tag],
        availability="open",
        actor=ws["owner"],
        disclosure=disclosure or None,
    )


async def test_a_private_job_cannot_be_traced_from_the_directory(
    client, admin_headers, federation_on
):
    """A-02 / F-026."""
    prov = await _ws(client, admin_headers, "pp")
    req = await _ws(client, admin_headers, "pr")
    outsider = await _ws(client, admin_headers, "po")
    await _connect(req, prov)
    secret = f"PROJECT-NIGHTJAR-{uuid.uuid4().hex[:8]}"
    await messaging.send(
        req["id"],
        peer_workspace=prov["fid"],
        text_body=f"Keep {secret} between us.",
        actor=req["owner"],
    )
    jobs = [await _job(req, prov, secret=secret) for _ in range(3)]
    await reputation.issue_due()
    await integrity.review_pair(
        req["fid"], prov["fid"], verdict="genuine", note="Known client.", operator="ops"
    )
    tag = f"t{uuid.uuid4().hex[:8]}"
    await _list(prov, tag)

    party_proofs = await reputation.proofs(prov["fid"], viewer_fid=req["fid"])
    hashes = set()
    for item in party_proofs:
        payload = item["proof"]["payload"]
        hashes |= {payload["acceptance_hash"], payload["result_sha256"], item["proof"]["signature"]}
    assert len(party_proofs) == 3 and secret not in json.dumps(party_proofs)

    # Everything an outsider can reach: search, entry, reputation, proofs.
    seen = {
        "search": await directory.search(capability=tag),
        "entry": await directory.entry(prov["fid"], viewer_fid=outsider["fid"]),
        "reputation": await routes.public_reputation(prov["fid"], outsider["fid"]),
        "proofs": await reputation.proofs(prov["fid"], viewer_fid=outsider["fid"]),
    }
    blob = json.dumps(seen)
    assert secret not in blob
    assert not any(job in blob for job in jobs)
    assert req["fid"] not in blob
    # Not even the hashes or signatures that would let someone match a guess.
    assert not any(h and h in blob for h in hashes)
    assert seen["proofs"] == []
    # What is shown is the permitted summary.
    entry = seen["entry"]
    assert entry["reputation"]["acceptance"]["samples"] == 3
    assert entry["reputation"]["cooperation"]["counterparts"] == 1
    # The requester itself sees its own proofs, still without the secret.
    assert secret not in json.dumps(await directory.entry(prov["fid"], viewer_fid=req["fid"]))


async def test_disclosure_choices_and_the_public_complaint_record(
    client, admin_headers, federation_on
):
    """A-03."""
    prov = await _ws(client, admin_headers, "dp")
    req = await _ws(client, admin_headers, "dr")
    outsider = await _ws(client, admin_headers, "do")
    reporters = [await _ws(client, admin_headers, f"dc{i}") for i in range(3)]
    await _connect(req, prov)
    for r in reporters:
        await _connect(r, prov)
    for _ in range(3):
        await _job(req, prov)
    await reputation.issue_due()
    await integrity.review_pair(
        req["fid"], prov["fid"], verdict="genuine", note="Known client.", operator="ops"
    )
    tag = f"t{uuid.uuid4().hex[:8]}"
    assert (
        await _code(
            directory.set_listing(
                prov["id"],
                listed=True,
                headline="x",
                capabilities=[tag],
                availability="open",
                actor=prov["owner"],
                disclosure={"chats": True},
            )
        )
        == "FED_E_PACKAGE_INVALID"
    )
    await _list(prov, tag, acceptance=False, cooperation=False)

    # The owner chose to hide two measures: others see a marker, the owner
    # sees everything, and what is hidden cannot help the ranking.
    page = await routes.public_reputation(prov["fid"], outsider["fid"])
    assert page["acceptance"] == {"hidden_by_owner": True}
    assert page["cooperation"] == {"hidden_by_owner": True}
    assert page["completion"]["samples"] == 3
    own = await routes.public_reputation(prov["fid"], prov["fid"])
    assert own["acceptance"]["samples"] == 3
    [card] = (await directory.search(capability=tag))["items"]
    evidence = next(e for e in card["rank"]["explanation"] if e["factor"] == "evidence")
    assert evidence["value"] == directory.PRIOR and "hidden by the owner" in evidence["why"]
    assert card["reputation"]["acceptance"] == {"hidden_by_owner": True}
    assert (await directory.entry(prov["fid"], viewer_fid=prov["fid"]))["reputation"]["acceptance"][
        "samples"
    ] == 3

    # Complaints: one upheld, one dismissed, one still open.
    statements = [
        "They forwarded our brief to a third party without asking.",
        "Late delivery, we think on purpose.",
        "Rude replies in chat.",
    ]
    reports = [
        await governance.file_report(
            r["id"],
            accused=prov["fid"],
            category=cat,
            statement=text_,
            actor=r["owner"],
        )
        for r, cat, text_ in zip(
            reporters, ("data_leak", "fraud", "abuse"), statements, strict=True
        )
    ]
    upheld, dismissed, _open = (r["report_id"] for r in reports)
    for step in ("TRIAGED", "INVESTIGATING", "ACTION_PROPOSED"):
        await governance.move_case(upheld, step, operator="Ana")
    await governance.move_case(
        upheld, "DECIDED", operator="Ana", decision="Shared the brief with an outside party."
    )
    await governance.move_case(dismissed, "DISMISSED", operator="Ana", note="No evidence.")

    record = (await routes.public_reputation(prov["fid"], outsider["fid"]))["governance"][
        "complaints"
    ]
    assert (record["resolved"], record["upheld"], record["dismissed"]) == (2, 1, 1)
    assert record["upheld_by_category"] == {"data_leak": 1}
    assert record["upheld_last_year"] == 1
    assert sorted((c["category"], c["outcome"]) for c in record["recent"]) == [
        ("data_leak", "upheld"),
        ("fraud", "dismissed"),
    ]
    blob = json.dumps(record)
    for private in statements + ["Shared the brief", "No evidence."]:
        assert private not in blob
    assert not any(r["fid"] in blob for r in reporters)
    assert "abuse" not in blob  # the open case is not shown at all

    # An upheld complaint costs a little in ranking, and says so.
    [card] = (await directory.search(capability=tag))["items"]
    safety = next(e for e in card["rank"]["explanation"] if e["factor"] == "safety")
    assert safety["value"] == privacy.COMPLAINT_FACTOR
    assert "1 upheld complaint" in safety["why"]

    # The written policy covers what is public, parties-only and never shown.
    assert set(privacy.POLICY) >= {
        "public_when_listed",
        "parties_only",
        "never_published",
        "unlisted_workspaces",
        "ranking",
    }
    assert "open (unresolved) complaints" in privacy.POLICY["never_published"]
