"""Two-way, second-level collaboration between two agents (owner scenario,
2026-11-05).

Agent A gives task E to agent B. B hears it, tells A it has started, asks A a
question mid-way, delivers, and A sends the work back; B hears the rejection
and delivers again. Both agents hold their inbox streams open; every hop is
timed from the sender's call to the receiver's stream yielding it, and must
arrive within MAX_SECONDS. The hop times are printed (run with ``-s``).

This measures the platform's delivery. How long an agent then takes to think
and act is the agent's own time and is not part of it.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest

from tests.test_messaging_acceptance import _mcp

pytestmark = pytest.mark.integration

MAX_SECONDS = 2.0


async def _agent(client, admin_headers, ws_id, role):
    signature = f"rt_{role.lower()}_{uuid.uuid4().hex[:6]}"
    minted = await client.post(
        f"/v1/admin/workspaces/{ws_id}/agent-tokens",
        headers=admin_headers,
        json={"agent_signature": signature, "role_scope": role, "allowed_namespaces": ["*"]},
    )
    assert minted.status_code in (200, 201), minted.text
    headers = {"Authorization": f"Bearer {minted.json()['api_key']}"}
    registered = await client.post(
        "/v1/agents/register", headers=headers, json={"capabilities": ["testing"]}
    )
    assert registered.status_code == 201, registered.text
    return headers, registered.json()["agent_id"]


class Listener:
    """One agent's open inbox stream; every frame is kept with its arrival time."""

    def __init__(self, client, headers):
        self.client, self.headers = client, headers
        self.frames: list[tuple[float, str, dict]] = []
        self.ready = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        async with self.client.stream("GET", "/v1/inbox/stream", headers=self.headers) as resp:
            assert resp.status_code == 200
            event = "message"
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                    if event == "connected":
                        self.ready.set()
                elif line.startswith("data:") and event in ("poke", "message"):
                    self.frames.append((time.monotonic(), event, json.loads(line[5:].strip())))

    async def wait_for(self, started: float, match) -> tuple[float, dict]:
        while True:
            for at, event, body in self.frames:
                if at >= started and match(event, body):
                    return at - started, body
            assert time.monotonic() - started < 10, [
                (e, b.get("type"), b.get("task_key")) for _, e, b in self.frames
            ]
            await asyncio.sleep(0.005)


def _message(text):
    return lambda event, body: event == "message" and body["message"].get("body") == text


def _poke(task_key):
    return lambda event, body: event == "poke" and body.get("task_key") == task_key


async def test_two_agents_work_a_task_back_and_forth_within_seconds(
    client, admin_headers, workspace
):
    ws_id = workspace["workspace"]["id"]
    ha, a = await _agent(client, admin_headers, ws_id, "SUPER_ADMIN")
    hb, b = await _agent(client, admin_headers, ws_id, "EXECUTOR")
    la, lb = Listener(client, ha), Listener(client, hb)
    hops: list[tuple[str, float]] = []
    try:
        await asyncio.wait_for(asyncio.gather(la.ready.wait(), lb.ready.wait()), timeout=10)
        key = f"E-{uuid.uuid4().hex[:8].upper()}"

        async def say(sender, receiver: Listener, peer, text, label):
            started = time.monotonic()
            await _mcp(client, sender, "send_message", {"body": text, "peer_ref": f"agent:{peer}"})
            took, _ = await receiver.wait_for(started, _message(text))
            hops.append((label, took))

        # 1. A gives task E to B; B hears it.
        started = time.monotonic()
        made = await client.post(
            "/v1/tasks",
            headers=ha,
            json={
                "task_key": key,
                "title": "Task E",
                "assignee": f"agent:{b}",
                "assigned_role": "EXECUTOR",
            },
        )
        assert made.status_code == 201, made.text
        took, frame = await lb.wait_for(started, _poke(key))
        hops.append((f"A assigns E -> B hears {frame['kind']}", took))

        # 2. B takes it and tells A; 3. a question and its answer mid-way.
        claimed = await client.post(f"/v1/tasks/{key}/claim", headers=hb, json={})
        assert claimed.status_code == 200, claimed.text
        await say(hb, la, a, f"{key}: received, starting now", "B: started -> A hears it")
        await say(hb, la, a, f"{key}: the input has two totals, which one?", "B asks -> A hears")
        await say(ha, lb, b, f"{key}: use the audited total", "A answers -> B hears")

        # 4. B delivers; A hears the handoff.
        brief = {
            "summary": "done",
            "key_metrics": {},
            "unverified_hypotheses": [],
            "risk_flag": False,
        }
        started = time.monotonic()
        handed = await client.post(
            f"/v1/tasks/{key}/handoff",
            headers=hb,
            json={"next_status": "REVIEWING", "brief": brief},
        )
        assert handed.status_code == 200, handed.text
        took, frame = await la.wait_for(started, _poke(key))
        hops.append((f"B delivers -> A hears {frame['kind']}", took))

        # 5. A sends it back; B hears the rejection and why.
        started = time.monotonic()
        reviewed = await client.post(
            f"/v1/tasks/{key}/review",
            headers=ha,
            json={"decision": "reject", "feedback": "Totals are off by one row."},
        )
        assert reviewed.status_code == 200, reviewed.text
        took, frame = await lb.wait_for(started, _poke(key))
        hops.append((f"A sends back -> B hears {frame['kind']}", took))

        # 6. B takes it up again and delivers again; A hears it.
        reclaimed = await client.post(f"/v1/tasks/{key}/claim", headers=hb, json={})
        assert reclaimed.status_code == 200, reclaimed.text
        started = time.monotonic()
        again = await client.post(
            f"/v1/tasks/{key}/handoff",
            headers=hb,
            json={"next_status": "REVIEWING", "brief": brief},
        )
        assert again.status_code == 200, again.text
        took, frame = await la.wait_for(started, _poke(key))
        hops.append((f"B delivers fix -> A hears {frame['kind']}", took))
    finally:
        la.task.cancel()
        lb.task.cancel()

    print()
    for label, took in hops:
        print(f"  {label:<52} {took * 1000:6.0f} ms")
    assert all(took < MAX_SECONDS for _, took in hops), hops


