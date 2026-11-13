"""The three stages of a Loop dispatch: delivered, started, finished.

Delivered is not started and started is not finished. These tests drive the
server with a Python stand-in for the messenger: a computer that refuses to
start, a worker that dies without a word, a worker that arrives after its round
moved on, a task that came back and must be placed again, and a supervise round
that only counts once its placement reports finished.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import asyncpg
import pytest
import websockets

from contracts.nerve_protocol import orientation_timeout_ms
from tests.test_scheduler import ADMIN, REPO, _db, _schedule, _sql, _task, _workspace

pytestmark = pytest.mark.integration


def test_the_watchdog_follows_the_hub_wait() -> None:
    # One budget for both ends: the messenger's watchdog fires before the hub
    # stops waiting, whatever the wait is set to.
    assert orientation_timeout_ms(5.0) == 4_500
    assert orientation_timeout_ms(12.0) == 11_500
    assert orientation_timeout_ms(1.0) == 500
    assert orientation_timeout_ms(0.2) == 500


def test_only_a_held_lease_blocks_a_dispatch() -> None:
    from app.services.nerve_hub import dispatchable

    def task(state: str, status: str = "READY", owner: str | None = None,
             live: bool = False) -> dict:
        return {"dispatch_state": state, "status": status, "lease_owner": owner,
                "lease_live": live}

    assert dispatchable(task("IDLE"))
    assert not dispatchable(task("AWAITING_NET_ACK"))
    assert not dispatchable(task("AWAITING_EXEC_ACK"))
    # A computer really holds it: leave it alone.
    assert not dispatchable(task("ACTIVE_LEASE", "IN_PROGRESS", "box", True))
    # Leftovers of a dispatch whose task came back: never a reason to refuse.
    assert dispatchable(task("ACTIVE_LEASE", "READY"))
    assert dispatchable(task("ACTIVE_LEASE", "IN_PROGRESS", None, False))
    assert dispatchable(task("ACTIVE_LEASE", "IN_PROGRESS", "box", False))


def test_percentiles_are_nearest_rank() -> None:
    from app.services.scheduler import _percentiles

    assert _percentiles([]) == {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}
    one = _percentiles([40])
    assert (one["p50"], one["p95"], one["p99"], one["max"]) == (40, 40, 40, 40)
    hundred = _percentiles(list(range(1, 101)))
    assert (hundred["p50"], hundred["p95"], hundred["p99"], hundred["max"]) == (50, 95, 99, 100)


# ------------------------------------------------------------------ a computer


async def _fetch(query: str, *args) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(_db())
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


class Computer:
    """A messenger stand-in: one socket, every frame kept, pokes answered by a policy.

    ``policy(poke)`` returns the frames to send back for that poke, as
    ``(type, payload)`` pairs; the default answers both receipts.
    """

    def __init__(self, socket, host: dict) -> None:
        self.socket = socket
        self.host = host
        self.frames: list[dict] = []
        self.pokes: list[dict] = []
        self.policy = lambda poke: [("task.ack.net", {"received_at_ms": 0}),
                                    ("task.ack.exec", {"worker_pid": 4242})]
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self.socket:
                frame = json.loads(raw)
                self.frames.append(frame)
                if frame["type"] == "task.poke":
                    self.pokes.append(frame)
                    for kind, payload in self.policy(frame):
                        await self.send(kind, payload, frame["frame_id"])
        except websockets.exceptions.ConnectionClosed:
            pass

    async def send(self, kind: str, payload: dict, frame_id: str | None = None) -> None:
        await self.socket.send(json.dumps({"type": kind, "payload": payload,
                                           "frame_id": frame_id or str(uuid.uuid4())}))

    async def ping(self, **payload) -> None:
        await self.send("sys.ping", {"slots": 1, "active_count": 0, **payload})
        await self.wait_for(lambda: any(frame["type"] == "sys.pong" for frame in self.frames))
        self.frames = [frame for frame in self.frames if frame["type"] != "sys.pong"]

    async def wait_for(self, check, timeout: float = 15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = check()
            if found:
                return found
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out; frames: {[frame['type'] for frame in self.frames]}")

    async def poke(self, step: str | None = None, after: int = 0) -> dict:
        def find():
            wanted = [poke for poke in self.pokes[after:]
                      if step is None or poke["payload"].get("step") == step]
            return wanted[0] if wanted else None

        return await self.wait_for(find)

    async def close(self) -> None:
        await self.socket.close()
        await asyncio.gather(self._reader, return_exceptions=True)


async def _computer(api_base_url: str, client, ws: dict, name: str, *, role: str = "EXECUTOR",
                    capabilities: list[str] | None = None, slots: int = 1) -> Computer:
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"role_scope": role, "allowed_namespaces": ["*"]})
    assert minted.status_code == 201, minted.text
    key = minted.json()["api_key"]
    headers = {"Authorization": f"Bearer {key}"}
    named = await client.post("/v1/agents/register", headers=headers, json={
        "agent_id": name, "capabilities": capabilities or []})
    assert named.status_code in (200, 201), named.text
    hello = await client.post("/v1/runner/hello", headers=headers, json={"host": {
        "os": "linux", "arch": "x64", "max_concurrency": slots, "repos": [REPO],
        "runtimes": [{"id": "custom", "available": True}]}})
    assert hello.status_code == 200, hello.text
    host = {"headers": headers, "agent_id": name, "host_id": hello.json()["host_id"], "key": key}
    socket = await websockets.connect(
        api_base_url.replace("http://", "ws://") + "/v1/ws/nerve",
        additional_headers={"Authorization": f"Bearer {key}", "X-Agent-Id": name,
                            "X-Host-Id": host["host_id"]})
    ack = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
    assert ack["type"] == "auth.ack", ack
    return Computer(socket, host)


async def _placements(client, ws: dict, **params) -> list[dict]:
    listed = await client.get("/v1/runner/placements", headers=ws["owner"], params=params)
    assert listed.status_code == 200, listed.text
    return listed.json()["items"]


async def _placement(client, ws: dict, placement_id: str) -> dict:
    found = await client.get(f"/v1/runner/placements/{placement_id}", headers=ws["owner"])
    assert found.status_code == 200, found.text
    return found.json()


async def _task_row(client, ws: dict, key: str) -> dict:
    return (await client.get(f"/v1/tasks/{key}", headers=ws["owner"])).json()


async def _until(check, timeout: float = 20.0):
    """Poll an async check; the background pass and the dispatch loop also act."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await check()
        if last:
            return last
        await asyncio.sleep(0.2)
    raise AssertionError(f"condition not met in {timeout}s (last: {last!r})")


