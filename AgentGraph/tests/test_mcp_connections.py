"""MCP connections: one installation, one credential, and an expiry that cleans up.

docs/MCP_CONNECTION_LIFECYCLE.md, rollout steps 1-3. Signing in again from the
same installation rotates the same credential instead of minting ``cursor-2``;
a token from before connections is adopted; a credential the CLI issued expires
and is revoked (never deleted) once nobody uses it; a CLI that sends no
installation id behaves as before.
"""

from __future__ import annotations

import uuid

import pytest

from tests.test_growth import _keypair, _open, _signup
from tests.test_loop_reliability import _fetch
from tests.test_scheduler import _sql

pytestmark = pytest.mark.integration


async def _sign_in(client, owner: dict, signature: str, *, instance: str | None = None,
                   label: str = "dev-box", expect: int = 200, **approve) -> dict:
    """One `npx veynod` run: start, approve, collect. The opened key rides along."""
    # Sign-ins are limited per address per minute; these tests make many.
    await _sql("DELETE FROM rate_limit_windows WHERE bucket LIKE 'login:cli:%'")
    private, public = _keypair()
    body = {"public_key": public, "client_label": label, "clients": ["cursor"]}
    if instance:
        body["client_instance_id"] = instance
    started = await client.post("/v1/auth/cli/sessions", json=body)
    assert started.status_code == 201, started.text
    flow = started.json()
    approved = await client.post(f"/v1/auth/cli/sessions/{flow['user_code']}/approve",
                                 headers=owner, json={"agent_signature": signature, **approve})
    assert approved.status_code == expect, approved.text
    if expect != 200:
        return approved.json()
    collected = (await client.post("/v1/auth/cli/poll",
                                   json={"device_code": flow["device_code"]})).json()
    return {**approved.json(), "api_key": _open(collected["sealed_key"], private)}


async def _tokens(owner_email: str, signature_like: str) -> list:
    return await _fetch(
        "SELECT t.id, t.agent_signature, t.connection_id, t.expires_at, t.revoked_at, "
        "t.revoke_reason, t.is_active, t.canonical_id FROM agent_tokens t "
        "JOIN human_users u ON u.workspace_id = t.workspace_id "
        "WHERE u.email = $1 AND t.agent_signature LIKE $2 ORDER BY t.created_at",
        owner_email, signature_like)