async def test_several_agents_at_once_and_nothing_is_missed_while_away(
    client, admin_headers, workspace
):
    """A hands out work to B and C at once; B and C talk to each other; D was
    not listening when its task came and gets it the moment it connects."""
    ws_id = workspace["workspace"]["id"]
    ha, _a = await _agent(client, admin_headers, ws_id, "SUPER_ADMIN")
    hb, b = await _agent(client, admin_headers, ws_id, "EXECUTOR")
    hc, c = await _agent(client, admin_headers, ws_id, "EXECUTOR")
    hd, d = await _agent(client, admin_headers, ws_id, "EXECUTOR")
    lb, lc = Listener(client, hb), Listener(client, hc)
    listeners = [lb, lc]
    hops: list[tuple[str, float]] = []
    try:
        await asyncio.wait_for(asyncio.gather(lb.ready.wait(), lc.ready.wait()), timeout=10)

        async def give(peer):
            key = f"E-{uuid.uuid4().hex[:8].upper()}"
            made = await client.post(
                "/v1/tasks",
                headers=ha,
                json={
                    "task_key": key,
                    "title": f"Task {key}",
                    "assignee": f"agent:{peer}",
                    "assigned_role": "EXECUTOR",
                },
            )
            assert made.status_code == 201, made.text
            return key

        started = time.monotonic()
        k1, k2 = await asyncio.gather(give(b), give(c))
        hops.append(("A -> B and C at once: B hears", (await lb.wait_for(started, _poke(k1)))[0]))
        hops.append(("A -> B and C at once: C hears", (await lc.wait_for(started, _poke(k2)))[0]))

        started = time.monotonic()
        await _mcp(
            client,
            hb,
            "send_message",
            {"body": "C, can you check my totals?", "peer_ref": f"agent:{c}"},
        )
        took, _ = await lc.wait_for(started, _message("C, can you check my totals?"))
        hops.append(("B asks C -> C hears", took))
        started = time.monotonic()
        await _mcp(
            client,
            hc,
            "send_message",
            {"body": "B, row 7 is double-counted", "peer_ref": f"agent:{b}"},
        )
        took, _ = await lb.wait_for(started, _message("B, row 7 is double-counted"))
        hops.append(("C answers B -> B hears", took))

        # D was away when its task came: the first thing its stream gives it
        # on connecting is that task, without D looking anything up.
        k3 = await give(d)
        started = time.monotonic()
        ld = Listener(client, hd)
        listeners.append(ld)
        took, frame = await ld.wait_for(started, _poke(k3))
        assert frame["kind"] == "task.assigned" and frame["replay"] is True
        hops.append(("D was away; connects -> gets its task", took))
    finally:
        for listener in listeners:
            listener.task.cancel()

    print()
    for label, took in hops:
        print(f"  {label:<52} {took * 1000:6.0f} ms")
    assert all(took < MAX_SECONDS for _, took in hops), hops