# ------------------------------------------------------------------ re-dispatch


async def test_a_task_that_came_back_is_dispatched_again(api_base_url, client) -> None:
    """ACTIVE_LEASE used to outlive the lease: the task returned to READY and no
    dispatch ever reached a computer again ("already dispatching")."""
    ws = await _workspace(client, "full")
    box = await _computer(api_base_url, client, ws, f"again-{uuid.uuid4().hex[:4]}")
    key = await _task(client, ws)
    try:
        await _schedule(client, ws)
        first = await box.poke("work")
        assert first["payload"]["task_key"] == key
        await _until(lambda: _is(client, ws, key, "IN_PROGRESS"))
        state = await _fetch("SELECT dispatch_state FROM collab_tasks WHERE task_key = $1", key)
        assert state[0]["dispatch_state"] == "ACTIVE_LEASE"

        # The computer gives the task back.
        released = await client.post(f"/v1/tasks/{key}/release", headers=box.host["headers"],
                                     json={})
        assert released.status_code == 200, released.text
        state = await _fetch("SELECT dispatch_state, status::text AS status FROM collab_tasks "
                             "WHERE task_key = $1", key)
        assert (state[0]["status"], state[0]["dispatch_state"]) == ("READY", "IDLE")
        # Its worker is gone: the next ping says the slot is free again.
        await box.ping(active_count=0)

        # ... and is placed and dispatched again, with a later lease epoch.
        await _schedule(client, ws)
        await _schedule(client, ws)
        second = await box.poke("work", after=1)
        assert second["payload"]["task_key"] == key
        assert second["frame_id"] != first["frame_id"]
        assert second["payload"]["lease_epoch"] > first["payload"]["lease_epoch"]
    finally:
        await box.close()


async def _is(client, ws: dict, key: str, status: str) -> bool:
    return (await _task_row(client, ws, key))["status"] == status


async def test_the_state_follows_the_lease_in_the_database(client) -> None:
    ws = await _workspace(client, "full")
    key = await _task(client, ws)
    # Whatever writes it: ACTIVE_LEASE without a held lease does not stick.
    await _sql("UPDATE collab_tasks SET dispatch_state = 'ACTIVE_LEASE' WHERE task_key = $1", key)
    state = await _fetch("SELECT dispatch_state FROM collab_tasks WHERE task_key = $1", key)
    assert state[0]["dispatch_state"] == "IDLE"


# ------------------------------------------------------------------ start refusals


