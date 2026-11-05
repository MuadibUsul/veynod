"""Two independent deployments (federation F-11, docs/federation/ICD_I_I2I.md).

F-021: two servers, two databases, two key-encryption keys. They share no
database and no privileged connection; signed envelopes over HTTP and public
identity records are all that crosses. A connection, a chat with receipts and
a full delegation run between them, and an outage is caught up afterwards.

F-022: a deployment that lists no peer hosts sends nothing out, and its own
federation between local workspaces keeps working.
"""

from __future__ import annotations

import asyncio
import base64
import os
import threading
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import asyncpg
import httpx
import pytest

from tests.conftest import run_server
from tests.test_federation_api import ADMIN, _refused, _workspace

pytestmark = pytest.mark.integration


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _database(suffix: str) -> str:
    base = os.environ["TEST_DATABASE_URL"]
    root, name = base.rsplit("/", 1)
    target = f"{name}_{suffix}"
    conn = await asyncpg.connect(_dsn(f"{root}/postgres"))
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", target):
            await conn.execute(f'CREATE DATABASE "{target}"')
    finally:
        await conn.close()
    return f"{root}/{target}"


def _env(database: str, peers: str) -> dict[str, str]:
    return {
        "DATABASE_URL": database,
        "FEDERATION_ENABLED": "true",
        "FEDERATION_KEK": base64.b64encode(os.urandom(32)).decode(),
        "FEDERATION_PEER_HOSTS": peers,
        "FEDERATION_DELIVERY_INTERVAL_SECONDS": "1",
    }


@pytest.fixture(scope="module")
async def deployments(api_base_url) -> AsyncIterator[dict]:
    del api_base_url
    db_a, db_b = await _database("i2i_a"), await _database("i2i_b")
    async with (
        run_server(_env(db_a, "127.0.0.1"), ".server-i2i-a.log") as url_a,
        run_server(_env(db_b, "127.0.0.1"), ".server-i2i-b.log") as url_b,
    ):
        async with (
            httpx.AsyncClient(base_url=url_a, timeout=60) as a,
            httpx.AsyncClient(base_url=url_b, timeout=60) as b,
        ):
            yield {"a": a, "b": b, "url_a": url_a, "url_b": url_b, "db_a": db_a, "db_b": db_b}


async def _enabled(client: httpx.AsyncClient, name: str) -> dict:
    ws = await _workspace(client, name)
    made = await client.post("/v1/federation/enable", headers=ws["owner"])
    assert made.status_code == 200, made.text
    ws["fid"] = made.json()["identity"]["federation_id"]
    return ws


def _terms() -> dict:
    return {
        "objective": "Translate the onboarding guide into Spanish.",
        "acceptance_criteria": ["Every section translated"],
        "input_summary": "A 12-page public onboarding guide.",
        "data_classification": "public",
        "allowed_use": "Only for this task.",
        "deadline": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        "budget_cap": None,
        "result_contract": "One Markdown file.",
        "cancellation_policy": "drain",
        "external_side_effects": False,
    }


async def _until(check, seconds: float = 20.0):
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        value = await check()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.5)


