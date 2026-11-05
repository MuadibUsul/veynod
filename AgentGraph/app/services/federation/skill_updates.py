"""Installed skills over time: updates, following, rollback (P3-3).

Contract: docs/federation/ICD_P3_REGISTRY.md §10 (vision §11.4, F-024).

* A pin stays where it is until someone moves it. ``updates`` shows what is
  newer, what changed (files, permissions, dependencies) and whether it can
  follow on its own.
* A workspace chooses per skill: ``locked`` (the default) or follow patch or
  minor releases. Following applies a newer version on its own only when it
  passes every install check, asks for no permission beyond what was accepted
  and keeps the same dependencies; anything else waits for a person.
* Moving a pin by hand (update, rollback) is an install of that version: the
  same checks, and an executable version needs exactly its own permissions
  accepted again. Old acceptances never carry over.
* Every move is kept in an append-only history, so any earlier version that
  is still installable can be restored.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.database import async_session_factory
from app.services.federation import registry
from app.services.federation.canonical import sha256_hex
from app.services.federation.errors import FederationError
from app.services.federation.grants import Actor

FOLLOW_ACTOR = "system:follow"


async def _pin_or_404(workspace_id: str, name: str) -> dict[str, Any]:
    pin = await registry._current_pin(workspace_id, name)
    if pin is None:
        raise FederationError("FED_E_NOT_INSTALLED", "this skill is not installed here", name=name)
    return pin


def _in_range(policy: str, pinned: tuple[int, int, int], candidate: tuple[int, int, int]) -> bool:
    if candidate <= pinned:
        return False
    if policy == "follow_patch":
        return candidate[:2] == pinned[:2]
    if policy == "follow_minor":
        return candidate[0] == pinned[0]
    return False


def _files_change(old: list[dict], new: list[dict]) -> dict[str, list[str]]:
    before = {f["path"]: sha256_hex(f["content"]) for f in old}
    after = {f["path"]: sha256_hex(f["content"]) for f in new}
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(p for p in set(before) & set(after) if before[p] != after[p]),
    }


async def _versions(name: str) -> list[dict[str, Any]]:
    async with async_session_factory() as platform:
        rows = (
            (
                await platform.execute(
                    text(
                        "SELECT * FROM registry_versions WHERE name = :n "
                        "ORDER BY major, minor, patch"
                    ),
                    {"n": name},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def _tuple(row: dict[str, Any]) -> tuple[int, int, int]:
    return (row["major"], row["minor"], row["patch"])


async def _compare(pin: dict[str, Any], current: dict[str, Any], candidate: dict[str, Any]):
    """What moving the pin from current to candidate would change."""
    old_m, new_m = registry._loads(current["manifest"]), registry._loads(candidate["manifest"])
    declared = sorted(new_m.get("permissions", []))
    accepted = list(pin["accepted_permissions"])
    perms = registry.permission_change(accepted, declared)
    old_deps = {(d["name"], d["digest"]) for d in registry._loads(current["dependencies"])}
    new_deps = {(d["name"], d["digest"]) for d in registry._loads(candidate["dependencies"])}
    report = registry.sandbox.inspect(new_m, registry._loads(candidate["files"]))
    return {
        "version": candidate["version"],
        "digest": candidate["digest"],
        "kind": candidate["kind"],
        "declared_permissions": declared,
        "permissions": perms,
        "kind_changed": old_m.get("kind") != new_m.get("kind"),
        "files": _files_change(
            registry._loads(current["files"]), registry._loads(candidate["files"])
        ),
        "dependencies_changed": old_deps != new_deps,
        "sandbox": report["verdict"],
        "sandbox_findings": [f for f in report["findings"] if f["severity"] == "block"][:5],
    }


async def updates(workspace_id: str) -> list[dict[str, Any]]:
    """For each installed skill: the newest installable version, what it
    changes, and whether following may apply it on its own."""
    out = []
    for pin in await registry.installs(workspace_id):
        versions = await _versions(pin["name"])
        current = next(v for v in versions if v["digest"] == pin["digest"])
        newer = [v for v in versions if v["state"] == "published" and _tuple(v) > _tuple(current)]
        if not newer:
            continue
        # Under a follow policy, the newest release inside it; else the newest.
        in_range = [v for v in newer if _in_range(pin["update_policy"], _tuple(current), _tuple(v))]
        candidate = in_range[-1] if in_range else newer[-1]
        raw = await registry._current_pin(workspace_id, pin["name"])
        change = await _compare(raw, current, candidate)
        follows = _in_range(pin["update_policy"], _tuple(current), _tuple(candidate))
        blockers = []
        if change["permissions"]["added"]:
            blockers.append("asks for new permissions")
        if change["kind_changed"]:
            blockers.append("changes kind")
        if change["dependencies_changed"]:
            blockers.append("changes dependencies")
        if change["sandbox"] != "passed":
            blockers.append("fails the sandbox check")
        if not follows:
            blockers.append(
                "outside the follow policy" if pin["update_policy"] != "locked" else "locked"
            )
        out.append(
            {
                "name": pin["name"],
                "pinned_version": pin["version"],
                "update_policy": pin["update_policy"],
                "available": change,
                "applies_on_its_own": not blockers,
                "needs_a_person_because": blockers,
            }
        )
    return out


async def update(
    workspace_id: str,
    name: str,
    *,
    version: str | None = None,
    accept_permissions: list[str] | None = None,
    actor: Actor,
) -> dict[str, Any]:
    """A person moves the pin forward (to the newest installable version by
    default), accepting the new version's permissions afresh."""
    pin = await _pin_or_404(workspace_id, name)
    if version is None:
        newer = [
            v
            for v in await _versions(name)
            if v["state"] == "published"
            and _tuple(v) > tuple(int(p) for p in pin["version"].split("."))
        ]
        if not newer:
            raise registry._invalid("no newer installable version", pinned=pin["version"])
        version = newer[-1]["version"]
    elif registry._version_tuple(version) <= registry._version_tuple(pin["version"]):
        raise registry._invalid("an update goes to a newer version; use rollback to go back")
    return await registry.install(
        workspace_id,
        name,
        version,
        actor=actor,
        accept_permissions=accept_permissions,
        _action="update",
    )


