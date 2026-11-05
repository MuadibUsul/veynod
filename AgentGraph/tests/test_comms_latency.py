"""The core promise: one agent's message reaches another online agent within
seconds (owner requirement, 2026-11-05).

Agent B holds its inbox stream open; agent A sends ROUNDS messages one after
another. Each is timed from the moment A's send call starts to the moment B's
stream yields that message. The numbers are printed (run with ``-s``) and the
test fails if the slowest message takes more than MAX_SECONDS.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time

import pytest

from tests.test_messaging_acceptance import _agent, _mcp

pytestmark = pytest.mark.integration

ROUNDS = 20
MAX_SECONDS = 2.0


async def test_messages_reach_an_online_agent_within_seconds(client, workspace):
    ws_id = workspace["workspace"]["id"]
    ha, _a = await _agent(client, ws_id, "lat_a")
    hb, b = await _agent(client, ws_id, "lat_b")
    arrived: dict[str, float] = {}
    ready = asyncio.Event()

    async def reader() -> None:
        async with client.stream("GET", "/v1/inbox/stream", headers=hb) as resp:
            assert resp.status_code == 200
            event = "message"
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                    if event == "connected":
                        ready.set()
                elif line.startswith("data:") and event == "message":
                    body = (json.loads(line[5:].strip()).get("message") or {}).get("body")
                    if body:
                        arrived[body] = time.monotonic()

    stream = asyncio.create_task(reader())
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
        latencies = []
        for i in range(ROUNDS):
            body = f"ping {i}"
            started = time.monotonic()
            await _mcp(client, ha, "send_message", {"body": body, "peer_ref": f"agent:{b}"})
            while body not in arrived:
                assert time.monotonic() - started < 10, f"{body} never arrived"
                await asyncio.sleep(0.005)
            latencies.append(arrived[body] - started)
    finally:
        stream.cancel()

    ordered = sorted(latencies)
    p50 = statistics.median(ordered)
    p95 = ordered[int(0.95 * (len(ordered) - 1))]
    print(
        f"\nmessage latency over {ROUNDS} sends: p50 {p50 * 1000:.0f} ms, "
        f"p95 {p95 * 1000:.0f} ms, max {ordered[-1] * 1000:.0f} ms"
    )
    assert ordered[-1] < MAX_SECONDS, ordered
