"""Messaging (blueprint-L3-messaging-open): 1:1 conversations, permanent
messages, stream-first delivery with inbox fallback, and sent/delivered/read
receipts driven by an explicit per-member read watermark (M-07).

These are the A-01/A-02/A-07/A-08 vertical-slice assertions: two agents
exchange a message, the recipient reads it back, and the sender sees the
receipt progress sent -> delivered -> read.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.integration


async def _agent(client, admin_headers, workspace, signature):
    response = await client.post(
        f"/v1/admin/workspaces/{workspace['workspace']['id']}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": signature, "role_scope": "EXECUTOR",
              "allowed_namespaces": ["*"]},
    )
    assert response.status_code in (200, 201), response.text
    headers = {"Authorization": f"Bearer {response.json()['api_key']}"}
    registered = await client.post("/v1/agents/register", headers=headers,
                                   json={"capabilities": ["testing"]})
    assert registered.status_code == 201, registered.text
    return headers, registered.json()["agent_id"]


async def _mcp(client, headers, tool, args):
    common = {"Content-Type": "application/json",
              "Accept": "application/json, text/event-stream"}
    init = await client.post("/mcp/", headers={**headers, **common}, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}},
    })
    assert init.status_code == 200, init.text
    call = await client.post("/mcp/", headers={**headers, **common}, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    })
    assert call.status_code == 200, call.text
    result = call.json()["result"]
    if result.get("isError"):
        raise AssertionError(result)
    return json.loads(result["content"][0]["text"])


async def test_direct_message_receipts(client, admin_headers, workspace):
    a_headers, a_id = await _agent(client, admin_headers, workspace, "msg_a")
    b_headers, b_id = await _agent(client, admin_headers, workspace, "msg_b")

    # A sends to B; the direct conversation is created on first use.
    sent = await _mcp(client, a_headers, "send_message",
                      {"body": "hello B", "peer_ref": f"agent:{b_id}"})
    conv_key, seq = sent["conv_key"], sent["seq"]
    assert sent["sender"] == f"agent:{a_id}"
    assert sent["recipients"] == [f"agent:{b_id}"]

    # B sees the conversation and the message (permanent storage).
    convs = await _mcp(client, b_headers, "list_conversations", {})
    assert any(c["conv_key"] == conv_key for c in convs["items"])
    msgs = await _mcp(client, b_headers, "list_messages", {"conv_key": conv_key})
    assert [m["body"] for m in msgs["items"]] == ["hello B"]

    # Receipts: sent + delivered, not yet read.
    before = await _mcp(client, a_headers, "get_message_receipts",
                        {"conv_key": conv_key, "seq": seq})
    recipient = before["recipients"][0]
    assert recipient["ref"] == f"agent:{b_id}"
    assert recipient["delivered"]["at"] is not None
    assert recipient["read"]["at"] is None

    # B marks read; the watermark moves forward (M-07 explicit mark_read).
    marked = await _mcp(client, b_headers, "mark_read",
                        {"conv_key": conv_key, "up_to_seq": seq})
    assert marked["last_read_seq"] == seq

    after = await _mcp(client, a_headers, "get_message_receipts",
                       {"conv_key": conv_key, "seq": seq})
    recipient = after["recipients"][0]
    assert recipient["read"]["at"] is not None
    assert recipient["read"]["via"] == "mark_read"
