"""Skill registry: publish and versions (federation P3-1, ICD_P3_REGISTRY.md).

A-02: a skill can be followed and verified from publication to another
workspace pulling and using it. A-03 (F-023, in part): an executable
dependency is published and then yanked; its digest stays on record.
"""

from __future__ import annotations

import asyncpg
import pytest

from app.services.federation import registry
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


def _skill(name: str, version: str, **extra) -> dict:
    manifest = {
        "name": name,
        "version": version,
        "description": "Summarise a public report into five bullet points.",
        "kind": "declarative",
        "entry": "SKILL.md",
        **extra.pop("manifest", {}),
    }
    files = extra.pop("files", None) or [
        {"path": "SKILL.md", "content": f"# {name} {version}\n\nRead the report, then ..."},
        {"path": "examples/one.md", "content": "Input: ...\nOutput: ..."},
    ]
    return {"manifest": manifest, "files": files}


def _name(prefix: str) -> str:
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def test_publish_pull_verify_and_use_with_a_pinned_digest(
    client, admin_headers, federation_on, transport
):
    """A-02."""
    a, b = await _ws(client, admin_headers, "pa"), await _ws(client, admin_headers, "pb")
    name = _name("summarise")
    published = await registry.publish(a["id"], **_skill(name, "1.0.0"), actor=a["owner"])
    assert published["digest"].startswith("sha256:")
    assert published["signature"]["type"] == "skill.release"
    assert published["signature"]["issuer"] == a["fid"]
    assert published["signature"]["payload"]["digest"] == published["digest"]

    installed = await registry.install(b["id"], name, "1.0.0", actor=b["owner"])
    assert installed["pinned"]["digest"] == published["digest"]
    assert installed["pinned"]["publisher"] == a["fid"]
    # A newer version does not move what B pinned.
    await registry.publish(a["id"], **_skill(name, "1.1.0"), actor=a["owner"])
    used = await registry.use(b["id"], name)
    assert used["version"] == "1.0.0" and used["digest"] == published["digest"]
    assert used["files"][0]["path"] == "SKILL.md" and "1.0.0" in used["files"][0]["content"]
    assert used["trust"] == "untrusted"
    [pin] = await registry.installs(b["id"])
    assert pin["digest"] == published["digest"] and pin["yanked"] is False

    # People publish and install; agents read.
    assert (
        await _code(registry.publish(a["id"], **_skill(name, "2.0.0"), actor=a["agent"]))
        == "FED_E_APPROVAL_NOT_HUMAN"
    )
    assert (
        await _code(registry.install(b["id"], name, "1.1.0", actor=b["agent"]))
        == "FED_E_APPROVAL_NOT_HUMAN"
    )


async def test_versions_are_immutable_and_names_belong_to_their_publisher(
    client, admin_headers, federation_on, transport
):
    a, b = await _ws(client, admin_headers, "ia"), await _ws(client, admin_headers, "ib")
    name = _name("immutable")
    first = await registry.publish(a["id"], **_skill(name, "1.2.0"), actor=a["owner"])
    for version in ("1.2.0", "1.1.9", "0.9.0"):
        assert (
            await _code(registry.publish(a["id"], **_skill(name, version), actor=a["owner"]))
            == "FED_E_VERSION_EXISTS"
        )
    assert (
        await _code(registry.publish(b["id"], **_skill(name, "9.0.0"), actor=b["owner"]))
        == "FED_E_NAME_TAKEN"
    )
    assert (
        await _code(registry.publish(a["id"], **_skill(name, "1.3"), actor=a["owner"]))
        == "FED_E_PACKAGE_INVALID"
    )

    conn = await asyncpg.connect(db_dsn())
    try:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "UPDATE registry_versions SET files = '[]'::jsonb WHERE digest = $1",
                first["digest"],
            )
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute("DELETE FROM registry_versions WHERE digest = $1", first["digest"])
        # Even if someone got round the database guard, install would notice.
        async with conn.transaction():
            await conn.execute(
                "ALTER TABLE registry_versions DISABLE TRIGGER trg_registry_version_guard"
            )
            await conn.execute(
                "UPDATE registry_versions SET files = jsonb_set(files, '{0,content}', "
                "'\"# tampered\"') WHERE digest = $1",
                first["digest"],
            )
            await conn.execute(
                "ALTER TABLE registry_versions ENABLE TRIGGER trg_registry_version_guard"
            )
        assert await _code(registry.install(b["id"], name, "1.2.0", actor=b["owner"])) == (
            "FED_E_DIGEST_MISMATCH"
        )
    finally:
        await conn.close()