async def rollback(
    workspace_id: str,
    name: str,
    *,
    version: str | None = None,
    accept_permissions: list[str] | None = None,
    actor: Actor,
) -> dict[str, Any]:
    """Restore an earlier version (by default the one pinned before this one).
    It must still be installable: a yanked or failing version is not restored."""
    pin = await _pin_or_404(workspace_id, name)
    if version is None:
        for line in await history(workspace_id, name):
            if line["version"] and line["version"] != pin["version"]:
                version = line["version"]
                break
        if version is None:
            raise registry._invalid("no earlier version to restore", pinned=pin["version"])
    if version == pin["version"]:
        raise registry._invalid("that version is already pinned")
    return await registry.install(
        workspace_id,
        name,
        version,
        actor=actor,
        accept_permissions=accept_permissions,
        _action="rollback",
    )


async def set_policy(workspace_id: str, name: str, *, policy: str, actor: Actor) -> dict:
    if actor.kind != "user":
        raise FederationError("FED_E_APPROVAL_NOT_HUMAN", "a person chooses how a skill updates")
    if policy not in registry.POLICIES:
        raise registry._invalid("update_policy is locked, follow_patch or follow_minor")
    pin = await _pin_or_404(workspace_id, name)
    async with async_session_factory() as platform:
        await platform.execute(
            text(
                "UPDATE registry_installs SET update_policy = :p WHERE workspace_id = "
                "CAST(:w AS uuid) AND name = :n"
            ),
            {"p": policy, "w": str(workspace_id), "n": name},
        )
        await platform.execute(
            text(
                "INSERT INTO registry_install_history (workspace_id, tenant_id, name, action, "
                "from_version, version, digest, accepted_permissions, update_policy, actor) VALUES "
                "(CAST(:w AS uuid), '', :n, 'policy', :v, :v, :d, :p, :pol, :by)"
            ),
            {
                "w": str(workspace_id),
                "n": name,
                "v": pin["version"],
                "d": pin["digest"],
                "p": list(pin["accepted_permissions"]),
                "pol": policy,
                "by": actor.ref,
            },
        )
        await platform.commit()
    return {"name": name, "version": pin["version"], "update_policy": policy}


async def uninstall(workspace_id: str, name: str, *, actor: Actor) -> dict[str, Any]:
    if actor.kind != "user":
        raise FederationError("FED_E_APPROVAL_NOT_HUMAN", "a person uninstalls a skill")
    pin = await _pin_or_404(workspace_id, name)
    async with async_session_factory() as platform:
        await platform.execute(
            text(
                "DELETE FROM registry_installs WHERE workspace_id = CAST(:w AS uuid) AND name = :n"
            ),
            {"w": str(workspace_id), "n": name},
        )
        await platform.execute(
            text(
                "INSERT INTO registry_install_history (workspace_id, tenant_id, name, action, "
                "from_version, actor) VALUES (CAST(:w AS uuid), '', :n, 'uninstall', :v, :by)"
            ),
            {"w": str(workspace_id), "n": name, "v": pin["version"], "by": actor.ref},
        )
        await platform.commit()
    return {"name": name, "uninstalled": pin["version"]}


async def history(workspace_id: str, name: str) -> list[dict[str, Any]]:
    """Every move of this skill's pin here, newest first."""
    async with async_session_factory() as platform:
        rows = (
            (
                await platform.execute(
                    text(
                        "SELECT * FROM registry_install_history WHERE workspace_id = "
                        "CAST(:w AS uuid) AND name = :n ORDER BY id DESC"
                    ),
                    {"w": str(workspace_id), "n": name},
                )
            )
            .mappings()
            .all()
        )
    return [
        {
            "action": r["action"],
            "from_version": r["from_version"],
            "version": r["version"],
            "digest": r["digest"],
            "accepted_permissions": list(r["accepted_permissions"]),
            "update_policy": r["update_policy"],
            "actor": r["actor"],
            "at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


async def apply_followed(limit: int = 200) -> int:
    """Move followed pins forward where nothing needs a person's decision
    (run by the federation background loop). Returns how many moved."""
    async with async_session_factory() as platform:
        followed = (
            (
                await platform.execute(
                    text(
                        "SELECT workspace_id, name FROM registry_installs "
                        "WHERE update_policy <> 'locked' ORDER BY installed_at LIMIT :l"
                    ),
                    {"l": limit},
                )
            )
            .mappings()
            .all()
        )
    moved = 0
    for item in followed:
        try:
            moved += await _follow_one(str(item["workspace_id"]), item["name"])
        except FederationError:
            continue  # one skill that cannot move does not hold up the others
    return moved


async def _follow_one(workspace_id: str, name: str) -> int:
    """Move one followed pin if its pending update needs nobody's decision."""
    pending = next((u for u in await updates(workspace_id) if u["name"] == name), None)
    if pending is None or not pending["applies_on_its_own"]:
        return 0
    async with async_session_factory() as platform:
        row = await registry._version_row(platform, name, pending["available"]["version"])
    await registry.admissible(row)
    await registry._pin(
        workspace_id,
        row,
        accepted=pending["available"]["declared_permissions"]
        if row["kind"] == "executable"
        else [],
        parent=None,
        actor_ref=FOLLOW_ACTOR,
        action="follow",
        update_policy=None,
    )
    return 1
