"""Tokens and leases from the 2026-10-24 playtest (TASK-999A4453, batch B)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest

from tests.conftest import run_server

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(scope="module")
async def tree(api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url
    async with run_server({"CHILD_TOKEN_ISSUANCE": "true"}, ".server-lifecycle.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as http:
            yield http


async def _workspace(client) -> dict:
    created = await client.post("/v1/admin/workspaces", headers=ADMIN,
                                json={"name": f"Life {uuid.uuid4().hex[:6]}", "tier": "pro"})
    body = created.json()
    return {"id": body["workspace"]["id"], "key": _bearer(body["api_key"]),
            "root": _bearer(body["root_key"])}


async def _issued(client, headers, **body) -> dict:
    payload = {"scopes": ["docs:read", "docs:write", "tasks:read", "tasks:write"],
               "purpose": "test", **body}
    payload.setdefault("agent", {"agent_id": f"a-{uuid.uuid4().hex[:8]}"})
    response = await client.post("/v1/tokens", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    data = response.json()
    data["headers"] = _bearer(data["token"])
    return data


async def test_expired_tokens_renew_in_place_and_secrets_hot_swap(tree):
    client = tree
    ws = await _workspace(client)
    parent = await _issued(client, ws["root"],
                           scopes=["docs:read", "docs:write", "token:issue"], max_descendants=5)
    child = await _issued(client, parent["headers"], scopes=["docs:read"], ttl_seconds=3_600)

    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("UPDATE agent_tokens SET expires_at = NOW() - interval '1 hour' "
                           "WHERE canonical_id = $1", child["token_id"])
    finally:
        await conn.close()
    assert (await client.get("/v1/whoami", headers=child["headers"])).status_code == 401

    # Decision 5: renewed in place, same id and secret: no r2 -> r3 -> r4.
    renewed = await client.post(f"/v1/tokens/{child['token_id']}/renew",
                                headers=parent["headers"], json={"ttl_seconds": 3_600})
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["renewed_after_expiry"] is True
    me = await client.get("/v1/whoami", headers=child["headers"])
    assert me.status_code == 200, me.text

    # #21: a new secret for the same token; the old one keeps working for a while.
    rotated = await client.post(f"/v1/tokens/{child['token_id']}/rotate",
                                headers=child["headers"])
    assert rotated.status_code == 200, rotated.text
    fresh = _bearer(rotated.json()["token"])
    assert rotated.json()["token_id"] == child["token_id"]
    assert (await client.get("/v1/whoami", headers=fresh)).status_code == 200
    assert (await client.get("/v1/whoami", headers=child["headers"])).status_code == 200
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("UPDATE agent_tokens SET previous_key_valid_until = NOW() "
                           "WHERE canonical_id = $1", child["token_id"])
    finally:
        await conn.close()
    assert (await client.get("/v1/whoami", headers=child["headers"])).status_code == 401
    assert (await client.get("/v1/whoami", headers=fresh)).status_code == 200


async def test_agent_facing_errors_point_somewhere(client, admin_headers, workspace,
                                                   api_base_url):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    created = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": f"e_{uuid.uuid4().hex[:6]}", "role_scope": "EXECUTOR",
              "allowed_namespaces": ["*"]})
    agent = _bearer(created.json()["api_key"])

    # #16: /v1/auth/me sends an agent to /v1/whoami instead of "sign in again".
    me = await client.get("/v1/auth/me", headers=agent)
    assert me.status_code == 403
    assert me.json()["error"]["details"]["use"] == "/v1/whoami"
    # Decision 6: leases default to 15 minutes.
    assert (await client.get("/v1/whoami", headers=agent)).json()["default_lease_seconds"] == 900

    async with httpx.AsyncClient(headers=agent, timeout=60) as http:
        async with streamable_http_client(f"{api_base_url}/mcp/", http_client=http) as (
                read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                # #15: serverInfo.version is the product version.
                assert init.serverInfo.version == (
                    await client.get("/v1/whoami", headers=agent)).json()["version"]
                # #22: permission before validation.
                review = await session.call_tool("review_task", {
                    "task_key": "nope", "decision": "approve",
                    "items": [{"item_id": "a", "result": "PASS", "note": "x"}]})
                assert review.isError
                assert review.structuredContent["code"] == "forbidden"
                # #23: the message names the missing field.
                missing = await session.call_tool("get_watermark", {})
                assert missing.isError
                assert "name" in missing.structuredContent["message"]
