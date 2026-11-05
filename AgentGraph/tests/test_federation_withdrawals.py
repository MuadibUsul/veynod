"""Skill withdrawal and notices (federation P3-4, ICD_P3_REGISTRY.md §11).

A-02 (F-023): after a withdrawal, new installs are blocked and everyone
affected is told. A-03: the withdrawn version's digest stays on record.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from app.services.federation import registry, skill_updates, withdrawals
from app.services.federation.errors import FederationError
from tests import test_federation_messaging as _base
from tests.test_federation_messaging import _ws
from tests.test_federation_p1_gate import db_dsn

pytestmark = pytest.mark.integration

federation_on = _base.federation_on
transport = _base.transport


async def _code(awaitable) -> str:
    with pytest.raises(FederationError) as err:
        await awaitable
    return err.value.code


def _doc(name: str, version: str, deps: list[dict] | None = None) -> dict:
    manifest = {"name": name, "version": version, "description": "Summarise.", "entry": "SKILL.md"}
    if deps:
        manifest["dependencies"] = deps
    return {
        "manifest": manifest,
        "files": [{"path": "SKILL.md", "content": f"# {name} {version}\n\nList five points."}],
    }


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _events(workspace_id: str) -> list[dict]:
    conn = await asyncpg.connect(db_dsn())
    try:
        rows = await conn.fetch(
            "SELECT type, refs FROM workspace_events WHERE workspace_id = $1::uuid "
            "AND type = 'timeline.skill_withdrawn' ORDER BY seq",
            workspace_id,
        )
    finally:
        await conn.close()
    return [dict(r) for r in rows]


async def test_a_withdrawal_blocks_new_installs_and_tells_everyone_affected(
    client, admin_headers, federation_on, transport
):
    """A-02 / F-023."""
    pub = await _ws(client, admin_headers, "wp")
    other_pub = await _ws(client, admin_headers, "wq")
    direct, indirect, late = (
        await _ws(client, admin_headers, "wd"),
        await _ws(client, admin_headers, "wi"),
        await _ws(client, admin_headers, "wl"),
    )
    lib, wrapper = _name("lib"), _name("wrapper")
    await registry.publish(pub["id"], **_doc(lib, "0.9.0"), actor=pub["owner"])
    bad = await registry.publish(pub["id"], **_doc(lib, "1.0.0"), actor=pub["owner"])
    await registry.publish(
        other_pub["id"],
        **_doc(wrapper, "1.0.0", [{"name": lib, "version": "1.0.0"}]),
        actor=other_pub["owner"],
    )
    await registry.install(direct["id"], lib, "0.9.0", actor=direct["owner"])
    await skill_updates.update(direct["id"], lib, version="1.0.0", actor=direct["owner"])
    await registry.install(indirect["id"], wrapper, "1.0.0", actor=indirect["owner"])
    await registry.publish(pub["id"], **_doc(lib, "1.1.0"), actor=pub["owner"])

    await registry.yank(
        lib, "1.0.0", reason="Gives wrong totals.", actor=pub["owner"], workspace_id=pub["id"]
    )
    # Nothing new can take it on.
    assert await _code(registry.install(late["id"], lib, "1.0.0", actor=late["owner"])) == (
        "FED_E_YANKED"
    )
    assert (
        await _code(
            registry.publish(
                other_pub["id"],
                **_doc(_name("late-wrapper"), "1.0.0", [{"name": lib, "version": "1.0.0"}]),
                actor=other_pub["owner"],
            )
        )
        == "FED_E_YANKED"
    )

    # Everyone affected is told, with what they can do about it.
    [mine] = await withdrawals.notices(direct["id"])
    assert (mine["kind"], mine["source"], mine["name"], mine["version"]) == (
        "withdrawn",
        "publisher",
        lib,
        "1.0.0",
    )
    assert mine["digest"] == bad["digest"] and mine["reason"] == "Gives wrong totals."
    assert mine["affected"] == {
        "pinned": True,
        "as_dependency_of": None,
        "update_to": "1.1.0",
        "roll_back_to": "0.9.0",
    }
    [theirs] = await withdrawals.notices(indirect["id"])
    assert theirs["affected"]["as_dependency_of"] == wrapper
    assert theirs["affected"]["roll_back_to"] is None
    [dependant] = await withdrawals.notices(other_pub["id"])
    assert dependant["kind"] == "dependency_withdrawn"
    assert dependant["affected"]["your_skills"] == [{"name": wrapper, "version": "1.0.0"}]
    assert await withdrawals.notices(late["id"]) == []
    assert await withdrawals.notices(pub["id"]) == []
    # Agents and the console hear of it as a workspace event.
    assert len(await _events(direct["id"])) == 1
    assert len(await _events(indirect["id"])) == 1
    assert await _events(late["id"]) == []

    # Telling twice does not repeat a notice.
    assert await withdrawals.notify(bad["digest"]) == 0
    assert len(await withdrawals.notices(direct["id"])) == 1

    # The pin stays and keeps working, with a plain warning for the agent.
    used = await registry.use(direct["id"], lib)
    assert used["version"] == "1.0.0"
    assert used["warning"]["withdrawn"] is True and used["warning"]["reason"] == (
        "Gives wrong totals."
    )

    # A person acknowledges; the notice stays on the list.
    assert (
        await _code(withdrawals.acknowledge(direct["id"], mine["id"], actor=direct["agent"]))
        == "FED_E_APPROVAL_NOT_HUMAN"
    )
    assert (
        await _code(withdrawals.acknowledge(late["id"], mine["id"], actor=late["owner"]))
        == "FED_E_NOTICE_UNKNOWN"
    )
    seen = await withdrawals.acknowledge(direct["id"], mine["id"], actor=direct["owner"])
    assert seen["acknowledged_at"] is not None
    assert await withdrawals.notices(direct["id"], open_only=True) == []
    assert len(await withdrawals.notices(direct["id"])) == 1

    # Acting on it: roll back to what was pinned before.
    await skill_updates.rollback(direct["id"], lib, actor=direct["owner"])
    assert (await registry.use(direct["id"], lib))["version"] == "0.9.0"


async def test_the_platform_revokes_and_digests_stay_on_record(
    client, admin_headers, federation_on, transport
):
    """A-03, and a withdrawal by the platform rather than the publisher."""
    pub, user = await _ws(client, admin_headers, "rp"), await _ws(client, admin_headers, "ru")
    name = _name("scraper")
    first = await registry.publish(pub["id"], **_doc(name, "1.0.0"), actor=pub["owner"])
    await registry.install(user["id"], name, "1.0.0", actor=user["owner"])

    assert (
        await _code(withdrawals.revoke(name, "1.0.0", reason="no", operator="ops-anna"))
        == "FED_E_PACKAGE_INVALID"
    )
    revoked = await withdrawals.revoke(
        name, "1.0.0", reason="Sends inputs to a third party.", operator="ops-anna"
    )
    assert revoked["state"] == "yanked" and revoked["yank_source"] == "platform"
    assert await _code(registry.install(user["id"], name, "1.0.0", actor=user["owner"])) == (
        "FED_E_YANKED"
    )
    [notice] = await withdrawals.notices(user["id"])
    assert notice["source"] == "platform" and notice["reason"] == "Sends inputs to a third party."
    # The publisher is told that the platform withdrew its version.
    [told] = await withdrawals.notices(pub["id"])
    assert told["source"] == "platform" and told["affected"] == {"publisher": True}

    # A-03: the digest still says what it was, who withdrew it and why.
    record = await withdrawals.lookup(first["digest"])
    assert (record["name"], record["version"], record["digest"], record["state"]) == (
        name,
        "1.0.0",
        first["digest"],
        "yanked",
    )
    assert record["yank_source"] == "platform" and record["yanked_at"]
    assert record["yank_reason"] == "Sends inputs to a third party."
    assert [(v["version"], v["digest"]) for v in (await registry.package(name))["versions"]] == [
        ("1.0.0", first["digest"])
    ]
    # The database keeps it: a withdrawn version is never deleted.
    conn = await asyncpg.connect(db_dsn())
    try:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute("DELETE FROM registry_versions WHERE digest = $1", first["digest"])
    finally:
        await conn.close()

    # A publisher yank first, then a platform revoke: both are told, once each.
    second = _name("second")
    two = await registry.publish(pub["id"], **_doc(second, "1.0.0"), actor=pub["owner"])
    await registry.install(user["id"], second, "1.0.0", actor=user["owner"])
    await registry.yank(
        second, "1.0.0", reason="Superseded.", actor=pub["owner"], workspace_id=pub["id"]
    )
    await withdrawals.revoke(second, "1.0.0", reason="Found to be malicious.", operator="ops-ben")
    await withdrawals.revoke(second, "1.0.0", reason="Found to be malicious.", operator="ops-ben")
    sources = sorted(
        n["source"] for n in await withdrawals.notices(user["id"]) if n["digest"] == two["digest"]
    )
    assert sources == ["platform", "publisher"]
    assert await _code(withdrawals.lookup("sha256:" + "0" * 64)) == "FED_E_PACKAGE_UNKNOWN"
