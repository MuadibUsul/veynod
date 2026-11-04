"""Connections between workspaces (federation F-03a/b/c, docs/federation/ICD_I_GRANT.md).

Covers vision acceptance F-002 (no delegate scope: refused, no side effect),
F-003 (only a person approves; nobody else can approve for them), F-004 (no
transitive access) and F-005 (expiry, revocation, freeze and block fence
new and replayed requests).
"""

from __future__ import annotations

import asyncio
import base64
import os
import uuid

import asyncpg
import pytest

from app.config import get_settings
from app.services.federation import grants, identity
from app.services.federation.errors import FederationError
from app.services.federation.grants import Actor

pytestmark = pytest.mark.integration


def db_dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.fixture
def federation_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "federation_enabled", True)
    monkeypatch.setattr(settings, "federation_kek", base64.b64encode(os.urandom(32)).decode())
    return settings


async def _ws(client, admin_headers, name: str) -> dict:
    response = await client.post(
        "/v1/admin/workspaces",
        headers=admin_headers,
        json={"name": f"{name}-{uuid.uuid4().hex[:6]}"},
    )
    assert response.status_code == 201, response.text
    workspace_id = response.json()["workspace"]["id"]
    view = await identity.enable_workspace(workspace_id, actor=f"user:owner-{name}")
    return {
        "id": workspace_id,
        "fid": view["federation_id"],
        "owner": Actor("user", f"owner-{name}", is_owner=True),
        "member": Actor("user", f"member-{name}", is_owner=False),
        "agent": Actor("agent", f"bot-{name}"),
    }


async def _code(awaitable) -> str:
    with pytest.raises(FederationError) as err:
        await awaitable
    return err.value.code


async def _connected(a: dict, b: dict, *, a_to_b=("chat",), b_to_a=("chat",)) -> str:
    asked = await grants.request(
        a["id"],
        peer_workspace=b["fid"],
        outbound_scopes=list(a_to_b),
        inbound_scopes=list(b_to_a),
        reason="pilot collaboration",
        actor=a["owner"],
    )
    await grants.approve(b["id"], asked["connection_id"], actor=b["owner"])
    return asked["connection_id"]


async def _state(ws: dict, connection_id: str) -> str:
    rows = {r["connection_id"]: r for r in await grants.list_connections(ws["id"])}
    return rows[connection_id]["state"]


async def test_two_owners_must_agree_and_scopes_are_directional(
    client, admin_headers, federation_on
):
    a, b = await _ws(client, admin_headers, "a"), await _ws(client, admin_headers, "b")
    asked = await grants.request(
        a["id"],
        peer_workspace=b["fid"],
        outbound_scopes=["chat", "delegate"],
        inbound_scopes=["chat"],
        reason="joint research",
        actor=a["owner"],
    )
    cid = asked["connection_id"]
    assert asked["state"] == "REQUESTED" and asked["initiator"] == "local"
    # B sees the request from its own side: what A wants to do to B is inbound.
    b_side = {r["connection_id"]: r for r in await grants.list_connections(b["id"])}[cid]
    assert b_side["initiator"] == "peer" and b_side["state"] == "REQUESTED"
    assert b_side["inbound_scopes"] == ["chat", "delegate"]
    assert b_side["outbound_scopes"] == ["chat"]
    assert b_side["peer_workspace"] == a["fid"]

    # F-003: an agent or a non-owner cannot approve; the owner may narrow.
    assert await _code(grants.approve(b["id"], cid, actor=b["agent"])) == "FED_E_APPROVAL_NOT_HUMAN"
    assert (
        await _code(grants.approve(b["id"], cid, actor=b["member"])) == "FED_E_APPROVAL_NOT_HUMAN"
    )
    assert (
        await _code(
            grants.approve(b["id"], cid, actor=b["owner"], inbound_scopes=["chat", "presence"])
        )
        == "FED_E_SCOPE_DENIED"
    )
    approved = await grants.approve(b["id"], cid, actor=b["owner"], inbound_scopes=["chat"])
    assert approved["state"] == "ACTIVE" and approved["inbound_scopes"] == ["chat"]
    a_side = {r["connection_id"]: r for r in await grants.list_connections(a["id"])}[cid]
    assert a_side["state"] == "ACTIVE"
    assert a_side["outbound_scopes"] == ["chat"]  # narrowed by B
    assert a_side["inbound_scopes"] == ["chat"]

    # F-002: chat works both ways; delegate is refused before any side effect.
    await grants.authorize(a["id"], b["fid"], "chat", direction="out")
    await grants.authorize(b["id"], a["fid"], "chat", direction="in")
    assert await _code(grants.authorize(a["id"], b["fid"], "delegate", direction="out")) == (
        "FED_E_SCOPE_DENIED"
    )
    assert await _code(grants.authorize(b["id"], a["fid"], "delegate", direction="in")) == (
        "FED_E_SCOPE_DENIED"
    )

    # Each side's history: request then approve, each approval names a person.
    history_a = await grants.history(a["id"], cid)
    history_b = await grants.history(b["id"], cid)
    assert [(h["action"], h["approved_by"]) for h in history_a] == [
        ("request", "user:owner-a"),
        ("approve", "user:owner-a"),
    ]
    assert [(h["action"], h["actor"], h["approved_by"]) for h in history_b] == [
        ("request", f"peer:{a['fid']}", None),
        ("approve", "user:owner-b", "user:owner-b"),
    ]
    # One live connection per pair.
    assert (
        await _code(
            grants.request(
                a["id"],
                peer_workspace=b["fid"],
                outbound_scopes=["chat"],
                inbound_scopes=[],
                reason="second try",
                actor=a["owner"],
            )
        )
        == "FED_E_GRANT_TRANSITION"
    )
    # Only an owner can ask for a connection in P1.
    c = await _ws(client, admin_headers, "c")
    assert (
        await _code(
            grants.request(
                a["id"],
                peer_workspace=c["fid"],
                outbound_scopes=["chat"],
                inbound_scopes=[],
                reason="agent asks",
                actor=a["agent"],
            )
        )
        == "FED_E_APPROVAL_NOT_HUMAN"
    )


