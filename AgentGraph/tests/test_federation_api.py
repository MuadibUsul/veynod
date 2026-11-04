"""Federation HTTP API (round 2): identity, handles and connections end to end
through a federation-enabled server (app/api/federation_routes.py)."""

from __future__ import annotations

import base64
import json
import os
import re
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from tests.conftest import run_server

pytestmark = pytest.mark.integration

ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="module")
async def fed(api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url
    env = {
        "FEDERATION_ENABLED": "true",
        "FEDERATION_KEK": base64.b64encode(os.urandom(32)).decode(),
    }
    async with run_server(env, ".server-federation.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as http:
            yield http


async def _workspace(client: httpx.AsyncClient, name: str) -> dict:
    created = await client.post(
        "/v1/admin/workspaces", headers=ADMIN, json={"name": f"{name}-{uuid.uuid4().hex[:6]}"}
    )
    assert created.status_code == 201, created.text
    body = created.json()
    ws: dict = {
        "id": body["workspace"]["id"],
        "key": {"Authorization": f"Bearer {body['api_key']}"},
    }
    for role in ("OWNER", "REVIEWER"):
        email = f"{role.lower()}-{uuid.uuid4().hex[:8]}@example.test"
        made = await client.post(
            f"/v1/admin/workspaces/{ws['id']}/human-users",
            headers=ADMIN,
            json={"email": email, "display_name": role, "role": role, "password": PASSWORD},
        )
        assert made.status_code in (200, 201), made.text
        login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert login.status_code == 200, login.text
        ws[role.lower()] = {"Authorization": f"Bearer {login.json()['token']}"}
    return ws


def _refused(response: httpx.Response, status: int, code: str, federation_code: str) -> None:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code, error
    assert error["details"]["federation_code"] == federation_code, error


def _no_local_ids(response: httpx.Response, *local: str) -> None:
    blob = json.dumps(response.json())
    assert not re.search(r"\b(prn|tok|ten|agk)_[A-Za-z0-9]", blob), blob
    for value in local:
        assert value not in blob


async def test_disabled_deployment_answers_federation_disabled(client):
    ws = await _workspace(client, "off")
    response = await client.get("/v1/federation/me", headers=ws["owner"])
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "federation_disabled"


async def test_connection_lifecycle_over_http(fed):
    a, b = await _workspace(fed, "api-a"), await _workspace(fed, "api-b")
    assert (await fed.get("/v1/federation/me", headers=a["owner"])).json() == {"identity": None}

    # Only an owner-level person enables federation for a workspace.
    _refused(
        await fed.post("/v1/federation/enable", headers=a["reviewer"]),
        403,
        "federation_forbidden",
        "FED_E_APPROVAL_NOT_HUMAN",
    )
    _refused(
        await fed.post("/v1/federation/enable", headers=a["key"]),
        403,
        "federation_forbidden",
        "FED_E_APPROVAL_NOT_HUMAN",
    )
    enabled_a = await fed.post("/v1/federation/enable", headers=a["owner"])
    assert enabled_a.status_code == 200, enabled_a.text
    a_fid = enabled_a.json()["identity"]["federation_id"]
    _no_local_ids(enabled_a, a["id"])
    b_fid = (await fed.post("/v1/federation/enable", headers=b["owner"])).json()["identity"][
        "federation_id"
    ]

    # A handle is published by the owner and resolves to the federation id.
    handle = f"lab-{uuid.uuid4().hex[:8]}"
    published = await fed.put(
        "/v1/federation/me/alias", headers=b["owner"], json={"handle": handle}
    )
    assert published.status_code == 200, published.text
    resolved = await fed.get(
        "/v1/federation/resolve", headers=a["owner"], params={"workspace": handle}
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "unique"
    assert resolved.json()["matches"][0]["federation_id"] == b_fid
    looked_up = await fed.get(f"/v1/federation/identities/{b_fid}", headers=a["reviewer"])
    assert looked_up.json()["identity"]["display_alias"] == handle
    _no_local_ids(looked_up, b["id"])

    # A's owner asks; B's reviewer cannot approve; B's owner approves fewer scopes.
    asked = await fed.post(
        "/v1/federation/connections",
        headers=a["owner"],
        json={
            "peer_workspace": b_fid,
            "outbound_scopes": ["chat", "delegate"],
            "inbound_scopes": ["chat"],
            "reason": "joint research",
        },
    )
    assert asked.status_code == 201, asked.text
    cid = asked.json()["connection"]["connection_id"]
    _no_local_ids(asked, a["id"], b["id"])
    pending = (await fed.get("/v1/federation/connections", headers=b["reviewer"])).json()["items"]
    assert [(c["connection_id"], c["state"], c["peer_workspace"]) for c in pending] == [
        (cid, "REQUESTED", a_fid)
    ]
    _refused(
        await fed.post(f"/v1/federation/connections/{cid}/approve", headers=b["reviewer"]),
        403,
        "federation_forbidden",
        "FED_E_APPROVAL_NOT_HUMAN",
    )
    _refused(
        await fed.post(f"/v1/federation/connections/{cid}/approve", headers=b["key"]),
        403,
        "federation_forbidden",
        "FED_E_APPROVAL_NOT_HUMAN",
    )
    approved = await fed.post(
        f"/v1/federation/connections/{cid}/approve",
        headers=b["owner"],
        json={"inbound_scopes": ["chat"]},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["connection"]["state"] == "ACTIVE"
    mine = (await fed.get("/v1/federation/connections", headers=a["owner"])).json()["items"][0]
    assert (mine["state"], mine["outbound_scopes"]) == ("ACTIVE", ["chat"])

    # Unknown scopes are refused by validation; narrowing works; widening does not.
    bad = await fed.post(
        "/v1/federation/connections",
        headers=a["owner"],
        json={"peer_workspace": b_fid, "outbound_scopes": ["admin"], "reason": "too much"},
    )
    assert bad.status_code == 422, bad.text
    widened = await fed.patch(
        f"/v1/federation/connections/{cid}/scopes",
        headers=b["reviewer"],
        json={"outbound_scopes": ["chat", "delegate"], "inbound_scopes": ["chat"]},
    )
    _refused(widened, 403, "federation_forbidden", "FED_E_SCOPE_DENIED")

    # Either side's person revokes; both sides see it; history is per side.
    revoked = await fed.post(f"/v1/federation/connections/{cid}/revoke", headers=a["reviewer"])
    assert revoked.json()["connection"]["state"] == "REVOKED"
    theirs = (await fed.get("/v1/federation/connections", headers=b["owner"])).json()["items"][0]
    assert theirs["state"] == "REVOKED"
    _refused(
        await fed.post(f"/v1/federation/connections/{cid}/revoke", headers=b["owner"]),
        409,
        "federation_conflict",
        "FED_E_GRANT_TRANSITION",
    )
    history = (
        await fed.get(f"/v1/federation/connections/{cid}/history", headers=b["owner"])
    ).json()["items"]
    assert [h["action"] for h in history] == ["request", "approve", "revoke"]
    assert history[1]["approved_by"].startswith("user:")

    # A connection id from another workspace is not visible here.
    _refused(
        await fed.get(
            f"/v1/federation/connections/{cid}/history",
            headers=(await _workspace(fed, "api-c"))["owner"],
        ),
        404,
        "federation_not_found",
        "FED_E_GRANT_UNKNOWN",
    )
