"""Realtime pokes (TASK-72A59BD9): presence, push latency, receipts, resume, wake."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.integration


async def _agent(client, admin_headers, workspace, namespaces=None):
    signature = f"rt_{uuid.uuid4().hex[:6]}"
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": signature, "role_scope": "EXECUTOR",
              "allowed_namespaces": namespaces or ["*"]})
    assert response.status_code in (200, 201), response.text
    headers = {"Authorization": f"Bearer {response.json()['api_key']}"}
    registered = await client.post("/v1/agents/register", headers=headers,
                                   json={"capabilities": ["testing"]})
    assert registered.status_code == 201, registered.text
    return headers, signature, registered.json()["agent_id"]


async def _read_pokes(base_url, headers, want: int, *, last_event_id=None, timeout=15):
    """Open the inbox stream, collect `want` poke frames, then close it."""
    frames: list[dict] = []
    stream_headers = dict(headers)
    if last_event_id is not None:
        stream_headers["Last-Event-ID"] = str(last_event_id)
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as http:
        async with http.stream("GET", "/v1/inbox/stream", headers=stream_headers) as response:
            assert response.status_code == 200
            event, data, ident = "message", "", None
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                elif line.startswith("id:"):
                    ident = line[3:].strip()
                elif line == "":
                    if event == "poke":
                        frames.append({**json.loads(data), "_id": int(ident)})
                        if len(frames) >= want:
                            return frames
                    event, data, ident = "message", "", None
    return frames


async def test_push_presence_and_receipts(client, admin_headers, workspace, api_base_url):
    owner = workspace["headers"]
    agent, signature, agent_id = await _agent(client, admin_headers, workspace)

    async def assign_soon(title):
        await asyncio.sleep(1.0)
        started = time.monotonic()
        made = await client.post("/v1/tasks", headers=owner, json={
            "title": title, "assignee": f"agent:{agent_id}", "namespace": "default"})
        assert made.status_code == 201, made.text
        return made.json()["task_key"], started

    async def watch_presence():
        await asyncio.sleep(0.6)
        return (await client.get(f"/v1/agents/{agent_id}", headers=owner)).json()

    # T-1: online agent gets the assignment pushed, well under 5 s.
    reader = asyncio.create_task(_read_pokes(api_base_url, agent, 1))
    presence = asyncio.create_task(watch_presence())
    task_key, started = await assign_soon("pushed")
    frames = await asyncio.wait_for(reader, timeout=15)
    elapsed = time.monotonic() - started
    assert frames[0]["task_key"] == task_key and frames[0]["replay"] is False
    assert elapsed < 5, elapsed
    stamps = frames[0]["timestamps"]
    assert {"sent_at", "stored_at", "pushed_at", "platform_ms"} <= set(stamps)
    # R-1: presence is real liveness while the stream is open.
    entry = await presence
    assert entry["presence"]["online"] is True

    # T-4: three receipts, surfaced only once delivered.
    receipts = (await client.get("/v1/pokes/receipts", headers=owner,
                                 params={"task_key": task_key})).json()
    mine = next(item for item in receipts["recipients"] if agent_id in item["recipient"]
                or signature in item["recipient"])
    assert mine["surfaced"] is True and "stream" in mine["delivered"]["via"]
    assert mine["read"] is None
    acked = await client.post("/v1/inbox/ack", headers=agent,
                              json={"up_to_event_seq": receipts["event_seq"]})
    assert acked.status_code == 200, acked.text
    after = (await client.get("/v1/pokes/receipts", headers=owner,
                              params={"event_seq": receipts["event_seq"]})).json()
    mine = next(item for item in after["recipients"] if item["recipient"] == mine["recipient"])
    assert mine["read"]["at"] is not None

    # Closed stream: offline again.
    await asyncio.sleep(0.5)
    entry = (await client.get(f"/v1/agents/{agent_id}", headers=owner)).json()
    assert entry["presence"]["online"] is False


async def test_offline_catch_up_and_resume(client, admin_headers, workspace, api_base_url):
    owner = workspace["headers"]
    agent, _, agent_id = await _agent(client, admin_headers, workspace)
    keys = []
    for index in range(3):
        made = await client.post("/v1/tasks", headers=owner, json={
            "title": f"queued {index}", "assignee": f"agent:{agent_id}",
            "namespace": "default"})
        keys.append(made.json()["task_key"])

    # T-2: coming online replays everything unacked, in seq order.
    started = time.monotonic()
    frames = await _read_pokes(api_base_url, agent, 3)
    assert time.monotonic() - started < 5
    assert [frame["task_key"] for frame in frames] == keys
    assert all(frame["replay"] for frame in frames)
    seqs = [frame["_id"] for frame in frames]
    assert seqs == sorted(seqs)

    # T-6: resuming after the second one yields only the third: no loss, no repeat.
    resumed = await _read_pokes(api_base_url, agent, 1, last_event_id=seqs[1])
    assert [frame["_id"] for frame in resumed] == [seqs[2]]


async def test_unknown_mentions_are_refused(client, workspace):
    # T-5: an explicit mention of nobody is refused with suggestions.
    refused = await client.post("/v1/tasks", headers=workspace["headers"], json={
        "title": "hello", "namespace": "default", "mentions": ["agent:nobody-here"]})
    assert refused.status_code == 422
    error = refused.json()["error"]
    assert error["code"] == "unknown_principal"
    assert "did_you_mean" in error["details"]


class _Sink:
    """A local wake endpoint: answers `status`, keeps what it received."""

    def __init__(self, status: int) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        sink = self
        self.hits: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                body = self.rfile.read(int(self.headers.get("content-length", "0")))
                sink.hits.append({"headers": dict(self.headers), "raw": body,
                                  "body": json.loads(body)})
                self.send_response(status)
                self.end_headers()

            def log_message(self, format: str, *args) -> None:  # noqa: A002
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/wake"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


async def test_offline_agent_is_woken_and_the_outcome_is_recorded(
        admin_headers, workspace, api_base_url):
    import hashlib
    import hmac

    import asyncpg

    from tests.test_agent_onboarding import _db

    good, bad = _Sink(200), _Sink(500)
    try:
        async with httpx.AsyncClient(base_url=api_base_url, timeout=30) as live:
            owner = workspace["headers"]
            agent, _, agent_id = await _agent(live, admin_headers, workspace)
            other, _, other_id = await _agent(live, admin_headers, workspace)
            wake = await live.put("/v1/agents/me/wake", headers=agent, json={"url": good.url})
            assert wake.status_code == 200, wake.text
            secret = wake.json()["secret"]
            assert (await live.put("/v1/agents/me/wake", headers=other,
                                   json={"url": bad.url})).status_code == 200

            # T-3: offline (no stream): the wake URL is called, signed.
            made = await live.post("/v1/tasks", headers=owner, json={
                "title": "wake up", "assignee": f"agent:{agent_id}", "namespace": "default"})
            task_key = made.json()["task_key"]
            for _ in range(40):
                if good.hits:
                    break
                await asyncio.sleep(0.5)
            assert good.hits, "the wake webhook was not called"
            hit = good.hits[0]
            assert hit["body"]["task_key"] == task_key
            stamp = hit["headers"]["X-AgentGraph-Timestamp"]
            expected = hmac.new(secret.encode(), f"{stamp}.".encode() + hit["raw"],
                                hashlib.sha256).hexdigest()
            assert hit["headers"]["X-AgentGraph-Signature"] == expected

            receipts = None
            for _ in range(20):
                receipts = (await live.get("/v1/pokes/receipts", headers=owner,
                                           params={"task_key": task_key})).json()
                vias = [via for item in receipts["recipients"]
                        for via in item["delivered"]["via"]]
                if "webhook" in vias:
                    break
                await asyncio.sleep(0.5)
            assert "webhook" in vias, receipts

            # A wake that keeps failing ends DEAD, and says why: never silent.
            failed = await live.post("/v1/tasks", headers=owner, json={
                "title": "nobody home", "assignee": f"agent:{other_id}",
                "namespace": "default"})
            failed_key = failed.json()["task_key"]
            reason = None
            for _ in range(60):
                conn = await asyncpg.connect(_db())
                try:
                    await conn.execute("UPDATE webhook_deliveries SET next_attempt_at = NOW() "
                                       "WHERE status = 'PENDING'")
                finally:
                    await conn.close()
                receipts = (await live.get("/v1/pokes/receipts", headers=owner,
                                           params={"task_key": failed_key})).json()
                channels = [channel for item in receipts["recipients"]
                            for channel in item["channels"] if channel["channel"] == "webhook"]
                if channels and channels[0]["status"] == "failed":
                    reason = channels[0]["detail"]
                    break
                await asyncio.sleep(1)
            assert reason and "DEAD" in reason, receipts
            assert len(bad.hits) == 4
    finally:
        good.close()
        bad.close()