async def test_a_refused_start_fails_at_once_with_its_reason(api_base_url, client) -> None:
    """The computer read the poke but its worker could not start. It says so, and
    the hub does not wait its five seconds out to find a timeout with no cause."""
    ws = await _workspace(client, "full")
    box = await _computer(api_base_url, client, ws, f"refuse-{uuid.uuid4().hex[:4]}")
    box.policy = lambda poke: [
        ("task.ack.net", {"received_at_ms": 0}),
        ("task.busy.reject", {"reason": "WORKER_START_FAILED: spawn node ENOENT"})]
    key = await _task(client, ws)
    try:
        started = time.monotonic()
        await _schedule(client, ws)
        poke = await box.poke("work")
        moved = await _until(lambda: _ended(client, ws, poke["frame_id"]))
        assert time.monotonic() - started < 4.0, "the refusal must not wait for the ACK timeout"
        assert moved["status"] == "moved"
        assert moved["reason"].startswith("nerve:AGENT_REJECTED: WORKER_START_FAILED"), moved
        assert moved["stage"] == "not_delivered"
        # The first receipt was measured even though the start failed.
        assert moved["timings"]["delivered_ms"] is not None
        assert moved["timings"]["worker_ready_ms"] is None
        # Nothing is left behind: the task is free and can be dispatched again.
        state = await _fetch("SELECT dispatch_state FROM collab_tasks WHERE task_key = $1", key)
        assert state[0]["dispatch_state"] == "IDLE"
        # The hub told the computer to drop whatever it started for that frame.
        revoked = await box.wait_for(lambda: [frame for frame in box.frames
                                              if frame["type"] == "lease.revoked"])
        assert revoked[0]["payload"]["placement_id"] == poke["frame_id"]
    finally:
        await box.close()


async def _ended(client, ws: dict, placement_id: str) -> dict | None:
    row = await _placement(client, ws, placement_id)
    return row if row["ended_at"] else None


async def test_the_poke_carries_one_budget_for_both_ends(api_base_url, client) -> None:
    ws = await _workspace(client, "full")
    box = await _computer(api_base_url, client, ws, f"budget-{uuid.uuid4().hex[:4]}")
    await _task(client, ws)
    try:
        await _schedule(client, ws)
        payload = (await box.poke("work"))["payload"]
        # The messenger's watchdog is the hub's wait less the margin, from one setting.
        assert payload["exec_ack_timeout_ms"] == 5000
        assert payload["orientation_timeout_ms"] == 4500
    finally:
        await box.close()


# ------------------------------------------------------------------ failed runs


async def test_a_failed_report_on_a_taken_task_releases_it_at_once(api_base_url, client) -> None:
    """The computer took the task, then its run failed. The task used to stay
    IN_PROGRESS under that lease until it ran out (15 minutes by default)."""
    ws = await _workspace(client, "full")
    box = await _computer(api_base_url, client, ws, f"fail-{uuid.uuid4().hex[:4]}")
    key = await _task(client, ws)
    try:
        await _schedule(client, ws)
        poke = await box.poke("work")
        await _until(lambda: _is(client, ws, key, "IN_PROGRESS"))
        reported = await client.post(
            f"/v1/runner/placements/{poke['frame_id']}/report", headers=box.host["headers"],
            json={"outcome": "failed", "note": "claude is not on PATH"})
        assert reported.status_code == 200, reported.text

        await _schedule(client, ws)
        ended = await _until(lambda: _ended(client, ws, poke["frame_id"]))
        assert (ended["status"], ended["reason"]) == ("failed", "reported_failed"), ended
        assert ended["note"] == "claude is not on PATH"
        assert ended["stage"] == "failed"
        # Free for the next placement straight away (the pass may already have made it).
        row = await _fetch("SELECT lease_owner, dispatch_state FROM collab_tasks "
                           "WHERE task_key = $1", key)
        assert row[0]["dispatch_state"] in ("IDLE", "AWAITING_NET_ACK", "AWAITING_EXEC_ACK",
                                            "ACTIVE_LEASE")
        later = [item for item in await _placements(client, ws, task_key=key)
                 if item["placement_id"] != poke["frame_id"]]
        assert later or (await _task_row(client, ws, key))["status"] == "READY"
    finally:
        await box.close()


