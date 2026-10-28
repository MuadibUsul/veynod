"""Write path under bursts: database admission and lookups a batch shares."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app import batch_memo, concurrency
from app.concurrency import DatabaseAdmissionMiddleware
from app.services.onboarding import readme_key


async def test_batch_memo_shares_only_inside_a_batch():
    calls: list[int] = []

    async def compute() -> int:
        calls.append(1)
        return len(calls)

    assert await batch_memo.shared("key", compute) == 1
    assert await batch_memo.shared("key", compute) == 2  # no batch: every call runs
    with batch_memo.batch_scope():
        assert await batch_memo.shared("key", compute) == 3
        assert await batch_memo.shared("key", compute) == 3
        batch_memo.remember("key", "recorded")
        assert await batch_memo.shared("key", compute) == "recorded"
        batch_memo.forget_all()
        assert await batch_memo.shared("key", compute) == 4
    assert await batch_memo.shared("key", compute) == 5


def _scope(method: str, path: str) -> dict:
    return {"type": "http", "method": method, "path": path, "headers": []}


def test_only_short_rest_writes_wait_for_a_database_slot():
    admitted = DatabaseAdmissionMiddleware._admitted
    assert admitted(_scope("PUT", "/v1/documents/a"))
    assert admitted(_scope("POST", "/v1/documents/batch"))
    # Streams, long polls and reads never hold a slot.
    assert not admitted(_scope("GET", "/v1/events"))
    assert not admitted(_scope("GET", "/v1/admin/workspaces/w/stream"))
    assert not admitted(_scope("GET", "/v1/documents/a"))
    # MCP tool calls include poll_events waits; operator routes run backups;
    # resource acquires wait for a free slot.
    assert not admitted(_scope("POST", "/mcp"))
    assert not admitted(_scope("POST", "/v1/admin/backups/run"))
    assert not admitted(_scope("POST", "/v1/resources/acquire"))


async def test_writes_past_the_limit_queue_then_get_overloaded(monkeypatch):
    monkeypatch.setattr(concurrency.settings, "db_pool_timeout", 0.2)
    release = asyncio.Event()

    async def app(scope, receive, send):
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = DatabaseAdmissionMiddleware(app)
    middleware.slots = asyncio.Semaphore(1)

    async def call(method: str, path: str) -> list[dict]:
        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        await middleware(_scope(method, path), None, send)
        return sent

    first = asyncio.create_task(call("PUT", "/v1/documents/a"))
    await asyncio.sleep(0.05)
    shed = await call("PUT", "/v1/documents/b")
    assert shed[0]["status"] == 503
    assert (b"retry-after", b"1") in shed[0]["headers"]
    assert json.loads(shed[1]["body"])["error"]["code"] == "overloaded"
    # A read does not queue behind the write that holds the slot.
    read = asyncio.create_task(call("GET", "/v1/documents/a"))
    release.set()
    assert (await first)[0]["status"] == 200
    assert (await read)[0]["status"] == 200
    assert not middleware.slots.locked()


@pytest.mark.integration
async def test_failed_batch_item_does_not_leave_shared_lookups(client, admin_headers, workspace):
    wid = workspace["workspace"]["id"]
    policy = await client.post(
        f"/v1/admin/workspaces/{wid}/policies", headers=admin_headers,
        json={"name": "tiny", "resource_type": "document", "pattern": "tiny-*",
              "max_content_bytes": 4})
    assert policy.status_code == 201, policy.text
    namespace = f"memo{uuid.uuid4().hex[:6]}"
    response = await client.post("/v1/documents/batch", headers=workspace["headers"], json={
        "atomic": False,
        "items": [
            # Registers the new namespace and its readme, then fails the policy.
            {"doc_key": "tiny-1", "namespace": namespace, "content": "far too long",
             "expected_version": 0},
            {"doc_key": "fine-1", "namespace": namespace, "content": "fits",
             "expected_version": 0},
        ],
    })
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert [item["ok"] for item in results] == [False, True]
    assert results[0]["error"]["code"] == "policy_size_limit"
    # The failed item's savepoint undid its registration; the second item
    # registered the namespace again instead of trusting the first.
    listed = await client.get("/v1/namespaces", headers=workspace["headers"])
    assert namespace in {item["namespace"] for item in listed.json()["items"]}
    readme = await client.get(f"/v1/documents/{readme_key(namespace)}",
                              headers=workspace["headers"])
    assert readme.status_code == 200, readme.text
