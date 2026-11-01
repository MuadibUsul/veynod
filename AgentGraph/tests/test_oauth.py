"""OAuth 2.1 for MCP clients (v3.36): discovery, registration, consent, PKCE."""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest

pytestmark = pytest.mark.integration

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def _owner(client) -> dict[str, str]:
    registered = await client.post("/v1/auth/register", json={
        "email": f"oauth-{uuid.uuid4().hex[:8]}@example.test",
        "password": "correct-horse-battery-staple",
        "display_name": "OAuth Owner", "workspace_name": "OAuth Workspace",
    })
    assert registered.status_code == 201, registered.text
    return {"Authorization": f"Bearer {registered.json()['token']}"}


async def _register(client) -> str:
    created = await client.post("/oauth/register", json={
        "client_name": "Claude", "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none"})
    assert created.status_code == 201, created.text
    assert created.json()["token_endpoint_auth_method"] == "none"
    return created.json()["client_id"]


async def _authorize(client, client_id: str, challenge: str, state: str = "st-1") -> str:
    response = await client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "scope": "mcp", "resource": "http://testserver/mcp/"}, follow_redirects=False)
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith("/dashboard/oauth/?request=")
    return parse_qs(urlsplit(location).query)["request"][0]


async def test_discovery_documents_and_the_mcp_challenge(client):
    resource = (await client.get("/.well-known/oauth-protected-resource")).json()
    assert resource["resource"].endswith("/mcp/")
    issuer = resource["authorization_servers"][0]
    nested = await client.get("/.well-known/oauth-protected-resource/mcp/")
    assert nested.json() == resource
    server = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert server["issuer"] == issuer
    assert server["code_challenge_methods_supported"] == ["S256"]
    assert server["registration_endpoint"].endswith("/oauth/register")
    assert (await client.get("/.well-known/openid-configuration")).json() == server

    challenge = await client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1,
                                                 "method": "initialize", "params": {}})
    assert challenge.status_code == 401
    assert 'resource_metadata="' in challenge.headers["www-authenticate"]
    assert "/.well-known/oauth-protected-resource" in challenge.headers["www-authenticate"]


async def test_claude_connects_with_pkce_and_gets_a_working_key(client):
    owner = await _owner(client)
    client_id = await _register(client)
    verifier, challenge = _pkce()
    request_id = await _authorize(client, client_id, challenge)

    view = (await client.get(f"/v1/auth/oauth/requests/{request_id}", headers=owner)).json()
    assert view["status"] == "pending" and view["client_name"] == "Claude"
    assert view["redirect_host"] == "claude.ai" and view["can_approve"] is True
    assert (await client.get(f"/v1/auth/oauth/requests/{request_id}")).status_code == 401

    approved = await client.post(f"/v1/auth/oauth/requests/{request_id}/approve",
                                 headers=owner, json={"agent_signature": "claude"})
    assert approved.status_code == 200, approved.text
    target = approved.json()["redirect_to"]
    assert target.startswith(REDIRECT + "?")
    query = parse_qs(urlsplit(target).query)
    assert query["state"] == ["st-1"] and "iss" in query
    code = query["code"][0]

    wrong = await client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": secrets.token_urlsafe(48)})
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
    issued = await client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": verifier})
    assert issued.status_code == 200, issued.text
    assert issued.headers["cache-control"] == "no-store"
    body = issued.json()
    assert body["token_type"] == "Bearer" and body["access_token"].startswith("agt_")

    agent = {"Authorization": f"Bearer {body['access_token']}"}
    assert (await client.get("/v1/orientation", headers=agent)).status_code == 200
    replay = await client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": verifier})
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"


async def test_long_oauth_state_is_returned_unchanged(client):
    """Gemini's encrypted state is multi-KB; never truncate it."""
    owner = await _owner(client)
    client_id = await _register(client)
    _, challenge = _pkce()
    state = "g" * 2_000
    request_id = await _authorize(client, client_id, challenge, state=state)
    approved = await client.post(f"/v1/auth/oauth/requests/{request_id}/approve",
                                 headers=owner, json={"agent_signature": "gemini"})
    assert approved.status_code == 200, approved.text
    query = parse_qs(urlsplit(approved.json()["redirect_to"]).query)
    assert query["state"] == [state]


async def test_refusals_and_bad_requests(client):
    owner = await _owner(client)
    client_id = await _register(client)
    _, challenge = _pkce()

    bad_client = await client.post("/oauth/register", json={"redirect_uris": ["http://evil"]})
    assert bad_client.status_code == 400
    assert bad_client.json()["error"] == "invalid_redirect_uri"
    unknown = await client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": "vnd_nope", "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256"}, follow_redirects=False)
    assert unknown.status_code == 400
    elsewhere = await client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": "https://evil.test/cb",
        "code_challenge": challenge, "code_challenge_method": "S256"}, follow_redirects=False)
    assert elsewhere.status_code == 400
    plain = await client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "plain", "state": "x"},
        follow_redirects=False)
    assert plain.status_code == 307 or plain.status_code == 302
    assert "error=invalid_request" in plain.headers["location"]
    assert plain.headers["location"].startswith(REDIRECT)

    request_id = await _authorize(client, client_id, challenge, state="st-2")
    refused = await client.post(f"/v1/auth/oauth/requests/{request_id}/deny", headers=owner)
    location = refused.json()["redirect_to"]
    assert "error=access_denied" in location and "state=st-2" in location
    again = await client.post(f"/v1/auth/oauth/requests/{request_id}/approve", headers=owner,
                              json={})
    assert again.status_code == 409
    unsupported = await client.post("/oauth/token", data={"grant_type": "password"})
    assert unsupported.json()["error"] == "unsupported_grant_type"
