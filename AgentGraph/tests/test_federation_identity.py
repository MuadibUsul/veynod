"""Federation core tables and hosted identities against the database
(federation F-01a/b/c, F-02a/b; doc://federation-i-id-identity-icd §8 F-001)."""

from __future__ import annotations

import base64
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from app.config import get_settings
from app.services.federation import identity
from app.services.federation.envelope import verify_envelope
from app.services.federation.errors import FederationError
from app.services.federation.ids import b64url_decode, new_connection_id, verify_derivation

pytestmark = pytest.mark.integration

PLATFORM_TABLES = (
    "federation_identities",
    "federation_identity_keys",
    "federation_identity_log",
    "federation_local_map",
    "federation_addresses",
    "federation_address_history",
)
TENANT_TABLES = (
    "federation_peers",
    "federation_grants",
    "federation_grant_history",
    "federation_threads",
    "federation_messages",
    "federation_delivery_attempts",
    "federation_replay_guard",
)


def db_dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.fixture
def federation_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "federation_enabled", True)
    monkeypatch.setattr(settings, "federation_kek", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setattr(settings, "federation_kek_id", "kek-test")
    return settings


async def _workspace(client, admin_headers, name="fed"):
    response = await client.post(
        "/v1/admin/workspaces",
        headers=admin_headers,
        json={"name": f"{name}-{uuid.uuid4().hex[:6]}"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {
        "id": body["workspace"]["id"],
        "headers": {"Authorization": f"Bearer {body['api_key']}"},
    }


async def _code(awaitable) -> str:
    with pytest.raises(FederationError) as err:
        await awaitable
    return err.value.code


def _assert_no_local_ids(value, *local_ids: str) -> None:
    """F-001 R5: no local identifiers or local-id field names leave the platform."""
    blob = json.dumps(value)
    assert not re.search(r"\b(prn|tok|ten|agk)_[A-Za-z0-9]", blob), blob
    for name in ("agent_id", "principal_id", "tenant_id", "workspace_id"):
        assert f'"{name}"' not in blob
    for local in local_ids:
        assert local not in blob


async def test_schema_shape(client):
    conn = await asyncpg.connect(db_dsn())
    try:
        for table in TENANT_TABLES:
            row = await conn.fetchrow(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = $1", table
            )
            assert row is not None and row["relrowsecurity"] and row["relforcerowsecurity"], table
        for table in PLATFORM_TABLES:
            assert not await conn.fetchval(
                "SELECT has_table_privilege('agentgraph_app', $1, 'SELECT')", table
            ), table
        assert not await conn.fetchval(
            "SELECT has_table_privilege('agentgraph_app', 'federation_grant_history', 'UPDATE')"
        )
    finally:
        await conn.close()


async def test_disabled_by_default(client, admin_headers):
    assert get_settings().federation_enabled is False
    workspace = await _workspace(client, admin_headers)
    assert (
        await _code(identity.enable_workspace(workspace["id"], actor="user:test"))
        == "FED_E_DISABLED"
    )
    assert await identity.local_identity(workspace["id"]) is None


async def test_same_named_agents_never_collide(client, admin_headers, federation_on):
    """F-001: two root tenants each have a local agent called `researcher`."""
    a = await _workspace(client, admin_headers, "fed-a")
    b = await _workspace(client, admin_headers, "fed-b")
    ws_a = await identity.enable_workspace(a["id"], actor="user:owner-a")
    ws_b = await identity.enable_workspace(b["id"], actor="user:owner-b")
    assert await identity.enable_workspace(a["id"], actor="user:owner-a") == ws_a  # idempotent
    assert ws_a["federation_id"] != ws_b["federation_id"]
    assert ws_a["parent_id"] != ws_b["parent_id"]  # one subject per root tenant
    for view in (ws_a, ws_b):
        assert verify_derivation(view["federation_id"], b64url_decode(view["genesis_public_key"]))

    agent_a = await identity.register_agent(a["id"], "prn_researcher_a", actor="user:owner-a")
    agent_b = await identity.register_agent(b["id"], "prn_researcher_b", actor="user:owner-b")
    assert agent_a["federation_id"] != agent_b["federation_id"]
    assert agent_a["parent_id"] == ws_a["federation_id"]

    # handles are confirmed by a person; the same handle in two workspaces is fine
    run = uuid.uuid4().hex[:8]  # handles are never reused, so each run picks fresh ones
    acme, globex, researcher = f"acme-{run}", f"globex-{run}", f"researcher-{run}"
    await identity.set_alias(ws_a["parent_id"], acme, actor="user:owner-a")
    await identity.set_alias(ws_b["parent_id"], globex, actor="user:owner-b")
    await identity.set_alias(ws_a["federation_id"], "research", actor="user:owner-a")
    await identity.set_alias(ws_b["federation_id"], "research", actor="user:owner-b")
    await identity.set_alias(agent_a["federation_id"], researcher.upper(), actor="user:owner-a")
    await identity.set_alias(agent_b["federation_id"], researcher, actor="user:owner-b")

    partial = await identity.resolve_alias(agent=researcher)
    assert partial["status"] == "ambiguous" and partial["code"] == "FED_E_ALIAS_AMBIGUOUS"
    assert {m["federation_id"] for m in partial["matches"]} == {
        agent_a["federation_id"],
        agent_b["federation_id"],
    }
    full = await identity.resolve_alias(subject=acme, workspace="research", agent=researcher)
    assert full["status"] == "unique"
    assert full["matches"][0]["federation_id"] == agent_a["federation_id"]
    assert (await identity.resolve_alias(subject=globex, agent=researcher))["matches"][0][
        "federation_id"
    ] == agent_b["federation_id"]

    # R5: nothing local leaves the platform
    for value in (ws_a, agent_a, partial, full, await identity.resolve(agent_a["federation_id"])):
        _assert_no_local_ids(value, a["id"], b["id"])
    ref = await identity.map_local(agent_a["federation_id"])
    assert ref.workspace_id == a["id"] and ref.principal_id == "prn_researcher_a"


async def test_aliases_redirect_and_are_never_reused(client, admin_headers, federation_on):
    workspace = await _workspace(client, admin_headers)
    ws = await identity.enable_workspace(workspace["id"], actor="user:owner")
    subject = ws["parent_id"]
    handle = f"co{uuid.uuid4().hex[:8]}"
    await identity.set_alias(subject, handle, actor="user:owner")
    await identity.set_alias(subject, handle + "-new", actor="user:owner")
    old = await identity.resolve_alias(subject=handle)
    assert old["status"] == "redirect" and old["redirect_to"] == handle + "-new"
    assert old["matches"][0]["federation_id"] == subject
    other = await identity.enable_workspace(
        (await _workspace(client, admin_headers))["id"], actor="user:x"
    )
    assert (
        await _code(identity.set_alias(other["parent_id"], handle, actor="user:x"))
        == "FED_E_ALIAS_TAKEN"
    )
    with pytest.raises(FederationError):
        await identity.set_alias(subject, "bad handle", actor="user:owner")


async def test_rotation_revocation_and_freeze(client, admin_headers, federation_on):
    alice_ws = await _workspace(client, admin_headers, "alice")
    bob_ws = await _workspace(client, admin_headers, "bob")
    alice = await identity.enable_workspace(alice_ws["id"], actor="user:alice")
    bob = await identity.enable_workspace(bob_ws["id"], actor="user:bob")
    fid = alice["federation_id"]

    async def verify(envelope):
        return await verify_envelope(
            envelope,
            local_workspace_fid=bob["federation_id"],
            key_lookup=identity.key_lookup,
            replay_check=identity.replay_check_for(bob_ws["id"]),
        )

    before = await identity.sign_as(
        fid, type="chat.message", audience=bob["federation_id"], payload={"text": "hi"}
    )
    unused = await identity.sign_as(
        fid, type="chat.message", audience=bob["federation_id"], payload={"text": "held back"}
    )
    rotated = await identity.rotate(fid, actor="user:alice")
    assert rotated["key_set_version"] == 2
    assert sorted(k["state"] for k in rotated["keys"]) == ["active", "retiring"]
    # signed before rotation: still valid during the overlap window
    assert (await verify(before)).payload == {"text": "hi"}
    assert await _code(verify(before)) == "FED_E_REPLAY"
    after = await identity.sign_as(
        fid, type="chat.message", audience=bob["federation_id"], payload={"text": "again"}
    )
    assert after["key_id"] != before["key_id"]
    await verify(after)

    # revoking the old key is immediate
    revoked = await identity.revoke_key(fid, before["key_id"], actor="user:alice")
    assert revoked["key_set_version"] == 3
    # signed with that key before the revocation, never delivered: now refused
    assert unused["key_id"] == before["key_id"]
    assert await _code(verify(unused)) == "FED_E_KEY_REVOKED"
    fresh_old = await identity.sign_as(
        fid, type="chat.message", audience=bob["federation_id"], payload={"text": "new key"}
    )
    assert fresh_old["key_id"] == after["key_id"]

    # freeze stops signing and verification; unfreeze restores
    await identity.set_status(fid, "frozen", actor="user:alice")
    assert (
        await _code(
            identity.sign_as(fid, type="chat.message", audience=bob["federation_id"], payload={})
        )
        == "FED_E_IDENTITY_FROZEN"
    )
    assert await _code(verify(fresh_old)) == "FED_E_IDENTITY_FROZEN"
    await identity.set_status(fid, "active", actor="user:alice")
    await verify(fresh_old)

    # the identity log is a signed hash chain
    conn = await asyncpg.connect(db_dsn())
    try:
        rows = await conn.fetch(
            "SELECT serial, object_type, object_hash, previous_hash, envelope "
            "FROM federation_identity_log WHERE federation_id = $1 ORDER BY serial",
            fid,
        )
        assert [r["object_type"] for r in rows] == [
            "identity.record",
            "identity.rotation",
            "identity.revocation",
            "identity.freeze",
            "identity.unfreeze",
        ]
        for prev, row in zip(rows, rows[1:], strict=False):
            assert row["previous_hash"] == prev["object_hash"]
        record = json.loads(rows[0]["envelope"])
        assert record["issuer"] == alice["parent_id"] and record["audience"] is None
        await verify_envelope(
            record,
            local_workspace_fid=bob["federation_id"],
            key_lookup=identity.key_lookup,
            replay_check=identity.replay_check_for(bob_ws["id"]),
        )
        # identities are never deleted, and ids/genesis keys are immutable
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute("DELETE FROM federation_identities WHERE federation_id = $1", fid)
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "UPDATE federation_identities SET parent_id = NULL WHERE federation_id = $1", fid
            )
        with pytest.raises(asyncpg.PostgresError):  # wrong parent layer
            await conn.execute(
                "INSERT INTO federation_identities (federation_id, id_type, parent_id, "
                "genesis_public_key, "
                "expires_at) VALUES ('fed:agent:zzzzzzzzzzzzzzzzzzzzzzzzzz', 'agent', $1, 'g', "
                "NOW() + "
                "interval '1 day')",
                alice["parent_id"],
            )
    finally:
        await conn.close()

    # a tombstone is final and frees nothing
    await identity.set_status(fid, "tombstoned", actor="user:alice")
    assert (
        await _code(identity.set_status(fid, "active", actor="user:alice"))
        == "FED_E_IDENTITY_TOMBSTONED"
    )


async def _grant_row(conn, workspace_id: str, connection_id: str, local: str, peer: str) -> None:
    await conn.execute(
        "INSERT INTO federation_grants (connection_id, workspace_id, tenant_id, "
        "local_workspace_fid, "
        "peer_workspace_fid, initiator, state, outbound_scopes, inbound_scopes, grant_version, "
        "expires_at, reason) VALUES ($1, $2::uuid, '', $3, $4, 'local', 'REQUESTED', '{chat}', "
        "'{chat}', 1, "
        "NOW() + interval '90 days', 'pilot collaboration')",
        connection_id,
        workspace_id,
        local,
        peer,
    )


async def _history(conn, workspace_id, connection_id, version, action, *, approved_by=None):
    await conn.execute(
        "INSERT INTO federation_grant_history (workspace_id, tenant_id, connection_id, "
        "grant_version, "
        "action, to_state, outbound_scopes, inbound_scopes, expires_at, revocation_epoch, actor, "
        "approved_by, policy_hash) VALUES ($1::uuid, '', $2, $3, $4, 'ACTIVE', '{chat}', '{chat}', "
        "NOW() + interval '90 days', 0, 'user:owner', $5, repeat('0', 64))",
        workspace_id,
        connection_id,
        version,
        action,
        approved_by,
    )


async def test_grant_history_is_append_only_and_versioned(client, admin_headers):
    workspace = await _workspace(client, admin_headers)
    connection_id = new_connection_id()
    local, peer = "fed:workspace:" + "a" * 26, "fed:workspace:" + "b" * 26
    conn = await asyncpg.connect(db_dsn())
    try:
        await _grant_row(conn, workspace["id"], connection_id, local, peer)
        tenant = await conn.fetchval(
            "SELECT tenant_id FROM federation_grants WHERE connection_id = $1", connection_id
        )
        assert tenant.startswith("ten_")  # derived by the trigger, not trusted from the writer
        await _history(conn, workspace["id"], connection_id, 1, "request")
        with pytest.raises(asyncpg.PostgresError):  # GRANT-INV-6: versions go up by one
            await _history(conn, workspace["id"], connection_id, 3, "change_scopes")
        with pytest.raises(asyncpg.PostgresError):  # GRANT-INV-2: approval names a person
            await _history(conn, workspace["id"], connection_id, 2, "approve")
        await _history(conn, workspace["id"], connection_id, 2, "approve", approved_by="user:owner")
        with pytest.raises(asyncpg.PostgresError):  # GRANT-INV-5: append-only
            await conn.execute(
                "UPDATE federation_grant_history SET actor = 'system' WHERE connection_id = $1",
                connection_id,
            )
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "DELETE FROM federation_grant_history WHERE connection_id = $1", connection_id
            )
        with pytest.raises(asyncpg.PostgresError):  # unknown scope
            await conn.execute(
                "UPDATE federation_grants SET outbound_scopes = '{admin}' WHERE connection_id = $1",
                connection_id,
            )
        with pytest.raises(asyncpg.PostgresError):  # ADR-002: no re-delegation in P1
            await conn.execute(
                "UPDATE federation_grants SET may_redelegate = TRUE WHERE connection_id = $1",
                connection_id,
            )
    finally:
        await conn.close()


async def test_messages_are_idempotent(client, admin_headers):
    """F-007 at the storage layer: one business row per message id and per idempotency key."""
    workspace = await _workspace(client, admin_headers)
    connection_id = new_connection_id()
    peer = "fed:workspace:" + "b" * 26
    conn = await asyncpg.connect(db_dsn())
    try:
        await _grant_row(conn, workspace["id"], connection_id, "fed:workspace:" + "a" * 26, peer)

        async def insert(message_id, key):
            await conn.execute(
                "INSERT INTO federation_messages (workspace_id, tenant_id, direction, message_id, "
                "connection_id, peer_workspace_fid, idempotency_key, kind, sender_fid, "
                "grant_version, "
                "payload, payload_sha256, state, expires_at) VALUES ($1::uuid, '', 'in', $2, "
                "$3, $4, $5, "
                "'chat.message', $4, 1, '{}'::jsonb, repeat('0', 64), 'received', NOW() + "
                "interval '1 hour')",
                workspace["id"],
                message_id,
                connection_id,
                peer,
                key,
            )

        await insert("fmsg_1", "k1")
        with pytest.raises(asyncpg.UniqueViolationError):
            await insert("fmsg_1", "k2")
        with pytest.raises(asyncpg.UniqueViolationError):
            await insert("fmsg_2", "k1")
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM federation_messages WHERE connection_id = $1", connection_id
            )
            == 1
        )
        with pytest.raises(asyncpg.PostgresError):  # P1 is server_readable only (ADR-009)
            await conn.execute(
                "UPDATE federation_messages SET payload_encoding = 'aes' WHERE connection_id = $1",
                connection_id,
            )
    finally:
        await conn.close()


async def test_replay_guard_is_per_workspace(client, admin_headers):
    a = await _workspace(client, admin_headers)
    b = await _workspace(client, admin_headers)
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    digest = uuid.uuid4().hex * 2
    assert await identity.replay_check_for(a["id"])(digest, expires) is True
    assert await identity.replay_check_for(a["id"])(digest, expires) is False
    assert await identity.replay_check_for(b["id"])(digest, expires) is True