async def test_two_deployments_work_together_with_nothing_shared_but_envelopes(deployments):
    """F-021."""
    a_http, b_http = deployments["a"], deployments["b"]
    a = await _enabled(a_http, "i2i-a")
    b = await _enabled(b_http, "i2i-b")

    # Each deployment knows nothing about the other until an operator names it.
    unknown = await a_http.post(
        "/v1/federation/connections",
        headers=a["owner"],
        json={
            "peer_workspace": b["fid"],
            "outbound_scopes": ["chat"],
            "inbound_scopes": ["chat"],
            "reason": "cross-deployment pilot",
        },
    )
    assert unknown.status_code in (404, 422), unknown.text
    for http, peer, url in ((a_http, b, deployments["url_b"]), (b_http, a, deployments["url_a"])):
        registered = await http.post(
            "/v1/admin/federation/peers",
            headers=ADMIN,
            json={"federation_id": peer["fid"], "base_url": url, "operator": "Ana"},
        )
        assert registered.status_code == 201, registered.text

    asked = await a_http.post(
        "/v1/federation/connections",
        headers=a["owner"],
        json={
            "peer_workspace": b["fid"],
            "outbound_scopes": ["chat", "delegate"],
            "inbound_scopes": ["chat"],
            "reason": "cross-deployment pilot",
        },
    )
    assert asked.status_code in (200, 201), asked.text
    cid = asked.json()["connection"]["connection_id"]
    approved = await b_http.post(f"/v1/federation/connections/{cid}/approve", headers=b["owner"])
    assert approved.status_code == 200, approved.text
    mine = (await a_http.get("/v1/federation/connections", headers=a["owner"])).json()["items"]
    assert mine[0]["state"] == "ACTIVE"

    sent = await a_http.post(
        "/v1/federation/messages",
        headers=a["key"],
        json={"peer_workspace": b["fid"], "text": "Hello from another deployment."},
    )
    assert sent.status_code == 201 and sent.json()["state"] == "received", sent.text
    inbox = (await b_http.get("/v1/federation/messages?direction=in", headers=b["key"])).json()
    assert inbox["items"][0]["untrusted_content"] == "Hello from another deployment."

    offered = await a_http.post(
        "/v1/federation/delegations",
        headers=a["owner"],
        json={"peer_workspace": b["fid"], "terms": _terms()},
    )
    assert offered.status_code in (200, 201), offered.text
    job = offered.json()["delegation"]["federation_job_id"]
    accepted = await b_http.post(f"/v1/federation/delegations/{job}/accept", headers=b["owner"])
    assert accepted.status_code == 200, accepted.text
    task_key = accepted.json()["delegation"]["local_task_key"]
    assert (await b_http.get(f"/v1/tasks/{task_key}", headers=b["key"])).status_code == 200
    submitted = await b_http.post(
        f"/v1/federation/delegations/{job}/submit",
        headers=b["key"],
        json={
            "result": {
                "summary": "Translated.",
                "artifacts": [{"name": "guia.md", "content": "# Guía"}],
            }
        },
    )
    assert submitted.status_code == 200, submitted.text
    taken = await a_http.post(f"/v1/federation/delegations/{job}/accept_result", headers=a["owner"])
    assert taken.status_code == 200, taken.text

    async def b_sees_it_accepted():
        view = await b_http.get(f"/v1/federation/delegations/{job}", headers=b["owner"])
        return view.json()["delegation"]["state"] == "ACCEPTED_RESULT"

    await _until(b_sees_it_accepted)

    # Nothing shared: each database holds the other side only as public keys.
    for db, peer_fid, own_ws in (
        (deployments["db_a"], b["fid"], a["id"]),
        (deployments["db_b"], a["fid"], b["id"]),
    ):
        conn = await asyncpg.connect(_dsn(db))
        try:
            hosted = await conn.fetchval(
                "SELECT hosted FROM federation_identities WHERE federation_id = $1", peer_fid
            )
            private = await conn.fetchval(
                "SELECT count(*) FROM federation_identity_keys WHERE federation_id = $1 "
                "AND private_key_sealed IS NOT NULL",
                peer_fid,
            )
            mapped = await conn.fetchval(
                "SELECT count(*) FROM federation_local_map WHERE federation_id = $1", peer_fid
            )
            other_ws = await conn.fetchval(
                "SELECT count(*) FROM workspaces WHERE id <> $1::uuid AND id::text IN ($2, $3)",
                own_ws,
                a["id"],
                b["id"],
            )
        finally:
            await conn.close()
        assert hosted is False and private == 0 and mapped == 0 and other_ws == 0


async def test_an_outage_between_deployments_is_caught_up(deployments):
    a_http, b_http = deployments["a"], deployments["b"]
    a = await _enabled(a_http, "out-a")
    b = await _enabled(b_http, "out-b")
    for http, peer, url in ((a_http, b, deployments["url_b"]), (b_http, a, deployments["url_a"])):
        await http.post(
            "/v1/admin/federation/peers",
            headers=ADMIN,
            json={"federation_id": peer["fid"], "base_url": url, "operator": "Ana"},
        )
    cid = (
        await a_http.post(
            "/v1/federation/connections",
            headers=a["owner"],
            json={
                "peer_workspace": b["fid"],
                "outbound_scopes": ["chat"],
                "inbound_scopes": ["chat"],
                "reason": "outage drill",
            },
        )
    ).json()["connection"]["connection_id"]
    await b_http.post(f"/v1/federation/connections/{cid}/approve", headers=b["owner"])

    conn = await asyncpg.connect(_dsn(deployments["db_a"]))
    try:
        # The network to B goes away: point A at a closed port.
        await conn.execute(
            "UPDATE federation_remote_endpoints SET base_url = 'http://127.0.0.1:9' "
            "WHERE federation_id = $1",
            b["fid"],
        )
        sent = await a_http.post(
            "/v1/federation/messages",
            headers=a["key"],
            json={"peer_workspace": b["fid"], "text": "Are you there?"},
        )
        assert sent.json()["state"] == "accepted_locally", sent.text
        assert sent.json()["delivery"]["last_error"] == "FED_E_UNREACHABLE"
        # The network comes back; the next retry is due now.
        await conn.execute(
            "UPDATE federation_remote_endpoints SET base_url = $2 WHERE federation_id = $1",
            b["fid"],
            deployments["url_b"],
        )
        await conn.execute(
            "UPDATE federation_delivery_attempts SET next_retry_at = NOW() WHERE result = 'retry'"
        )
    finally:
        await conn.close()
    message_id = sent.json()["message_id"]

    async def delivered():
        view = await a_http.get(f"/v1/federation/messages/{message_id}", headers=a["key"])
        return view.json()["state"] == "received"

    await _until(delivered)
    inbox = (await b_http.get("/v1/federation/messages?direction=in", headers=b["key"])).json()
    assert [m["message_id"] for m in inbox["items"]].count(message_id) == 1