async def test_nobody_else_can_approve_or_revoke_for_a_peer(client, admin_headers, federation_on):
    """F-003: a third workspace's signed decision does not touch A↔B."""
    a, b, c = [await _ws(client, admin_headers, n) for n in ("a", "b", "c")]
    asked = await grants.request(
        a["id"],
        peer_workspace=b["fid"],
        outbound_scopes=["chat"],
        inbound_scopes=["chat"],
        reason="joint research",
        actor=a["owner"],
    )
    forged = await identity.sign_as(
        c["fid"],
        type="grant.decision",
        audience=a["fid"],
        payload={
            "connection_id": asked["connection_id"],
            "decision": "approved",
            "requester_scopes": ["chat"],
            "recipient_scopes": ["chat"],
            "encryption_policy": "server_readable",
        },
    )
    assert await _code(grants.receive(a["id"], forged)) == "FED_E_GRANT_UNKNOWN"
    # C's key cannot speak for B either.
    forged["issuer"] = b["fid"]
    assert await _code(grants.receive(a["id"], forged)) in {
        "FED_E_IDENTITY_UNKNOWN",
        "FED_E_BAD_SIGNATURE",
    }
    assert await _state(a, asked["connection_id"]) == "REQUESTED"


async def test_access_is_not_transitive(client, admin_headers, federation_on):
    """F-004: A↔B and B↔C do not let A reach C."""
    a, b, c = [await _ws(client, admin_headers, n) for n in ("a", "b", "c")]
    await _connected(a, b)
    await _connected(b, c)
    await grants.authorize(b["id"], c["fid"], "chat", direction="out")
    assert await _code(grants.authorize(a["id"], c["fid"], "chat", direction="out")) == (
        "FED_E_GRANT_UNKNOWN"
    )
    assert await _code(grants.authorize(c["id"], a["fid"], "chat", direction="in")) == (
        "FED_E_GRANT_UNKNOWN"
    )


