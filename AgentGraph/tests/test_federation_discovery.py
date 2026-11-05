"""Skill registry: search and discovery (federation P3-2, ICD_P3_REGISTRY.md §7).

A-02: skills can be filtered by capability, reputation, price and response
speed. A-03: an entry carries its address, history, ratings and the
publisher's representative works.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.federation import delegations, discovery, registry
from app.services.federation.errors import FederationError
from tests import test_federation_messaging as _base
from tests.test_federation_delegations import Tasks, _terms
from tests.test_federation_messaging import _connect, _ws
from tests.test_federation_p1_gate import db_dsn

pytestmark = pytest.mark.integration

federation_on = _base.federation_on
transport = _base.transport


async def _code(awaitable) -> str:
    with pytest.raises(FederationError) as err:
        await awaitable
    return err.value.code


def _skill(name: str, version: str, *, capabilities, category="research", **manifest) -> dict:
    return {
        "manifest": {
            "name": name,
            "version": version,
            "description": f"{name}: summarise public reports.",
            "category": category,
            "capabilities": capabilities,
            "entry": "SKILL.md",
            **manifest,
        },
        "files": [{"path": "SKILL.md", "content": f"# {name} {version}"}],
    }


async def _answer_offers(requester, provider, count: int, *, slow_by_hours: int = 0) -> None:
    """The provider answers ``count`` delegation offers from the requester."""
    import asyncpg

    jobs = []
    for _ in range(count):
        sent = await delegations.offer(
            requester["id"],
            peer_workspace=provider["fid"],
            terms=_terms(),
            actor=requester["owner"],
        )
        job = sent["delegation"]["federation_job_id"]
        await delegations.act(
            provider["id"], job, "accept", actor=provider["owner"], create_local_task=Tasks().create
        )
        jobs.append(job)
    if slow_by_hours:
        conn = await asyncpg.connect(db_dsn())
        try:
            # The offer arrived hours before the answer.
            await conn.execute(
                "UPDATE federation_delegations SET created_at = created_at - "
                "make_interval(hours => $1) WHERE workspace_id = $2::uuid AND "
                "federation_job_id = ANY($3::text[])",
                slow_by_hours,
                provider["id"],
                jobs,
            )
        finally:
            await conn.close()


async def _market(client, admin_headers):
    """Two publishers with the same capability: one fast and well rated, one
    slow with too few ratings to show an average."""
    tag = f"t{uuid.uuid4().hex[:8]}"
    fast = await _ws(client, admin_headers, "fast")
    slow = await _ws(client, admin_headers, "slow")
    users = [await _ws(client, admin_headers, f"u{i}") for i in range(4)]
    quick, late = f"quick-{tag}", f"late-{tag}"
    await registry.publish(
        fast["id"], **_skill(quick, "1.0.0", capabilities=[tag, "summarise"]), actor=fast["owner"]
    )
    await registry.publish(
        slow["id"], **_skill(late, "1.0.0", capabilities=[tag]), actor=slow["owner"]
    )
    for user in users:
        await registry.install(user["id"], quick, "1.0.0", actor=user["owner"])
        await registry.install(user["id"], late, "1.0.0", actor=user["owner"])
    # Installing again is not another download.
    await registry.install(users[0]["id"], quick, "1.0.0", actor=users[0]["owner"])
    for user, stars in zip(users, (5, 5, 4), strict=False):
        await discovery.rate(
            user["id"], quick, stars=stars, review="Clear and short.", actor=user["owner"]
        )
    for user, stars in zip(users, (2, 3), strict=False):
        await discovery.rate(user["id"], late, stars=stars, review=None, actor=user["owner"])
    for provider in (fast, slow):
        await _connect(users[0], provider, a_to_b=("chat", "delegate"))
    await _answer_offers(users[0], fast, 3)
    await _answer_offers(users[0], slow, 3, slow_by_hours=2)
    return {"tag": tag, "fast": fast, "slow": slow, "users": users, "quick": quick, "late": late}


def _names(found) -> list[str]:
    return [item["name"] for item in found["items"]]


async def test_filter_by_capability_reputation_price_and_response_speed(
    client, admin_headers, federation_on, transport
):
    """A-02."""
    m = await _market(client, admin_headers)
    tag, quick, late = m["tag"], m["quick"], m["late"]

    found = await discovery.search(capability=tag, sort="downloads")
    assert sorted(_names(found)) == sorted([quick, late]) and found["total"] == 2
    by_name = {item["name"]: item for item in found["items"]}
    assert by_name[quick]["downloads"] == 4  # four workspaces, the re-install not counted
    assert by_name[quick]["rating"] == {"average": 4.67, "count": 3, "enough_evidence": True}
    assert by_name[late]["rating"] == {"average": None, "count": 2, "enough_evidence": False}
    assert by_name[quick]["response"]["enough_evidence"] is True
    assert by_name[quick]["response"]["median_seconds"] < 600
    assert by_name[late]["response"]["median_seconds"] >= 7200
    assert by_name[quick]["price"] == {"model": "free", "amount": None}
    assert by_name[quick]["publisher"]["fid"] == m["fast"]["fid"]

    # Capability.
    assert _names(await discovery.search(capability="summarise", q=tag)) == [quick]
    # Reputation: an average needs three ratings; two do not qualify.
    assert _names(await discovery.search(capability=tag, min_rating=4)) == [quick]
    assert _names(await discovery.search(capability=tag, min_rating=1)) == [quick]
    # Price: everything is free for now; nothing is paid.
    assert sorted(_names(await discovery.search(capability=tag, price="free"))) == sorted(
        [quick, late]
    )
    assert (await discovery.search(capability=tag, price="paid"))["items"] == []
    # Response speed, measured by the platform from answered delegation offers.
    assert _names(await discovery.search(capability=tag, max_response_seconds=600)) == [quick]
    assert sorted(
        _names(await discovery.search(capability=tag, max_response_seconds=3 * 3600))
    ) == sorted([quick, late])
    # Sorting.
    assert _names(await discovery.search(capability=tag, sort="rating")) == [quick, late]
    assert _names(await discovery.search(capability=tag, sort="response")) == [quick, late]
    # Words, category and kind.
    assert _names(await discovery.search(q=late)) == [late]
    assert _names(await discovery.search(capability=tag, category="coding")) == []
    assert _names(await discovery.search(capability=tag, kind="executable")) == []
    # Paging.
    page = await discovery.search(capability=tag, sort="downloads", limit=1, offset=1)
    assert page["total"] == 2 and len(page["items"]) == 1
    assert await _code(discovery.search(category="astrology")) == "FED_E_PACKAGE_INVALID"


async def test_an_entry_has_address_history_ratings_and_works(
    client, admin_headers, federation_on, transport
):
    """A-03."""
    m = await _market(client, admin_headers)
    fast, slow, users, quick = m["fast"], m["slow"], m["users"], m["quick"]
    tag = m["tag"]
    newer = await registry.publish(
        fast["id"], **_skill(quick, "1.1.0", capabilities=[tag, "cite"]), actor=fast["owner"]
    )
    await registry.yank(
        quick,
        "1.0.0",
        reason="Mis-cites page numbers.",
        actor=fast["owner"],
        workspace_id=fast["id"],
    )
    other = f"other-{tag}"
    await registry.publish(
        fast["id"],
        **_skill(other, "2.0.0", capabilities=["translate"], category="writing"),
        actor=fast["owner"],
    )
    await registry.install(users[1]["id"], other, "2.0.0", actor=users[1]["owner"])
    wrapper = f"wrap-{tag}"
    await registry.publish(
        slow["id"],
        **_skill(
            wrapper,
            "1.0.0",
            capabilities=[tag],
            dependencies=[{"name": quick, "version": "1.1.0"}],
        ),
        actor=slow["owner"],
    )

    page = await discovery.entry(quick)
    # Address: the name, the publisher, and what an install pins.
    assert page["address"]["package"] == quick
    assert page["address"]["publisher"] == fast["fid"]
    assert page["address"]["pin"] == f"{quick}@1.1.0"
    assert page["address"]["digest"] == newer["digest"]
    assert page["capabilities"] == sorted([tag, "cite"]) and page["category"] == "research"
    # History: every version, newest first, the yanked one kept with its reason.
    assert [(h["version"], h["state"]) for h in page["history"]] == [
        ("1.1.0", "published"),
        ("1.0.0", "yanked"),
    ]
    assert page["history"][1]["yank_reason"] == "Mis-cites page numbers."
    assert page["history"][1]["downloads"] == 4
    assert page["history"][1]["rating"]["count"] == 3
    assert page["downloads"] == {"total": 4, "by_version": {"1.0.0": 4}}
    # Ratings: summary, distribution and reviews without the reviewer's address.
    assert page["rating"]["average"] == 4.67 and page["rating"]["count"] == 3
    assert page["rating"]["distribution"] == {"1": 0, "2": 0, "3": 0, "4": 1, "5": 2}
    assert page["rating"]["recent_reviews"][0]["review"] == "Clear and short."
    assert all(
        set(r) == {"version", "stars", "review", "at"} for r in page["rating"]["recent_reviews"]
    )
    assert not any(u["fid"] in str(page["rating"]) for u in users)
    # Dependencies both ways.
    assert page["dependants"] == [{"name": wrapper, "version": "1.0.0"}]
    wrapped = await discovery.entry(wrapper)
    assert wrapped["dependencies"] == [
        {"name": quick, "version": "1.1.0", "digest": newer["digest"], "state": "published"}
    ]
    # The publisher: track record and representative works.
    assert page["publisher"]["fid"] == fast["fid"] and page["publisher"]["packages"] == 2
    assert page["publisher"]["accepted_results"] == 0
    assert page["publisher"]["response"]["samples"] == 3
    assert [w["name"] for w in page["representative_works"]] == [other]
    assert page["representative_works"][0]["downloads"] == 1
    assert set(page["evidence"]) == {
        "self_declared",
        "machine_verified",
        "from_other_workspaces",
        "measured_by_platform",
    }
    # Search hides a skill with nothing installable unless asked.
    await registry.yank(
        other, "2.0.0", reason="Superseded elsewhere.", actor=fast["owner"], workspace_id=fast["id"]
    )
    assert _names(await discovery.search(q=other)) == []
    assert _names(await discovery.search(q=other, include_yanked=True)) == [other]
    assert (await discovery.entry(other))["installable"] is False


async def test_who_may_rate(client, admin_headers, federation_on, transport):
    pub = await _ws(client, admin_headers, "rp")
    user = await _ws(client, admin_headers, "ru")
    stranger = await _ws(client, admin_headers, "rs")
    name = f"rated-{uuid.uuid4().hex[:8]}"
    await registry.publish(pub["id"], **_skill(name, "1.0.0", capabilities=[]), actor=pub["owner"])
    await registry.install(user["id"], name, "1.0.0", actor=user["owner"])

    assert await _code(
        discovery.rate(user["id"], name, stars=5, review=None, actor=user["agent"])
    ) == ("FED_E_APPROVAL_NOT_HUMAN")
    assert await _code(
        discovery.rate(pub["id"], name, stars=5, review=None, actor=pub["owner"])
    ) == ("FED_E_RATING_NOT_ALLOWED")
    assert (
        await _code(
            discovery.rate(stranger["id"], name, stars=5, review=None, actor=stranger["owner"])
        )
        == "FED_E_RATING_NOT_ALLOWED"
    )
    assert (
        await _code(
            discovery.rate(
                user["id"],
                name,
                stars=4,
                review="token: Bearer abcdefghijklmnopqrstuvwxyz0123",
                actor=user["owner"],
            )
        )
        == "FED_E_CREDENTIAL_IN_PAYLOAD"
    )
    assert await _code(
        discovery.rate(user["id"], name, stars=6, review=None, actor=user["owner"])
    ) == ("FED_E_PACKAGE_INVALID")
    first = await discovery.rate(user["id"], name, stars=2, review="Too long.", actor=user["owner"])
    assert first["version"] == "1.0.0"
    await discovery.rate(
        user["id"], name, stars=4, review="Better after a reread.", actor=user["owner"]
    )
    page = await discovery.entry(name)
    # One rating per workspace: rating again replaces it.
    assert page["rating"]["count"] == 1 and page["rating"]["average"] is None
    assert page["rating"]["distribution"]["4"] == 1 and page["rating"]["distribution"]["2"] == 0