async def test_forged_records_and_unknown_issuers_are_refused(deployments):
    a_http = deployments["a"]
    a = await _enabled(a_http, "trust-a")
    # An envelope from an identity nobody registered is refused at the inbox.
    stray = {
        "protocol": "veynod-federation",
        "protocol_version": "0.1",
        "type": "chat.message",
        "object_id": "fmsg_" + "a" * 26,
        "issuer": "fed:workspace:" + "b" * 26,
        "key_id": "sha256:" + "0" * 64,
        "audience": a["fid"],
        "issued_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-01-01T01:00:00Z",
        "nonce": "x" * 24,
        "payload": {},
        "payload_sha256": "0" * 64,
        "signature": "AA",
    }
    refused = await a_http.post("/v1/federation/inbox", json=stray)
    assert refused.status_code >= 400
    # Records served by a peer must derive from their key: a host serving
    # someone else's id under the wrong key is refused.
    bogus = await a_http.post(
        "/v1/admin/federation/peers",
        headers=ADMIN,
        json={
            "federation_id": "fed:workspace:" + "c" * 26,
            "base_url": deployments["url_b"],
            "operator": "Ana",
        },
    )
    assert bogus.status_code >= 400


class _Sink(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        _Sink.hits.append(self.path)
        self.send_response(200)
        self.end_headers()

    do_POST = do_GET

    def log_message(self, *args):  # quiet
        return


async def test_a_closed_network_sends_nothing_out_and_still_works(api_base_url):
    """F-022: no peer hosts listed: nothing leaves, local federation works."""
    del api_base_url
    sink = ThreadingHTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    sink_url = f"http://127.0.0.1:{sink.server_address[1]}"
    _Sink.hits.clear()
    db_c = await _database("i2i_c")
    try:
        async with run_server(_env(db_c, ""), ".server-i2i-c.log") as url_c:
            async with httpx.AsyncClient(base_url=url_c, timeout=60) as c_http:
                one = await _enabled(c_http, "closed-1")
                two = await _enabled(c_http, "closed-2")
                _refused(
                    await c_http.post(
                        "/v1/admin/federation/peers",
                        headers=ADMIN,
                        json={
                            "federation_id": "fed:workspace:" + "d" * 26,
                            "base_url": sink_url,
                            "operator": "Ana",
                        },
                    ),
                    403,
                    "federation_forbidden",
                    "FED_E_EGRESS_DISABLED",
                )
                cid = (
                    await c_http.post(
                        "/v1/federation/connections",
                        headers=one["owner"],
                        json={
                            "peer_workspace": two["fid"],
                            "outbound_scopes": ["chat"],
                            "inbound_scopes": ["chat"],
                            "reason": "inside the company",
                        },
                    )
                ).json()["connection"]["connection_id"]
                await c_http.post(f"/v1/federation/connections/{cid}/approve", headers=two["owner"])
                sent = await c_http.post(
                    "/v1/federation/messages",
                    headers=one["key"],
                    json={"peer_workspace": two["fid"], "text": f"internal {uuid.uuid4().hex}"},
                )
                assert sent.json()["state"] == "received", sent.text
                await asyncio.sleep(2)  # a few delivery-loop passes
    finally:
        sink.shutdown()
    assert _Sink.hits == []
