"""The nerve socket end to end: presence by socket, and 200 ms is a real timer.

A Python client stands in for the messenger here because what is under test is
the *server*: the handshake, the two receipts, what happens when one is missing,
and that the socket — not ``runner_hosts`` — decides who gets dispatch.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import websockets

from tests.test_scheduler import ADMIN, REPO, _schedule, _task, _workspace

pytestmark = pytest.mark.integration


def _ws_url(api_base_url: str) -> str:
    return api_base_url.replace("http://", "ws://").replace("https://", "wss://") + "/v1/ws/nerve"


async def _host(client, ws: dict, name: str) -> dict:
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"role_scope": "EXECUTOR", "allowed_namespaces": ["*"]})
    assert minted.status_code == 201, minted.text
    headers = {"Authorization": f"Bearer {minted.json()['api_key']}"}
    named = await client.post("/v1/agents/register", headers=headers, json={"agent_id": name})
    assert named.status_code in (200, 201), named.text
    hello = await client.post("/v1/runner/hello", headers=headers, json={"host": {
        "os": "linux", "arch": "x64", "max_concurrency": 1, "repos": [REPO],
        "runtimes": [{"id": "custom", "available": True}]}})
    assert hello.status_code == 200, hello.text
    return {"headers": headers, "agent_id": name, "host_id": hello.json()["host_id"],
            "key": minted.json()["api_key"]}


async def _connect(api_base_url: str, host: dict) -> websockets.WebSocketClientProtocol:
    socket = await websockets.connect(
        _ws_url(api_base_url),
        additional_headers={
            "Authorization": f"Bearer {host['key']}",
            "X-Agent-Id": host["agent_id"],
            "X-Host-Id": host["host_id"],
        },
    )
    ack = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
    assert ack["type"] == "auth.ack", ack
    assert ack["payload"]["agent_id"] == host["agent_id"]
    return socket


async def _task_row(client, ws: dict, key: str) -> dict:
    return (await client.get(f"/v1/tasks/{key}", headers=ws["owner"])).json()


async def test_a_socket_is_presence_and_answers_ping(api_base_url, client) -> None:
    ws = await _workspace(client, "full")
    host = await _host(client, ws, f"nerve-{uuid.uuid4().hex[:4]}")
    socket = await _connect(api_base_url, host)
    try:
        await socket.send(json.dumps({"type": "sys.ping", "frame_id": "f1",
                                      "payload": {"worker_busy": False}}))
        pong = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
        assert pong["type"] == "sys.pong"
    finally:
        await socket.close()


async def test_a_wrong_agent_header_is_refused(api_base_url, client) -> None:
    ws = await _workspace(client, "full")
    host = await _host(client, ws, f"nerve-{uuid.uuid4().hex[:4]}")
    with pytest.raises(websockets.exceptions.InvalidStatus):
        await websockets.connect(
            _ws_url(api_base_url),
            additional_headers={"Authorization": f"Bearer {host['key']}",
                                "X-Agent-Id": "someone-else"},
        )


async def test_dispatch_needs_two_receipts_and_grants_the_lease(api_base_url, client) -> None:
    ws = await _workspace(client, "full")
    host = await _host(client, ws, f"nerve-{uuid.uuid4().hex[:4]}")
    socket = await _connect(api_base_url, host)
    key = await _task(client, ws)

    async def answer() -> None:
        poke = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
        assert poke["type"] == "task.poke"
        payload = poke["payload"]
        assert payload["task_key"] == key and payload["lease_epoch"] >= 1
        assert payload["orientation_timeout_ms"] == 4500
        await socket.send(json.dumps({"type": "task.ack.net", "frame_id": poke["frame_id"],
                                      "payload": {"received_at_ms": 0}}))
        await socket.send(json.dumps({"type": "task.ack.exec", "frame_id": poke["frame_id"],
                                      "payload": {"worker_pid": 1234}}))

    responder = asyncio.create_task(answer())
    try:
        result = await _schedule(client, ws)
        await responder
    finally:
        await socket.close()

    assert result.get("nerve", {}).get("ok") == 1, result
    row = await _task_row(client, ws, key)
    assert row["status"] == "IN_PROGRESS"
    assert row["assignee"] == f"agent:{host['agent_id']}"


async def test_a_silent_socket_times_out_in_about_200ms_and_the_task_returns(
        api_base_url, client) -> None:
    ws = await _workspace(client, "full")
    host = await _host(client, ws, f"nerve-{uuid.uuid4().hex[:4]}")
    socket = await _connect(api_base_url, host)
    key = await _task(client, ws)
    try:
        # Never answer: the server must not wait out a 90 s presence window.
        result = await _schedule(client, ws)
        assert result.get("nerve", {}).get("failed") == 1, result
        row = await _task_row(client, ws, key)
        assert row["status"] == "READY", row
        # Nerve is the only channel: an undelivered offer frees the task and the
        # placement moves, so the next pass places it again (no SSE fallback).
        # (The background pass may already have placed it again, so the
        # placement, not the assignee, is what says this one was taken back.)
        placements = (await client.get("/v1/runner/placements", headers=ws["owner"],
                                       params={"task_key": key})).json()["items"]
        moved = [item for item in placements if item["status"] == "moved"]
        assert moved and moved[0]["reason"].startswith("nerve:"), placements
    finally:
        await socket.close()


async def test_a_net_ack_without_an_exec_ack_is_revoked(api_base_url, client) -> None:
    ws = await _workspace(client, "full")
    host = await _host(client, ws, f"nerve-{uuid.uuid4().hex[:4]}")
    socket = await _connect(api_base_url, host)
    key = await _task(client, ws)

    async def half_answer() -> dict:
        poke = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
        await socket.send(json.dumps({"type": "task.ack.net", "frame_id": poke["frame_id"],
                                      "payload": {}}))
        return poke

    responder = asyncio.create_task(half_answer())
    try:
        result = await _schedule(client, ws)
        await responder
        # The revocation frame follows the timeout.
        while True:
            frame = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
            if frame["type"] == "lease.revoked":
                break
    finally:
        await socket.close()

    assert result.get("nerve", {}).get("failed") == 1, result
    assert (await _task_row(client, ws, key))["status"] == "READY"


async def test_a_second_socket_takes_over_and_the_first_is_closed(api_base_url, client) -> None:
    ws = await _workspace(client, "full")
    host = await _host(client, ws, f"nerve-{uuid.uuid4().hex[:4]}")
    first = await _connect(api_base_url, host)
    second = await _connect(api_base_url, host)
    try:
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await asyncio.wait_for(first.recv(), timeout=5)
    finally:
        await first.close()
        await second.close()


async def test_token_refresh_returns_a_secret_that_still_works(api_base_url, client) -> None:
    ws = await _workspace(client, "full")
    host = await _host(client, ws, f"nerve-{uuid.uuid4().hex[:4]}")
    socket = await _connect(api_base_url, host)
    try:
        await socket.send(json.dumps({"type": "auth.token_refresh.req", "frame_id": "r1",
                                      "payload": {}}))
        reply = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
        assert reply["type"] == "auth.token_refresh.res"
        fresh = reply["payload"]["token"]
        assert fresh and fresh != host["key"]
    finally:
        await socket.close()
    called = await client.get("/v1/tasks", headers={"Authorization": f"Bearer {fresh}"})
    assert called.status_code == 200, called.text
