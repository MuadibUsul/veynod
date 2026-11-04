"""The nerve socket: two receipts, and what a missing one does.

Pure in-process tests with a fake socket: no database, no network. The DB side
is covered by the integration test in ``test_nerve_dispatch.py``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


protocol = _load("nerve_protocol_test", "contracts/nerve_protocol.py")
hub_module = _load("nerve_hub_test", "AgentGraph/app/services/nerve_hub.py")
NerveFrameType = protocol.NerveFrameType


class FakeWS:
    """Records what was sent; ``behaviour`` decides what the peer answers."""

    def __init__(self, behaviour: str, delay: float = 0.0) -> None:
        self.behaviour = behaviour
        self.delay = delay
        self.sent: list[dict] = []
        self.closed: list[tuple[int, str]] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)
        if payload.get("type") != NerveFrameType.TASK_POKE.value:
            return
        if self.delay:
            # A far peer: the receipts come back after the round trip.
            asyncio.get_running_loop().call_later(
                self.delay, lambda: asyncio.ensure_future(self._answer(payload)))
            return
        await self._answer(payload)

    async def _answer(self, payload: dict) -> None:
        if self.behaviour in ("net", "both"):
            await _reply(payload, "task.ack.net", {"received_at_ms": 0})
        if self.behaviour == "both":
            await _reply(payload, "task.ack.exec", {"worker_pid": 4242})

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


async def _reply(poke: dict, frame_type: str, payload: dict) -> None:
    """Deliver a reply the way the route does, after the poke is sent."""
    for _ in range(50):
        await asyncio.sleep(0.005)
        sock = _HUB.get("ws", "agent-a")
        if sock is None:
            continue
        frame = {"type": frame_type, "frame_id": poke["frame_id"], "payload": payload}
        _HUB.on_frame(sock, frame)
        return


_HUB = hub_module.NerveHub()


def _sock(ws: FakeWS):
    return hub_module.ActiveSocket(
        workspace_id="ws", agent_id="agent-a", host_id="host-1", conn_id="c1", ws=ws)


async def test_two_receipts_make_a_successful_dispatch() -> None:
    async def run():
        ws = FakeWS("both")
        sock = await _HUB.register(_sock(ws))
        ok, reason, facts = await _HUB.dispatch(
            sock, task_key="T-1", placement_id="p1", title="t", epoch=7)
        await _HUB.unregister(sock, "test")
        return ok, reason, facts

    ok, reason, facts = await run()
    assert ok is True and reason == "DISPATCHED"
    assert facts["worker_pid"] == 4242
    assert facts["net_rtt_ms"] >= 0 and facts["exec_startup_ms"] >= 0


async def test_a_silent_peer_fails_the_first_receipt_and_kicks_the_socket() -> None:
    async def run():
        ws = FakeWS("none")
        sock = await _HUB.register(_sock(ws))
        git = await _HUB.dispatch(sock, task_key="T-1", placement_id="p1", title="t", epoch=1)
        await _HUB.unregister(sock, "test")
        return git

    ok, reason, _ = await run()
    assert ok is False
    assert reason == protocol.NerveError.NET_ACK_TIMEOUT.value


async def test_a_peer_that_answers_net_but_never_orientates_fails_the_second() -> None:
    async def run():
        ws = FakeWS("net")
        sock = await _HUB.register(_sock(ws))
        result = await _HUB.dispatch(sock, task_key="T-1", placement_id="p1", title="t", epoch=3)
        await _HUB.unregister(sock, "test")
        return result

    ok, reason, _ = await run()
    assert ok is False
    assert reason == protocol.NerveError.EXEC_ACK_TIMEOUT.value


async def test_a_busy_agent_refuses_before_a_frame_is_sent() -> None:
    async def run():
        ws = FakeWS("both")
        sock = await _HUB.register(_sock(ws))
        sock.active = sock.slots  # every slot runs something (from its last ping)
        result = await _HUB.dispatch(sock, task_key="T-1", placement_id="p1", title="t", epoch=1)
        await _HUB.unregister(sock, "test")
        return result, ws

    (ok, reason, _), ws = await run()
    assert ok is False and reason == protocol.NerveError.BUSY.value
    assert ws.sent == []


async def test_the_newest_socket_of_an_agent_wins() -> None:
    async def run():
        first = await _HUB.register(_sock(FakeWS("both")))
        second_ws = FakeWS("both")
        second = await _HUB.register(
            hub_module.ActiveSocket(workspace_id="ws", agent_id="agent-a", host_id="host-1",
                                    conn_id="c2", ws=second_ws))
        current = _HUB.get("ws", "agent-a")
        await _HUB.unregister(second, "test")
        return first, second, current

    first, second, current = await run()
    assert current is second
    assert first.closed is True


async def test_busy_reject_fails_the_pending_receipt() -> None:
    async def run():
        ws = FakeWS("none")
        sock = await _HUB.register(_sock(ws))
        pending = asyncio.ensure_future(
            _HUB.dispatch(sock, task_key="T-1", placement_id="p1", title="t", epoch=1))
        await asyncio.sleep(0.02)
        _HUB.on_frame(sock, {"type": "task.busy.reject", "frame_id": "p1",
                             "payload": {"reason": "WORKER_ALREADY_BUSY"}})
        result = await pending
        await _HUB.unregister(sock, "test")
        return result

    ok, reason, _ = await run()
    assert ok is False
    assert reason.startswith(protocol.NerveError.REJECTED.value)


def test_ping_reports_the_worker_and_is_answered() -> None:
    sock = _sock(FakeWS("both"))
    reply = _HUB.on_frame(sock, {"type": "sys.ping", "frame_id": "f1",
                                 "payload": {"worker_busy": True, "active_task_key": "T"}})
    assert reply["type"] == "sys.pong"
    assert sock.worker_busy is True and sock.active_task_key == "T"


def test_parse_frame_tolerates_junk() -> None:
    assert protocol.parse_frame(None) is None
    assert protocol.parse_frame({"payload": {}}) is None
    assert protocol.parse_frame({"type": "sys.ping"}) == {
        "type": "sys.ping", "payload": {}, "frame_id": None}
    assert protocol.parse_frame({"type": "x", "payload": "no"})["payload"] == {}


def test_poke_frame_carries_the_epoch_and_orientation_budget() -> None:
    frame = protocol.poke_frame(task_key="T", lease_epoch=4, placement_id="p",
                                title="do it").to_dict()
    assert frame["payload"]["lease_epoch"] == 4
    assert frame["payload"]["orientation_timeout_ms"] == protocol.ORIENTATION_TIMEOUT_MS
    assert protocol.NET_ACK_TIMEOUT_S == pytest.approx(0.2)
    assert protocol.EXEC_ACK_TIMEOUT_S == pytest.approx(5.0)


def test_nerve_url_and_worker_command() -> None:
    """The sidecar's pure helpers, so a regression there fails here."""
    import subprocess

    ok = subprocess.run(["node", "--check", str(ROOT / "AgentGraph/cli/bin/veynod-messenger.mjs")],
                        capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr


def test_the_first_receipt_deadline_follows_the_round_trip() -> None:
    deadline = protocol.net_ack_deadline_s
    # A near peer keeps the 200 ms budget; a far one gets three round trips.
    assert deadline(20) == pytest.approx(protocol.NET_ACK_TIMEOUT_S)
    assert deadline(250) == pytest.approx(0.75)
    # Unknown round trip (first poke on a socket), and a hard ceiling.
    assert deadline(0) == pytest.approx(protocol.NET_ACK_FIRST_CONTACT_S)
    assert deadline(None) == pytest.approx(protocol.NET_ACK_FIRST_CONTACT_S)
    assert deadline(10_000) == pytest.approx(protocol.NET_ACK_MAX_S)
    assert protocol.smooth_rtt_ms(0, 300) == 300
    assert protocol.smooth_rtt_ms(100, 900) == pytest.approx(200)


async def test_a_far_peer_is_not_kicked_for_its_round_trip() -> None:
    """A 300 ms link answered every poke 'too late' under a flat 200 ms timer."""
    async def run(rtt_known: float):
        ws = FakeWS("both", delay=0.3)
        sock = await _HUB.register(_sock(ws))
        sock.rtt_ms = rtt_known
        result = await _HUB.dispatch(sock, task_key="T-1", placement_id="p1", title="t", epoch=1)
        rtt_after = sock.rtt_ms
        await _HUB.unregister(sock, "test")
        return result, rtt_after

    (ok, reason, facts), rtt_after = await run(0.0)
    assert ok is True, reason
    assert facts["net_rtt_ms"] >= 300 and rtt_after >= 300
    # Once the socket has shown a 300 ms round trip, the next deadline is ~0.9 s.
    (ok, reason, _), _ = await run(rtt_after)
    assert ok is True, reason
    # A socket known to be near still fails fast when it does not answer in time.
    (ok, reason, _), _ = await run(20.0)
    assert ok is False and reason == protocol.NerveError.NET_ACK_TIMEOUT.value


async def test_the_reaper_loop_starts_and_survives_a_pass() -> None:
    """It imported a helper that did not exist and died at start-up, silently."""
    calls = []

    async def fake_reap():
        calls.append(1)
        return {}

    original = hub_module.reap_stale
    hub_module.reap_stale = fake_reap
    try:
        task = asyncio.ensure_future(hub_module.nerve_reaper_loop(0.01))
        await asyncio.sleep(0.05)
        assert not task.done(), task.exception() if task.done() else None
        task.cancel()
    finally:
        hub_module.reap_stale = original
    assert len(calls) >= 2