async def test_an_executable_dependency_is_yanked_and_its_digest_stays(
    client, admin_headers, federation_on, transport
):
    """A-03 / F-023 (in part)."""
    a = await _ws(client, admin_headers, "ya")
    b = await _ws(client, admin_headers, "yb")
    c = await _ws(client, admin_headers, "yc")
    runner, wrapper = _name("run-script"), _name("uses-runner")
    tool = await registry.publish(
        a["id"],
        **_skill(
            runner,
            "1.0.0",
            manifest={"kind": "executable", "entry": "run.py", "permissions": ["exec"]},
            files=[{"path": "run.py", "content": "print('hello')\n"}],
        ),
        actor=a["owner"],
    )
    outer = await registry.publish(
        a["id"],
        **_skill(
            wrapper, "1.0.0", manifest={"dependencies": [{"name": runner, "version": "1.0.0"}]}
        ),
        actor=a["owner"],
    )
    assert outer["dependencies"] == [{"name": runner, "version": "1.0.0", "digest": tool["digest"]}]

    # Installing pulls the executable dependency too, and only with consent.
    assert await _code(registry.install(b["id"], wrapper, "1.0.0", actor=b["owner"])) == (
        "FED_E_PERMISSIONS_NOT_ACCEPTED"
    )
    await registry.install(b["id"], wrapper, "1.0.0", actor=b["owner"], accept_permissions=["exec"])
    pins = {p["name"]: p for p in await registry.installs(b["id"])}
    assert pins[runner]["digest"] == tool["digest"] and pins[runner]["as_dependency_of"] == wrapper
    assert pins[runner]["accepted_permissions"] == ["exec"]

    yanked = await registry.yank(
        runner,
        "1.0.0",
        reason="Runs arbitrary code without a sandbox.",
        actor=a["owner"],
        workspace_id=a["id"],
    )
    assert yanked["state"] == "yanked" and yanked["digest"] == tool["digest"]
    history = await registry.package(runner)
    assert [(v["version"], v["digest"], v["state"]) for v in history["versions"]] == [
        ("1.0.0", tool["digest"], "yanked")
    ]
    # No new installs, no new dependants; the existing pin is flagged, not removed.
    assert (
        await _code(
            registry.install(
                c["id"], runner, "1.0.0", actor=c["owner"], accept_permissions=["exec"]
            )
        )
        == "FED_E_YANKED"
    )
    assert (
        await _code(
            registry.publish(
                a["id"],
                **_skill(
                    _name("late"),
                    "1.0.0",
                    manifest={"dependencies": [{"name": runner, "version": "1.0.0"}]},
                ),
                actor=a["owner"],
            )
        )
        == "FED_E_YANKED"
    )
    pins = {p["name"]: p for p in await registry.installs(b["id"])}
    assert pins[runner]["yanked"] is True
    # Only the publisher yanks.
    assert (
        await _code(
            registry.yank(
                wrapper, "1.0.0", reason="not mine to yank", actor=b["owner"], workspace_id=b["id"]
            )
        )
        == "FED_E_NAME_TAKEN"
    )


async def test_a_skill_carrying_a_credential_is_refused(
    client, admin_headers, federation_on, transport
):
    a = await _ws(client, admin_headers, "ka")
    assert (
        await _code(
            registry.publish(
                a["id"],
                **_skill(
                    _name("leaky"),
                    "1.0.0",
                    files=[
                        {"path": "SKILL.md", "content": "Use Bearer abcdefghijklmnopqrstuvwxyz0123"}
                    ],
                ),
                actor=a["owner"],
            )
        )
        == "FED_E_CREDENTIAL_IN_PAYLOAD"
    )
    assert (
        await _code(
            registry.publish(
                a["id"],
                **_skill(
                    _name("escape"), "1.0.0", files=[{"path": "../etc/passwd", "content": "x"}]
                ),
                actor=a["owner"],
            )
        )
        == "FED_E_PACKAGE_INVALID"
    )
    assert (
        await _code(
            registry.publish(
                a["id"],
                **_skill(_name("sneaky"), "1.0.0", manifest={"permissions": ["network"]}),
                actor=a["owner"],
            )
        )
        == "FED_E_PACKAGE_INVALID"
    )