async def test_a_worker_that_dies_without_a_report_is_a_failure_now(api_base_url, client) -> None:
    ws = await _workspace(client, "full")
    box = await _computer(api_base_url, client, ws, f"dies-{uuid.uuid4().hex[:4]}")
    key = await _task(client, ws)
    try:
        await _schedule(client, ws)
        poke = await box.poke("work")
        await _until(lambda: _is(client, ws, key, "IN_PROGRESS"))
        # The messenger saw the worker process end with nothing reported.
        await box.send("task.worker.exit", {"placement_id": poke["frame_id"], "code": 3221226505,
                                            "signal": None, "reported": False}, poke["frame_id"])
        marked = await _until(lambda: _reported(client, ws, poke["frame_id"]))
        assert marked["reported"] == "failed"
        assert "exited without a report" in marked["report_note"]
        assert "3221226505" in marked["report_note"]

        await _schedule(client, ws)
        ended = await _until(lambda: _ended(client, ws, poke["frame_id"]))
        assert (ended["status"], ended["reason"]) == ("failed", "reported_failed"), ended

        # A worker that did report is left as it reported.
        await box.ping(active_count=0)
        await _schedule(client, ws)
        again = await box.poke("work", after=1)
        done = await client.post(
            f"/v1/runner/placements/{again['frame_id']}/report", headers=box.host["headers"],
            json={"outcome": "finished"})
        assert done.status_code == 200, done.text
        await box.send("task.worker.exit", {"placement_id": again["frame_id"], "code": 0,
                                            "signal": None, "reported": True}, again["frame_id"])
        await box.ping()
        assert (await _placement(client, ws, again["frame_id"]))["reported"] == "finished"
    finally:
        await box.close()


async def _reported(client, ws: dict, placement_id: str) -> dict | None:
    row = await _placement(client, ws, placement_id)
    return row if row["reported"] else None


# ------------------------------------------------------------------ supervise rounds


async def _stuck_task(client, ws: dict) -> str:
    # A task no computer reaches gives the supervisor something to look at.
    return await _task(client, ws, repo="acme/unreachable")


async def _round(client, ws: dict, box: Computer, after: int = 0) -> dict:
    await _schedule(client, ws)
    return await box.poke("supervise", after=after)


async def test_a_round_counts_only_when_it_reports_finished(api_base_url, client) -> None:
    ws = await _workspace(client, "full")
    boss = await _computer(api_base_url, client, ws, f"boss-{uuid.uuid4().hex[:4]}",
                           role="AUDITOR", capabilities=["supervisor"])
    await _stuck_task(client, ws)
    try:
        poke = await _round(client, ws, boss)
        placement_id = poke["frame_id"]
        ready = await _until(lambda: _at_stage(client, ws, placement_id, "ready"))
        # Both receipts are in: the computer read it and a worker is up. Nothing ran yet.
        assert ready["status"] in ("offered", "claimed") and ready["reported"] is None
        assert ready["timings"]["delivered_ms"] is not None
        assert ready["timings"]["worker_ready_ms"] is not None
        assert ready["timings"]["execution_started_ms"] is None

        # The worker asks to start: the placement is open, so the round begins.
        began = await client.post(f"/v1/runner/placements/{placement_id}/start",
                                  headers=boss.host["headers"], json={})
        assert began.status_code == 200, began.text
        assert began.json()["start"] is True
        assert began.json()["detail"]["agenda"], began.json()
        row = await _placement(client, ws, placement_id)
        assert row["stage"] == "started" and row["started_at"]
        assert row["timings"]["execution_started_ms"] is not None
        assert row["timings"]["execution_started_ms"] >= row["timings"]["worker_ready_ms"] - 50

        # Still not a success: only the report makes it one.
        metrics = (await client.get("/v1/runner/metrics", headers=ws["owner"])).json()
        assert metrics["supervise"]["finished"] == 0 and metrics["supervise"]["open"] == 1

        done = await client.post(f"/v1/runner/placements/{placement_id}/report",
                                 headers=boss.host["headers"], json={"outcome": "finished"})
        assert done.status_code == 200, done.text
        await _schedule(client, ws)
        ended = await _until(lambda: _ended(client, ws, placement_id))
        assert (ended["status"], ended["stage"]) == ("delivered", "finished")

        metrics = (await client.get("/v1/runner/metrics", headers=ws["owner"])).json()
        assert metrics["supervise"]["finished"] == 1
        assert metrics["supervise"]["finished_rate"] == 1.0
        assert metrics["supervise"]["finished_in_a_row"] == 1
        assert metrics["timings"]["delivered_ms"]["count"] >= 1
        assert metrics["timings"]["execution_started_ms"]["count"] == 1
        assert metrics["targets_ms"]["delivered_p95"] == 1000

        events = [row["event_type"] for row in await _fetch(
            "SELECT event_type FROM timeline_streams WHERE workspace_id = $1::uuid "
            "AND event_type LIKE 'LOOP_ROUND%' ORDER BY created_at", ws["id"])]
        assert events == ["LOOP_ROUND", "LOOP_ROUND_FINISHED"], events
    finally:
        await boss.close()


async def _at_stage(client, ws: dict, placement_id: str, stage: str) -> dict | None:
    row = await _placement(client, ws, placement_id)
    return row if row["stage"] == stage else None