def _agent(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def _works(client, key: str) -> bool:
    return (await client.get("/v1/whoami", headers=_agent(key))).status_code == 200


async def test_the_same_installation_twice_is_one_connection_and_one_credential(client):
    owner, email = await _signup(client)
    instance = str(uuid.uuid4())

    first = await _sign_in(client, owner, "cursor", instance=instance)
    assert (first["agent_signature"], first["credential"]) == ("cursor", "created")
    assert first["connection_id"]
    assert await _works(client, first["api_key"])

    second = await _sign_in(client, owner, "cursor", instance=instance)
    # The same credential with a new secret: no cursor-2.
    assert (second["agent_signature"], second["credential"]) == ("cursor", "rotated")
    assert second["connection_id"] == first["connection_id"]
    assert second["api_key"] != first["api_key"]
    assert await _works(client, second["api_key"])
    # The previous secret keeps working for the rotation's grace window.
    assert await _works(client, first["api_key"])

    rows = await _tokens(email, "cursor%")
    assert [row["agent_signature"] for row in rows] == ["cursor"]
    assert str(rows[0]["connection_id"]) == first["connection_id"]
    listed = (await client.get("/v1/human/connections", headers=owner)).json()
    assert [(item["label"], item["status"], item["active_credentials"])
            for item in listed["items"]] == [("dev-box", "active", 1)]
    assert listed["counts"] == {"active": 1, "dormant": 0, "revoked": 0}
    # The token list says which installation a credential belongs to.
    tokens = (await client.get("/v1/human/agent-tokens", headers=owner)).json()["items"]
    mine = next(item for item in tokens if item["agent_signature"] == "cursor")
    assert (mine["connection_id"], mine["connection_label"], mine["connection_status"]) == (
        first["connection_id"], "dev-box", "active")


async def test_a_reconnect_takes_the_grant_the_owner_approved_now(client):
    owner, email = await _signup(client)
    instance = str(uuid.uuid4())
    await _sign_in(client, owner, "cursor", instance=instance)
    again = await _sign_in(client, owner, "cursor", instance=instance, role_scope="AUDITOR",
                           allowed_namespaces=["default"])
    assert again["credential"] == "rotated"
    row = await _fetch("SELECT t.role_scope::text AS role, t.allowed_namespaces "
                       "FROM agent_tokens t "
                       "JOIN human_users u ON u.workspace_id = t.workspace_id WHERE u.email = $1 "
                       "AND t.agent_signature = 'cursor'", email)
    assert (row[0]["role"], list(row[0]["allowed_namespaces"])) == ("AUDITOR", ["default"])
    assert await _works(client, again["api_key"])
    assert len(await _tokens(email, "cursor%")) == 1


async def test_two_installations_are_two_connections_never_merged(client):
    owner, email = await _signup(client)
    one = await _sign_in(client, owner, "cursor", instance=str(uuid.uuid4()), label="laptop")
    two = await _sign_in(client, owner, "cursor", instance=str(uuid.uuid4()), label="desktop")
    assert one["connection_id"] != two["connection_id"]
    # The name belongs to the first installation: the second gets its own, and
    # the first one's credential is not taken over.
    assert (two["agent_signature"], two["credential"]) == ("cursor-2", "created")
    assert await _works(client, one["api_key"]) and await _works(client, two["api_key"])
    assert len(await _tokens(email, "cursor%")) == 2


async def test_a_token_from_before_connections_is_adopted_not_duplicated(client):
    owner, email = await _signup(client)
    # A token minted the old way: no installation behind it.
    legacy = await client.post("/v1/human/agent-tokens", headers=owner, json={
        "agent_signature": "cursor", "role_scope": "EXECUTOR", "allowed_namespaces": ["*"]})
    assert legacy.status_code == 201, legacy.text
    before = await _tokens(email, "cursor%")
    assert len(before) == 1 and before[0]["connection_id"] is None

    adopted = await _sign_in(client, owner, "cursor", instance=str(uuid.uuid4()))
    assert (adopted["agent_signature"], adopted["credential"]) == ("cursor", "adopted")
    after = await _tokens(email, "cursor%")
    assert [row["id"] for row in after] == [before[0]["id"]], "the same row, no cursor-2"
    assert str(after[0]["connection_id"]) == adopted["connection_id"]
    assert after[0]["expires_at"] is not None
    assert await _works(client, adopted["api_key"])


async def test_a_cli_credential_expires_and_is_revoked_never_deleted(client):
    from app.services import connections

    owner, email = await _signup(client)
    signed = await _sign_in(client, owner, "claude-code", instance=str(uuid.uuid4()))
    row = (await _tokens(email, "claude-code"))[0]
    life = await _fetch("SELECT EXTRACT(EPOCH FROM (expires_at - NOW())) / 86400 AS days "
                        "FROM agent_tokens WHERE id = $1", row["id"])
    assert 89 < float(life[0]["days"]) <= 90, "a default lifetime of 90 days"

    # The client's configuration was deleted long ago: nobody used the key since.
    await _sql("UPDATE agent_tokens SET expires_at = NOW() - INTERVAL '1 hour', "
               "created_at = NOW() - INTERVAL '100 days' WHERE id = $1", row["id"])
    assert not await _works(client, signed["api_key"]), "an expired key stops at its expiry"
    counts = await connections.upkeep(force=True)
    assert counts["expired"] >= 1
    after = (await _tokens(email, "claude-code"))[0]
    assert after["id"] == row["id"], "the row stays: it is the audit trail"
    assert after["revoked_at"] is not None and after["is_active"] is False
    assert after["revoke_reason"] == "expired"
    assert not await _works(client, signed["api_key"])

    # Signing in again from the same installation issues a credential again.
    back = await _sign_in(client, owner, "claude-code", instance=signed["connection_id"][:8]
                          + str(uuid.uuid4())[8:])
    assert await _works(client, back["api_key"])


async def test_a_credential_in_use_never_runs_out(client):
    from app.services import connections

    owner, email = await _signup(client)
    signed = await _sign_in(client, owner, "claude-code", instance=str(uuid.uuid4()))
    row = (await _tokens(email, "claude-code"))[0]
    # Ten days left, and the agent was heard from today.
    seen = await client.get("/v1/orientation", headers=_agent(signed["api_key"]))
    assert seen.status_code == 200
    await _sql("UPDATE agent_tokens SET expires_at = NOW() + INTERVAL '10 days' WHERE id = $1",
               row["id"])
    await _sql("UPDATE agent_directory SET last_seen_at = NOW() "
               "WHERE $1 = ANY(token_ids)", row["canonical_id"])
    counts = await connections.upkeep(force=True)
    assert counts["extended"] >= 1
    life = await _fetch("SELECT EXTRACT(EPOCH FROM (expires_at - NOW())) / 86400 AS days "
                        "FROM agent_tokens WHERE id = $1", row["id"])
    assert float(life[0]["days"]) > 89


async def test_an_unused_connection_goes_dormant_then_is_revoked(client):
    from app.services import connections

    owner, email = await _signup(client)
    signed = await _sign_in(client, owner, "cursor", instance=str(uuid.uuid4()))
    cid = signed["connection_id"]

    async def aged(days: int) -> None:
        await _sql("UPDATE mcp_connections SET created_at = NOW() - make_interval(days => $2), "
                   "last_seen_at = NOW() - make_interval(days => $2) WHERE id = $1::uuid",
                   cid, days)
        await _sql("UPDATE agent_tokens SET created_at = NOW() - make_interval(days => $2), "
                   "last_seen_at = NULL WHERE connection_id = $1::uuid", cid, days)
        await _sql("UPDATE agent_directory d SET last_seen_at = NOW() - make_interval(days => $2) "
                   "FROM agent_tokens t WHERE t.connection_id = $1::uuid "
                   "AND t.canonical_id = ANY(d.token_ids)", cid, days)

    async def status() -> str:
        return (await _fetch("SELECT status FROM mcp_connections WHERE id = $1::uuid",
                             cid))[0]["status"]

    await aged(61)
    await connections.upkeep(force=True)
    assert await status() == "dormant"
    assert await _works(client, signed["api_key"]), "dormant is a label; the key still works"

    await aged(91)
    await connections.upkeep(force=True)
    assert await status() == "revoked"
    assert not await _works(client, signed["api_key"])
    row = (await _tokens(email, "cursor"))[0]
    assert row["revoke_reason"] == "connection_dormant" and row["revoked_at"] is not None


async def test_revoking_a_connection_stops_every_secret_and_keeps_the_rows(client, admin_headers):
    owner, email = await _signup(client)
    instance = str(uuid.uuid4())
    first = await _sign_in(client, owner, "cursor", instance=instance)
    second = await _sign_in(client, owner, "cursor", instance=instance)
    revoked = await client.post(f"/v1/human/connections/{first['connection_id']}/revoke",
                                headers=owner)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked" and len(revoked.json()["credentials_revoked"]) == 1
    # The current secret and the previous one (still inside its grace window).
    assert not await _works(client, second["api_key"])
    assert not await _works(client, first["api_key"])
    assert len(await _tokens(email, "cursor")) == 1, "nothing was deleted"

    hidden = (await client.get("/v1/human/connections", headers=owner)).json()
    assert hidden["items"] == [] and hidden["counts"]["revoked"] == 1
    shown = (await client.get("/v1/human/connections", headers=owner,
                              params={"include_inactive": "true"})).json()
    assert shown["items"][0]["status"] == "revoked"
    assert shown["items"][0]["credentials"][0]["revoke_reason"] == "revoked in the console"

    # That installation issues nothing more until it starts over.
    refused = await _sign_in(client, owner, "cursor", instance=instance, expect=409)
    assert refused["error"]["code"] == "connection_revoked"
    assert (await client.post(f"/v1/human/connections/{uuid.uuid4()}/revoke",
                              headers=owner)).status_code == 404

    # The operator sees counts, not the installations.
    workspace = (await _fetch("SELECT workspace_id FROM human_users WHERE email = $1", email))[0]
    counts = await client.get(f"/v1/admin/workspaces/{workspace['workspace_id']}/connections",
                              headers=admin_headers)
    assert counts.status_code == 200, counts.text
    assert counts.json()["counts"] == {"active": 0, "dormant": 0, "revoked": 1}
    assert "items" not in counts.json()


async def test_a_cli_that_sends_no_installation_id_works_as_before(client):
    owner, email = await _signup(client)
    first = await _sign_in(client, owner, "claude-code")
    second = await _sign_in(client, owner, "claude-code")
    # The old behaviour: a new token each time, a suffix when the name is taken.
    assert (first["agent_signature"], second["agent_signature"]) == ("claude-code", "claude-code-2")
    assert first["connection_id"] is None and first["credential"] == "created"
    assert await _works(client, first["api_key"]) and await _works(client, second["api_key"])
    rows = await _tokens(email, "claude-code%")
    assert [row["connection_id"] for row in rows] == [None, None]
    # Still a CLI credential: it has the default lifetime.
    assert all(row["expires_at"] is not None for row in rows)
    assert (await client.get("/v1/human/connections", headers=owner)).json()["items"] == []
    await _sql("DELETE FROM rate_limit_windows WHERE bucket LIKE 'login:cli:%'")
    bad = await client.post("/v1/auth/cli/sessions", json={
        "public_key": _keypair()[1], "client_instance_id": "no spaces allowed"})
    assert bad.status_code == 422
