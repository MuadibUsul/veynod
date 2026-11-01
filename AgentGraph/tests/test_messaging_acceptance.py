"""Acceptance tests for L3 messaging (blueprint-L3-messaging-open).

Covers the acceptance criteria that are in scope for the server-side messaging
system: A-01 (live push with body), A-02 (offline queue via inbox), A-05
(presence), A-06 (address uniqueness), A-08 (three-state receipts, read is
sender-only), A-09 (group + human approval), A-10 (task events still reach the
inbox), plus M-14 timeout escalation.

A-03 same-zone direct messaging needs no confirmation by construction (a
conversation is created on first send); cross-zone federation is a separate
task (cross-org-federation-protocol). A-04 first-connection full-channel
handshake belongs to L1/L2.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery"
ADMIN = {"Authorization": "Bearer test-admin"}


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://", 1)


async def _mcp(client, headers, tool, args):
    common = {"Content-Type": "application/json",
              "Accept": "application/json, text/event-stream"}
    await client.post("/mcp/", headers={**headers, **common}, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}},
    })
    call = await client.post("/mcp/", headers={**headers, **common}, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    })
    assert call.status_code == 200, call.text
    result = call.json()["result"]
    if result.get("isError"):
        raise RuntimeError(result.get("structuredContent") or result)
    return json.loads(result["content"][0]["text"])


async def _agent(client, ws_id, sig, *, agent_id=None, display_name=None):
    minted = await client.post(
        f"/v1/admin/workspaces/{ws_id}/agent-tokens", headers=ADMIN,
        json={"agent_signature": sig, "role_scope": "EXECUTOR",
              "allowed_namespaces": ["*"]})
    assert minted.status_code in (200, 201), minted.text
    headers = {"Authorization": f"Bearer {minted.json()['api_key']}"}
    body = {"capabilities": ["testing"]}
    if agent_id:
        body["agent_id"] = agent_id
    if display_name:
        body["display_name"] = display_name
    registered = await client.post("/v1/agents/register", headers=headers, json=body)
    assert registered.status_code == 201, registered.text
    return headers, registered.json()["agent_id"]


async def _human_owner(client, ws_id):
    email = f"owner-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(f"/v1/admin/workspaces/{ws_id}/human-users", headers=ADMIN,
                      json={"email": email, "display_name": "Owner", "role": "OWNER",
                            "password": PASSWORD})
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {login.json()['token']}"}


async def _read_stream(client, headers, want_event, timeout=15):
    frames = []
    async with client.stream("GET", "/v1/inbox/stream", headers=headers) as resp:
        assert resp.status_code == 200
        event = "message"
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
                if event == want_event:
                    frames.append(json.loads(data))
                    return frames
    return frames


async def test_a01_a02_a05_a08_live_delivery_and_presence(client, admin_headers, workspace):
    ws_id = workspace["workspace"]["id"]
    ha, a = await _agent(client, ws_id, "acc_a")
    hb, b = await _agent(client, ws_id, "acc_b")

    # A-01: B online, live push carries the body within seconds.
    frames_q: asyncio.Queue = asyncio.Queue()

    async def reader():
        async with client.stream("GET", "/v1/inbox/stream", headers=hb) as resp:
            assert resp.status_code == 200
            event = "message"
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:") and event == "message":
                    await frames_q.put(json.loads(line[5:].strip()))

    stream_task = asyncio.create_task(reader())
    await asyncio.sleep(0.5)
    started = time.monotonic()
    sent = await _mcp(client, ha, "send_message", {"body": "live hello", "peer_ref": f"agent:{b}"})
    frame = await asyncio.wait_for(frames_q.get(), timeout=15)
    assert frame["message"]["body"] == "live hello"
    assert time.monotonic() - started < 5

    # A-05: B is online in A's view while the stream is held.
    convs = await _mcp(client, ha, "list_conversations", {})
    conv = next(c for c in convs["items"] if c["conv_key"] == sent["conv_key"])
    member_b = next(m for m in conv["members"] if m["ref"] == f"agent:{b}")
    assert member_b["online"] is True
    stream_task.cancel()

    # A-08: receipts; read is sender-only.
    before = await _mcp(client, ha, "get_message_receipts",
                        {"conv_key": sent["conv_key"], "seq": sent["seq"]})
    rec = before["recipients"][0]
    assert rec["delivered"]["at"] is not None
    assert "read" in rec and rec["read"]["at"] is None

    # A-02: offline queue — A sends while B has no stream; B reads it back later.
    offline = await _mcp(client, ha, "send_message",
                         {"body": "offline hello", "conv_key": sent["conv_key"]})
    msgs = await _mcp(client, hb, "list_messages", {"conv_key": sent["conv_key"]})
    assert any(m["body"] == "offline hello" for m in msgs["items"])

    # mark read, then read is visible to sender only
    await _mcp(client, hb, "mark_read", {"conv_key": sent["conv_key"], "up_to_seq": offline["seq"]})
    after = await _mcp(client, ha, "get_message_receipts",
                       {"conv_key": sent["conv_key"], "seq": offline["seq"]})
    assert after["recipients"][0]["read"]["at"] is not None
    # non-sender (B) does not see the read field
    b_view = await _mcp(client, hb, "get_message_receipts",
                        {"conv_key": sent["conv_key"], "seq": offline["seq"]})
    assert "read" not in b_view["recipients"][0]


async def test_a06_address_uniqueness(client, admin_headers, workspace):
    ws_id = workspace["workspace"]["id"]
    ha, a = await _agent(client, ws_id, "addr_a", agent_id="same-address-1",
                         display_name="Same Name")
    hb, b = await _agent(client, ws_id, "addr_b", agent_id="same-address-2",
                         display_name="Same Name")
    assert a != b
    # Same display name, distinct addresses: message to one does not cross over.
    sent = await _mcp(client, ha, "send_message", {"body": "to addr-2", "peer_ref": f"agent:{b}"})
    assert sent["recipients"] == [f"agent:{b}"]
    msgs_b = await _mcp(client, hb, "list_messages", {"conv_key": sent["conv_key"]})
    assert any(m["body"] == "to addr-2" for m in msgs_b["items"])


async def test_a09_group_and_human_approval(client, admin_headers, workspace):
    ws_id = workspace["workspace"]["id"]
    human = await _human_owner(client, ws_id)
    ha, a = await _agent(client, ws_id, "grp_a")
    hb, b = await _agent(client, ws_id, "grp_b")
    hc, c = await _agent(client, ws_id, "grp_c")

    g = await _mcp(client, ha, "create_group",
                   {"title": "team", "member_refs": [f"agent:{b}", f"agent:{c}"]})
    assert g["status"] == "pending"
    conv_key = g["conv_key"]

    with pytest.raises(RuntimeError):
        await _mcp(client, ha, "send_message", {"body": "early", "conv_key": conv_key})

    appr = await client.post(f"/v1/human/groups/{conv_key}/approve", headers=human)
    assert appr.status_code == 200, appr.text
    assert appr.json()["status"] == "active"

    await _mcp(client, ha, "send_message", {"body": "hi team", "conv_key": conv_key})
    msgs = await _mcp(client, hb, "list_messages", {"conv_key": conv_key})
    assert any(m["body"] == "hi team" for m in msgs["items"])


async def test_a10_and_m14_timeouts(client, admin_headers, workspace):
    ws_id = workspace["workspace"]["id"]
    ha, a = await _agent(client, ws_id, "to_a")
    hb, b = await _agent(client, ws_id, "to_b")

    # A-10: task events still reach the inbox (assignment event).
    task = await client.post("/v1/tasks", headers=ha, json={
        "title": "still in inbox", "assignee": f"agent:{a}", "namespace": "default"})
    assert task.status_code == 201, task.text
    inbox = await _mcp(client, ha, "list_inbox", {})
    assert any(i.get("kind") == "task.assigned" for i in inbox.get("items", []))

    # M-14: unread message escalates to the sender after the threshold.
    await _mcp(client, ha, "send_message", {"body": "read me", "peer_ref": f"agent:{b}"})
    dsn = _dsn()
    conn = await asyncpg.connect(dsn)
    await conn.execute(
        "UPDATE messages SET created_at = NOW() - INTERVAL '31 minutes' "
        "WHERE workspace_id = $1::uuid", ws_id)
    await conn.close()
    await asyncio.sleep(8)  # reaper interval
    inbox = await _mcp(client, ha, "list_inbox", {})
    assert any(i.get("kind") == "message.unread" for i in inbox.get("items", []))

    # M-14: assigned-but-unclaimed task is opened for reassignment.
    stale = await client.post("/v1/tasks", headers=ha, json={
        "title": "stale assignment", "assignee": f"agent:{a}", "namespace": "default"})
    stale_key = stale.json()["task_key"]
    conn = await asyncpg.connect(dsn)
    await conn.execute(
        "UPDATE collab_tasks SET updated_at = NOW() - INTERVAL '3 hours' "
        "WHERE task_key = $1 AND workspace_id = $2::uuid", stale_key, ws_id)
    await conn.close()
    await asyncio.sleep(8)
    detail = await _mcp(client, ha, "list_tasks", {"task_key": stale_key, "detail": "full"})
    assert detail["items"][0].get("assignee") is None