async def test_a_worker_that_arrives_late_is_skipped_never_a_success(api_base_url, client) -> None:
    """The round moved on before the worker asked to start (the case in the
    field: the worker exited 0 with a local "skipped: placement is moved")."""
    ws = await _workspace(client, "full")
    boss = await _computer(api_base_url, client, ws, f"late-{uuid.uuid4().hex[:4]}",
                           role="AUDITOR", capabilities=["supervisor"])
    # The computer reads the poke, but no worker comes up before the hub stops waiting.
    boss.policy = lambda poke: [("task.ack.net", {"received_at_ms": 0})]
    await _stuck_task(client, ws)
    try:
        poke = await _round(client, ws, boss)
        placement_id = poke["frame_id"]
        moved = await _until(lambda: _ended(client, ws, placement_id), timeout=30)
        assert moved["status"] == "moved"
        assert moved["reason"].startswith("nerve:QOS_TIMEOUT_EXEC_ACK"), moved
        # The hub told the computer to kill whatever it was still starting.
        revoked = await boss.wait_for(lambda: [frame for frame in boss.frames
                                               if frame["type"] == "lease.revoked"])
        assert revoked[0]["payload"]["placement_id"] == placement_id

        # The worker comes up anyway and asks to start: no, with the reason.
        answer = await client.post(f"/v1/runner/placements/{placement_id}/start",
                                   headers=boss.host["headers"], json={})
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["start"] is False
        assert body["skip_reason"].startswith("placement is moved (nerve:QOS_TIMEOUT_EXEC_ACK")
        row = await _placement(client, ws, placement_id)
        assert (row["status"], row["reported"], row["stage"]) == ("moved", "skipped", "skipped")
        assert row["started_at"] is None

        metrics = (await client.get("/v1/runner/metrics", headers=ws["owner"])).json()
        assert metrics["supervise"]["skipped"] == 1
        assert metrics["supervise"]["finished"] == 0
        assert metrics["supervise"]["finished_rate"] == 0.0
        assert metrics["not_delivered_reasons"].get("QOS_TIMEOUT_EXEC_ACK_5000MS") == 1
        assert metrics["pokes"]["worker_start_failure_rate"] == 1.0

        events = [row["event_type"] for row in await _fetch(
            "SELECT event_type FROM timeline_streams WHERE workspace_id = $1::uuid "
            "AND event_type LIKE 'LOOP_ROUND%' ORDER BY created_at", ws["id"])]
        assert events == ["LOOP_ROUND", "LOOP_ROUND_MOVED", "LOOP_ROUND_SKIPPED"], events
        # Asking twice records it once.
        await client.post(f"/v1/runner/placements/{placement_id}/start",
                          headers=boss.host["headers"], json={})
        again = await _fetch(
            "SELECT count(*) AS n FROM timeline_streams WHERE workspace_id = $1::uuid "
            "AND event_type = 'LOOP_ROUND_SKIPPED'", ws["id"])
        assert again[0]["n"] == 1
    finally:
        await boss.close()


async def test_a_round_has_its_own_lane_beside_a_running_task(api_base_url, client) -> None:
    """One computer, one slot, a long task in it: the round is still delivered,
    and the round does not take the slot the task needs."""
    ws = await _workspace(client, "full")
    box = await _computer(api_base_url, client, ws, f"lane-{uuid.uuid4().hex[:4]}",
                          capabilities=["supervisor"])
    await box.ping(supervise_slots=1, supervising=0)
    key = await _task(client, ws)
    try:
        await _schedule(client, ws)
        work = await box.poke("work")
        await _until(lambda: _is(client, ws, key, "IN_PROGRESS"))
        # The computer says its one work slot is taken; the lane is free.
        await box.ping(slots=1, active_count=1, worker_busy=True, supervise_slots=1, supervising=0)

        await _stuck_task(client, ws)
        round_ = await _round(client, ws, box)
        assert round_["frame_id"] != work["frame_id"]
        ready = await _until(lambda: _at_stage(client, ws, round_["frame_id"], "ready"))
        assert ready["step"] == "supervise"
        # The task was not disturbed.
        assert (await _task_row(client, ws, key))["status"] == "IN_PROGRESS"
    finally:
        await box.close()


async def test_a_computer_without_the_lane_is_not_given_a_round_while_busy(
        api_base_url, client) -> None:
    ws = await _workspace(client, "full")
    box = await _computer(api_base_url, client, ws, f"old-{uuid.uuid4().hex[:4]}",
                          capabilities=["supervisor"])
    key = await _task(client, ws)
    try:
        await _schedule(client, ws)
        await box.poke("work")
        await _until(lambda: _is(client, ws, key, "IN_PROGRESS"))
        # An older CLI: one slot for everything, and it is taken.
        await box.ping(slots=1, active_count=1, worker_busy=True)
        await _stuck_task(client, ws)
        await _schedule(client, ws)
        rounds = await _until(lambda: _placements(client, ws, step="supervise"))
        moved = await _until(lambda: _ended(client, ws, rounds[0]["placement_id"]))
        assert (moved["status"], moved["reason"]) == ("moved", "nerve:AGENT_WORKER_BUSY"), moved
        assert not [poke for poke in box.pokes if poke["payload"]["step"] == "supervise"]
    finally:
        await box.close()


