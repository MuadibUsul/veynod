"""Trust and visibility fixes from the 2026-10-24 playtest (TASK-999A4453, batch A)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from tests.conftest import run_server

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(scope="module")
async def tree(api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url
    async with run_server({"CHILD_TOKEN_ISSUANCE": "true"}, ".server-trust.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as http:
            yield http


async def _workspace(client) -> dict:
    created = await client.post("/v1/admin/workspaces", headers=ADMIN,
                                json={"name": f"Trust {uuid.uuid4().hex[:6]}", "tier": "pro"})
    assert created.status_code in (200, 201), created.text
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


async def _put(client, headers, key, content, **extra):
    response = await client.put(f"/v1/documents/{key}", headers=headers,
                                json={"content": content, "expected_version": 0, **extra})
    assert response.status_code in (200, 201), response.text
    return response.json()


async def test_family_drafts_receipts_and_hidden_hints(tree):
    client = tree
    ws = await _workspace(client)
    for namespace in ("ops", "research"):
        await client.post("/v1/namespaces", headers=ws["key"], json={"namespace": namespace})
    branch = await _issued(client, ws["root"],
                           scopes=["docs:read", "docs:write", "token:issue"],
                           namespaces={"read": ["*"], "write": ["ops"]}, max_descendants=5)
    child = await _issued(client, branch["headers"], scopes=["docs:read", "docs:write"],
                          namespaces={"read": ["ops"], "write": ["ops"]})
    stranger = await _issued(client, ws["root"], scopes=["docs:read", "docs:write"],
                             namespaces={"read": ["ops"], "write": ["ops"]})

    # #10: no namespace in the body, a prefixed key: the prefix is the namespace.
    draft = await _put(client, branch["headers"], "ops/handbook", "the handbook")
    assert draft["document"]["namespace"] == "ops"
    # #4: the receipt says what a reader needs.
    assert draft["read_with"]["flags"] == {"include_unverified": True}
    assert draft["read_with"]["uri"] == "doc://ops/handbook"

    # #1/#3: the child reads its issuer's draft without flags; a stranger gets a
    # 404 that names the flags (#5).
    assert (await client.get("/v1/documents/ops/handbook",
                             headers=child["headers"])).status_code == 200
    hidden = await client.get("/v1/documents/ops/handbook", headers=stranger["headers"])
    assert hidden.status_code == 404
    details = hidden.json()["error"]["details"]
    assert details["hidden_by"] == ["unverified"]
    assert details["retry_with"] == {"include_unverified": True}

    # #2: citing a teammate's internal draft no longer taints the citing doc.
    summary = await _put(client, child["headers"], "ops/summary",
                         "see doc://ops/handbook for details")
    assert summary["document"]["tainted"] is False
    assert summary["read_with"]["tainted"] is False


async def test_namespace_prefix_uri_and_access_map(tree):
    client = tree
    ws = await _workspace(client)
    await client.post("/v1/namespaces", headers=ws["key"], json={"namespace": "tech"})
    await client.post("/v1/namespaces", headers=ws["key"], json={"namespace": "research"})
    await _put(client, ws["key"], "ai-dev-handbook", "handbook", namespace="tech")

    # #6: doc://<namespace>/<doc_key> resolves, with a warning naming the short form.
    read = await client.post("/v1/dereference", headers=ws["key"], json={
        "uri": "doc://tech/ai-dev-handbook", "include_unverified": True})
    assert read.status_code == 200, read.text
    assert "uri_namespace_prefix" in [item["code"] for item in read.json()["warnings"]]

    # #13/#14: my_access and whoami follow the write map, not the read map.
    agent = await _issued(client, ws["root"], scopes=["docs:read", "docs:write"],
                          namespaces={"read": ["*"], "write": ["tech"]})
    listed = (await client.get("/v1/namespaces", headers=agent["headers"])).json()["items"]
    access = {item["namespace"]: item["my_access"] for item in listed}
    assert access["tech"] == "write" and access["research"] == "read"
    me = (await client.get("/v1/whoami", headers=agent["headers"])).json()
    assert me["namespaces"] == {"read": ["*"], "write": ["tech"]}
    assert "doc:read" in me["scope_permissions"]["docs:read"]


async def test_register_namespace_and_delete(tree, client):
    del client
    http = tree
    ws = await _workspace(http)
    await http.post("/v1/namespaces", headers=ws["key"], json={"namespace": "ops"})
    branch = await _issued(http, ws["root"], scopes=["docs:read", "docs:write", "token:issue"],
                           namespaces={"read": ["*"], "write": ["ops"]}, max_descendants=5)
    child = await _issued(http, branch["headers"], scopes=["docs:read", "docs:write"])

    # #19 / decision 2: outside the write map, and from delegated tokens: refused.
    outside = await http.post("/v1/namespaces", headers=branch["headers"],
                              json={"namespace": "evil-trial-ns"})
    assert outside.status_code == 403
    delegated = await http.post("/v1/namespaces", headers=child["headers"],
                                json={"namespace": "ops-two"})
    assert delegated.status_code == 403
    assert delegated.json()["error"]["code"] == "namespace_register_not_permitted"


async def test_owner_removes_an_empty_namespace(client):
    registered = await client.post("/v1/auth/register", json={
        "email": f"ns-{uuid.uuid4().hex[:8]}@example.test",
        "password": "correct-horse-battery-staple",
        "display_name": "Owner", "workspace_name": "Namespaces"})
    assert registered.status_code == 201, registered.text
    owner = _bearer(registered.json()["token"])
    assert (await client.post("/v1/namespaces", headers=owner,
                              json={"namespace": "evil-trial-ns"})).status_code in (200, 201)
    await client.post("/v1/namespaces", headers=owner, json={"namespace": "busy"})
    await client.put("/v1/documents/busy/x", headers=owner,
                     json={"namespace": "busy", "content": "x", "expected_version": 0})

    busy = await client.delete("/v1/human/namespaces/busy", headers=owner)
    assert busy.status_code == 409 and busy.json()["error"]["details"]["documents"] == 1
    protected = await client.delete("/v1/human/namespaces/default", headers=owner)
    assert protected.status_code == 422
    gone = await client.delete("/v1/human/namespaces/evil-trial-ns", headers=owner)
    assert gone.status_code == 200, gone.text
    names = [item["namespace"] for item in
             (await client.get("/v1/human/namespaces", headers=owner)).json()["items"]]
    assert "evil-trial-ns" not in names and "busy" in names