async def test_revocation_freeze_block_and_expiry_fence_requests(
    client, admin_headers, federation_on
):
    """F-005: after expiry, revocation, freeze or block, new and replayed requests fail."""
    a, b = await _ws(client, admin_headers, "a"), await _ws(client, admin_headers, "b")
    cid = await _connected(a, b)
    before = await identity.sign_as(
        a["fid"], type="chat.message", audience=b["fid"], payload={"text": "hi"}
    )
    signed = grants.signed_at(before)
    await grants.authorize(b["id"], a["fid"], "chat", direction="in", signed_at=signed)

    # Freeze (local to B): everything stops; a member may freeze, only the owner unfreezes.
    await grants.freeze(b["id"], cid, actor=b["member"])
    assert await _code(grants.authorize(b["id"], a["fid"], "chat", direction="in")) == (
        "FED_E_GRANT_INACTIVE"
    )
    assert await _code(grants.unfreeze(b["id"], cid, actor=b["member"])) == (
        "FED_E_APPROVAL_NOT_HUMAN"
    )
    await grants.unfreeze(b["id"], cid, actor=b["owner"])
    # Signed before the freeze: stale even though the signature is fine.
    assert (
        await _code(grants.authorize(b["id"], a["fid"], "chat", direction="in", signed_at=signed))
        == "FED_E_GRANT_STALE"
    )
    await asyncio.sleep(1.1)  # envelope times have one-second resolution
    after = await identity.sign_as(
        a["fid"], type="chat.message", audience=b["fid"], payload={"text": "again"}
    )
    await grants.authorize(
        b["id"], a["fid"], "chat", direction="in", signed_at=grants.signed_at(after)
    )

    # Revocation by A reaches B; both sides refuse.
    await grants.revoke(a["id"], cid, actor=a["member"])
    assert await _state(a, cid) == "REVOKED" and await _state(b, cid) == "REVOKED"
    for ws, peer, direction in ((a, b, "out"), (b, a, "in")):
        assert await _code(
            grants.authorize(ws["id"], peer["fid"], "chat", direction=direction)
        ) == ("FED_E_GRANT_INACTIVE")
    assert await _code(grants.revoke(a["id"], cid, actor=a["owner"])) == "FED_E_GRANT_TRANSITION"

    # Block on B: A's next request is refused and leaves nothing behind on A.
    cid2 = await _connected(a, b)
    await grants.block(b["id"], cid2, actor=b["member"])
    assert await _state(a, cid2) == "ACTIVE"  # A is not told (no leak)
    assert await _code(grants.authorize(b["id"], a["fid"], "chat", direction="in")) == (
        "FED_E_GRANT_INACTIVE"
    )
    await grants.revoke(a["id"], cid2, actor=a["owner"])
    assert (
        await _code(
            grants.request(
                a["id"],
                peer_workspace=b["fid"],
                outbound_scopes=["chat"],
                inbound_scopes=["chat"],
                reason="try again",
                actor=a["owner"],
            )
        )
        == "FED_E_GRANT_INACTIVE"
    )
    assert {r["connection_id"] for r in await grants.list_connections(a["id"])} == {cid, cid2}

    # Expiry is checked at request time, and recorded.
    c = await _ws(client, admin_headers, "c")
    cid3 = await _connected(a, c)
    conn = await asyncpg.connect(db_dsn())
    try:
        await conn.execute(
            "UPDATE federation_grants SET expires_at = NOW() - interval '1 second' "
            "WHERE connection_id = $1",
            cid3,
        )
    finally:
        await conn.close()
    assert await _code(grants.authorize(a["id"], c["fid"], "chat", direction="out")) == (
        "FED_E_GRANT_EXPIRED"
    )
    assert [h["action"] for h in await grants.history(a["id"], cid3)][-1] == "expire"


async def test_narrowing_scopes_is_a_fence(client, admin_headers, federation_on):
    a, b = await _ws(client, admin_headers, "a"), await _ws(client, admin_headers, "b")
    cid = await _connected(a, b, a_to_b=("chat", "presence"))
    before = await identity.sign_as(
        a["fid"], type="chat.message", audience=b["fid"], payload={"text": "hi"}
    )
    narrowed = await grants.change_scopes(
        b["id"], cid, actor=b["member"], outbound_scopes=["chat"], inbound_scopes=["chat"]
    )
    assert narrowed["inbound_scopes"] == ["chat"] and narrowed["revocation_epoch"] == 1
    assert await _code(grants.authorize(b["id"], a["fid"], "presence", direction="in")) == (
        "FED_E_SCOPE_DENIED"
    )
    assert (
        await _code(
            grants.authorize(
                b["id"], a["fid"], "chat", direction="in", signed_at=grants.signed_at(before)
            )
        )
        == "FED_E_GRANT_STALE"
    )
    # Widening is not a narrowing (P1: ask for a new connection instead).
    assert (
        await _code(
            grants.change_scopes(
                b["id"],
                cid,
                actor=b["owner"],
                outbound_scopes=["chat", "delegate"],
                inbound_scopes=["chat"],
            )
        )
        == "FED_E_SCOPE_DENIED"
    )