async def test_a_draining_computer_keeps_its_offer_until_it_is_back(api_base_url, client) -> None:
    """A computer finishing its work before it restarts on a new version is not
    failed for it: the offer waits and is delivered when it takes work again."""
    ws = await _workspace(client, "full")
    box = await _computer(api_base_url, client, ws, f"drain-{uuid.uuid4().hex[:4]}")
    await box.ping(draining=True)
    key = await _task(client, ws)
    try:
        result = await _schedule(client, ws)
        assert result["nerve"]["skipped"] >= 1, result
        await asyncio.sleep(2.5)  # the dispatch loop runs every second
        assert box.pokes == []
        offered = await _placements(client, ws, task_key=key)
        assert [item["status"] for item in offered] == ["offered"], offered

        await box.ping(draining=False)
        poke = await box.poke("work")
        assert poke["payload"]["task_key"] == key
    finally:
        await box.close()


async def test_a_stale_epoch_is_not_started(api_base_url, client) -> None:
    """A worker of an earlier dispatch must not work a task a later one owns."""
    ws = await _workspace(client, "full")
    box = await _computer(api_base_url, client, ws, f"epoch-{uuid.uuid4().hex[:4]}")
    key = await _task(client, ws)
    try:
        await _schedule(client, ws)
        poke = await box.poke("work")
        await _until(lambda: _is(client, ws, key, "IN_PROGRESS"))
        epoch = poke["payload"]["lease_epoch"]
        ok = await client.post(f"/v1/runner/placements/{poke['frame_id']}/start",
                               headers=box.host["headers"], json={"lease_epoch": epoch})
        assert ok.json()["start"] is True, ok.text
        # The start is recorded once: asking again does not move the time.
        first = (await _placement(client, ws, poke["frame_id"]))["timings"]["execution_started_ms"]
        await client.post(f"/v1/runner/placements/{poke['frame_id']}/start",
                          headers=box.host["headers"], json={"lease_epoch": epoch})
        assert (await _placement(client, ws, poke["frame_id"]))["timings"][
            "execution_started_ms"] == first

        stale = await client.post(f"/v1/runner/placements/{poke['frame_id']}/start",
                                  headers=box.host["headers"], json={"lease_epoch": epoch - 1})
        assert stale.json()["start"] is False
        assert "stale lease epoch" in stale.json()["skip_reason"]
        # Another computer's key cannot start (or skip) this placement.
        other = await _computer(api_base_url, client, ws, f"other-{uuid.uuid4().hex[:4]}")
        try:
            refused = await client.post(f"/v1/runner/placements/{poke['frame_id']}/start",
                                        headers=other.host["headers"], json={})
            assert refused.status_code == 403, refused.text
        finally:
            await other.close()
    finally:
        await box.close()


async def test_the_stopped_tasks_come_with_what_their_rounds_failed_on(client) -> None:
    """Before any stopped task goes back to READY, each is checked for why it
    stopped: the list says it per task instead of one bulk reset."""
    ws = await _workspace(client, "full")
    key = await _task(client, ws)
    task_id = (await _fetch("SELECT id FROM collab_tasks WHERE task_key = $1", key))[0]["id"]
    for index in range(3):
        await _sql(
            "INSERT INTO runner_placements (workspace_id, task_id, task_key, mode, status, step, "
            "reason, note, ended_at) VALUES ($1::uuid, $2, $3, 'assign', 'failed', 'work', "
            "'not_claimed', $4, NOW())", ws["id"], task_id, key, f"round {index}")
    await _sql("UPDATE collab_tasks SET status = 'HANDOFF' WHERE task_key = $1", key)
    metrics = (await client.get("/v1/runner/metrics", headers=ws["owner"])).json()
    stopped = [item for item in metrics["stopped"] if item["task_key"] == key]
    assert len(stopped) == 1, metrics["stopped"]
    assert stopped[0]["role"] == "EXECUTOR" and stopped[0]["repo"] == REPO
    assert [item["reason"] for item in stopped[0]["rounds"]] == ["not_claimed"] * 3


# ------------------------------------------------------------------ no dead ends


def test_a_repository_is_matched_by_what_it_is_not_how_it_was_typed() -> None:
    from app.services.scheduler import same_repo

    assert same_repo("MuadibUsul/veynod", "muadibusul/Veynod")
    assert same_repo("https://github.com/MuadibUsul/veynod.git", "MuadibUsul/veynod")
    # A bare name is the repository of that name on the computer.
    assert same_repo("smoke", "smoke/smoke")
    assert same_repo("veynod", "MuadibUsul/veynod")
    assert not same_repo("acme/veynod", "MuadibUsul/veynod")
    assert not same_repo("site", "acme/website")
    assert not same_repo("", "acme/site")


def test_a_coding_agent_has_the_general_capabilities_without_declaring_them() -> None:
    from app.services.scheduler import _capabilities

    bare = {"capabilities": [], "runtimes": [{"id": "codex", "available": True}]}
    assert {"frontend", "backend", "docs", "codex"} <= _capabilities(bare)
    # Any computer that runs tasks has them, a custom command included (found in the
    # unattended run: the claim was refused on such a computer); one whose runtime
    # is not ready promises nothing.
    assert "frontend" in _capabilities({"capabilities": [], "runtimes": [{"id": "custom"}]})
    assert "frontend" not in _capabilities(
        {"capabilities": ["gpu"], "runtimes": [{"id": "claude", "available": False}]})
    assert "gpu" in _capabilities({"capabilities": ["GPU"], "runtimes": []})


async def test_an_analyst_or_frontend_task_finds_the_one_computer_there_is(client) -> None:
    """With one EXECUTOR computer, ANALYST tasks and tasks that name a general
    capability waited forever. Under full automation the computer takes them."""
    from tests.test_scheduler import _host

    ws = await _workspace(client, "full")
    host = await _host(client, ws, f"only-{uuid.uuid4().hex[:4]}", slots=3)
    research = f"an-{uuid.uuid4().hex[:8]}"
    made = await client.post("/v1/tasks", headers=ws["owner"], json={
        "task_key": research, "title": "Compare three vendors", "assigned_role": "ANALYST",
        "namespace": "default"})
    assert made.status_code == 201, made.text
    frontend = f"fe-{uuid.uuid4().hex[:8]}"
    made = await client.post("/v1/tasks", headers=ws["owner"], json={
        "task_key": frontend, "title": "Mobile shell", "assigned_role": "EXECUTOR",
        "namespace": "default", "required_capabilities": ["frontend"]})
    assert made.status_code == 201, made.text
    # A role its author made a hard requirement is kept.
    strict = f"st-{uuid.uuid4().hex[:8]}"
    made = await client.post("/v1/tasks", headers=ws["owner"], json={
        "task_key": strict, "title": "Analyst only", "assigned_role": "ANALYST",
        "required_role": "ANALYST", "namespace": "default"})
    assert made.status_code == 201, made.text

    await _schedule(client, ws)
    for key in (research, frontend):
        row = await _task_row(client, ws, key)
        assert row["assignee"] == host["assignee"], (key, row["assignee"])
    placements = await _placements(client, ws, task_key=strict)
    assert [(item["status"], item["reason"]) for item in placements] == [
        ("unplaced", "no_host_for_role")]

    # The computer it was placed on may claim it; another EXECUTOR may not.
    claimed = await client.post(f"/v1/tasks/{research}/claim", headers=host["headers"], json={})
    assert claimed.status_code == 200, claimed.text
    other = await _host(client, ws, f"other-{uuid.uuid4().hex[:4]}")
    refused = await client.post(f"/v1/tasks/{strict}/claim", headers=other["headers"], json={})
    assert refused.status_code == 403, refused.text


async def test_a_stopped_task_is_tried_again_once_its_failures_are_old(client) -> None:
    """HANDOFF after three failed rounds used to be the end. Old failures no
    longer count, so the task returns to the board; fresh ones keep it stopped."""
    from tests.test_scheduler import _host

    ws = await _workspace(client, "full")
    host = await _host(client, ws, f"again-{uuid.uuid4().hex[:4]}", slots=4)

    async def stopped(age: str) -> str:
        key = await _task(client, ws)
        task_id = (await _fetch("SELECT id FROM collab_tasks WHERE task_key = $1", key))[0]["id"]
        for _ in range(3):
            await _sql(
                "INSERT INTO runner_placements (workspace_id, task_id, task_key, host_id, "
                "agent_id, mode, status, step, reason, created_at, ended_at) VALUES ($1::uuid, $2, "
                f"$3, $4, $5, 'assign', 'failed', 'work', 'not_claimed', NOW() - INTERVAL '{age}', "
                f"NOW() - INTERVAL '{age}')",
                ws["id"], task_id, key, host["host_id"], host["agent_id"])
        await _sql("UPDATE collab_tasks SET status = 'HANDOFF' WHERE task_key = $1", key)
        return key

    old = await stopped("3 days")
    fresh = await stopped("1 hour")
    # A task with subtasks is not one piece of work: it is never reopened.
    parent = await stopped("3 days")
    child = await _task(client, ws)
    await _sql("UPDATE collab_tasks SET parent_task_id = (SELECT id FROM collab_tasks "
               "WHERE task_key = $1), status = 'DONE' WHERE task_key = $2", parent, child)
    await _sql("UPDATE collab_tasks SET handed_off_by = 'someone' WHERE task_key = $1", parent)

    result = await _schedule(client, ws)
    assert result["resumed"] >= 1, result
    row = await _task_row(client, ws, old)
    assert row["status"] in ("READY", "IN_PROGRESS") and row["assignee"] == host["assignee"], row
    assert (await _task_row(client, ws, fresh))["status"] == "HANDOFF"
    assert (await _task_row(client, ws, parent))["status"] == "HANDOFF"
    events = await _fetch(
        "SELECT payload->>'task_key' AS key FROM timeline_streams WHERE workspace_id = $1::uuid "
        "AND event_type = 'TASK_SCHEDULER_RESUMED'", ws["id"])
    assert [row["key"] for row in events] == [old]
    # The scheduler's stop is its own, not an agent's hand-off.
    stop = await _fetch("SELECT handed_off_by FROM collab_tasks WHERE task_key = $1", fresh)
    assert stop[0]["handed_off_by"] is None
    # The old failures do not count against the computer either: it was offered again.
    offered = [item for item in await _placements(client, ws, task_key=old)
               if item["status"] in ("offered", "claimed")]
    assert offered and offered[0]["host_id"] == host["host_id"]


async def test_the_doctor_judges_the_supervisor_by_rounds_that_finished(
        api_base_url, client) -> None:
    ws = await _workspace(client, "full")
    boss = await _computer(api_base_url, client, ws, f"doc-{uuid.uuid4().hex[:4]}",
                           role="AUDITOR", capabilities=["supervisor"])
    boss.policy = lambda poke: [("task.ack.net", {"received_at_ms": 0})]
    await _stuck_task(client, ws)

    async def check(name: str) -> dict:
        report = (await client.get("/v1/runner/doctor", headers=ws["owner"])).json()
        return {**next(item for item in report["checks"] if item["id"] == name),
                "doctor_ok": report["ok"]}

    try:
        assert (await check("rounds"))["ok"] is True  # nothing ended yet
        poke = await _round(client, ws, boss)
        await _until(lambda: _ended(client, ws, poke["frame_id"]), timeout=30)
        rounds = await check("rounds")
        # Online is not enough: a round that never finished turns the check red. It
        # is a warning, not a required check: it reports history, not this setup,
        # and `npx veynod` must not fail an install over rounds that ran before it.
        assert rounds["ok"] is False and rounds["required"] is False, rounds
        assert "0 of the last 1" in rounds["text"] and "nerve:" in rounds["text"]
        assert (await check("supervisor"))["text"] == "A supervisor computer is online."
    finally:
        await boss.close()


async def test_work_an_agent_handed_on_goes_back_on_the_board(client) -> None:
    """HANDOFF from an agent means "the next holder carries on". The scheduler
    placed only READY tasks, so unattended it waited for ever."""
    from tests.test_scheduler import _host

    ws = await _workspace(client, "full")
    host = await _host(client, ws, f"relay-{uuid.uuid4().hex[:4]}", slots=2)
    key = await _task(client, ws, repo=None)
    await _schedule(client, ws)
    claimed = await client.post(f"/v1/tasks/{key}/claim", headers=host["headers"], json={})
    assert claimed.status_code == 200, claimed.text
    passed = await client.post(f"/v1/tasks/{key}/handoff", headers=host["headers"], json={
        "next_status": "HANDOFF", "fencing_token": claimed.json()["task"]["fencing_token"],
        "brief": {"summary": "half done: the parser is written, the tests are not",
                  "key_metrics": {}, "unverified_hypotheses": [], "risk_flag": False}})
    assert passed.status_code == 200 and passed.json()["status"] == "HANDOFF", passed.text

    result = await _schedule(client, ws)
    assert result["resumed"] >= 1, result
    row = await _task_row(client, ws, key)
    # Back on the board and placed again; the brief and the hand-off author stay.
    assert row["status"] in ("READY", "IN_PROGRESS"), row
    assert row["handoff_summary"].startswith("half done"), row
    events = await _fetch(
        "SELECT payload->>'task_key' AS key FROM timeline_streams WHERE workspace_id = $1::uuid "
        "AND event_type = 'TASK_REOPENED_FROM_HANDOFF'", ws["id"])
    assert [item["key"] for item in events] == [key]

