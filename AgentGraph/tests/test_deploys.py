"""Deploy gate v2: per-project targets, CI releases, the gate and the deploy computer."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest

from app.errors import ApiError
from app.services.deploys import clean_schedule, in_window

# --------------------------------------------------------------------- pure


def test_a_schedule_window_is_checked_in_its_own_time_zone() -> None:
    window = clean_schedule({"days": [1, 2, 3, 4, 5], "start": "02:00", "end": "04:00",
                             "tz": "Europe/Berlin"})
    # Tuesday 01:30 UTC is 02:30 in Berlin (winter time): inside.
    assert in_window(window, datetime(2026, 11, 3, 1, 30, tzinfo=timezone.utc))
    assert not in_window(window, datetime(2026, 11, 3, 3, 30, tzinfo=timezone.utc))
    # Saturday is not a listed day.
    assert not in_window(window, datetime(2026, 11, 7, 1, 30, tzinfo=timezone.utc))
    assert not in_window(None, datetime.now(timezone.utc))


def test_a_window_may_wrap_past_midnight_and_belongs_to_the_day_it_opened() -> None:
    window = clean_schedule({"days": [5], "start": "22:00", "end": "02:00"})
    assert in_window(window, datetime(2026, 11, 6, 23, 0, tzinfo=timezone.utc))  # Fri 23:00
    assert in_window(window, datetime(2026, 11, 7, 1, 0, tzinfo=timezone.utc))   # Sat 01:00
    assert not in_window(window, datetime(2026, 11, 7, 23, 0, tzinfo=timezone.utc))
    assert not in_window(window, datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc))


def test_a_bad_schedule_is_refused() -> None:
    for bad in ({"start": "02:00", "end": "02:00"}, {"start": "2am", "end": "04:00"},
                {"days": [0], "start": "01:00", "end": "02:00"},
                {"start": "01:00", "end": "02:00", "tz": "Mars/Olympus"}, "nightly"):
        with pytest.raises(ApiError):
            clean_schedule(bad)


# --------------------------------------------------------------------- server

integration = pytest.mark.integration


def _digest(char: str) -> str:
    return "sha256:" + char * 64


def _manifest(**digests: str) -> dict:
    return {name: {"image": f"ghcr.io/acme/{name}", "digest": _digest(char)}
            for name, char in digests.items()}


async def _project(client, ws: dict) -> str:
    name = f"app-{uuid.uuid4().hex[:6]}"
    made = await client.post("/v1/projects", headers=ws["owner"], json={"name": name})
    assert made.status_code == 201, made.text
    return name


async def _deploy_host(client, ws: dict, name: str) -> dict:
    from tests.test_nerve_dispatch import _host

    host = await _host(client, ws, name)
    said = await client.post("/v1/runner/hello", headers=host["headers"], json={"host": {
        "os": "linux", "arch": "x64", "max_concurrency": 1, "repos": [],
        "runtimes": [{"id": "custom", "available": True}], "runner_version": "0.6.0",
        "capabilities_report": {"deploy": True, "runtime": "custom"}}})
    assert said.status_code == 200, said.text
    return host


async def _target(client, ws: dict, project: str, host: dict, **fields) -> str:
    put = await client.put(f"/v1/projects/{project}/deploys/production", headers=ws["owner"],
                           json={"enabled": True, "host_id": host["host_id"], "trigger": "auto",
                                 "branch": "main", **fields})
    assert put.status_code == 200, put.text
    token = await client.post(f"/v1/projects/{project}/deploys/production/release-token",
                              headers=ws["owner"])
    assert token.status_code == 200, token.text
    return token.json()["release_token"]


async def _release(client, token: str, commit: str, manifest: dict, branch: str = "main"):
    return await client.post("/v1/deploys/releases", headers={"X-Veynod-Release-Token": token},
                             json={"commit": commit, "branch": branch, "manifest": manifest})


async def _answer(socket) -> dict:
    """The deploy computer's two receipts for the next poke; returns the poke."""
    poke = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
    while poke["type"] != "task.poke":
        poke = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
    await socket.send(json.dumps({"type": "task.ack.net", "frame_id": poke["frame_id"],
                                  "payload": {"received_at_ms": 0}}))
    await socket.send(json.dumps({"type": "task.ack.exec", "frame_id": poke["frame_id"],
                                  "payload": {"worker_pid": 4242}}))
    return poke


async def _idle(socket) -> None:
    """The messenger's ping after its worker exits: no placement runs here now."""
    await socket.send(json.dumps({"type": "sys.ping", "frame_id": uuid.uuid4().hex,
                                  "payload": {"active_count": 0, "slots": 1}}))
    while json.loads(await asyncio.wait_for(socket.recv(), timeout=5))["type"] != "sys.pong":
        pass


async def _deploys(client, ws: dict, project: str) -> dict:
    got = await client.get(f"/v1/projects/{project}/deploys", headers=ws["owner"])
    assert got.status_code == 200, got.text
    return got.json()


@integration
async def test_only_a_person_turns_deploys_on_and_it_needs_a_computer(client) -> None:
    from tests.test_scheduler import _workspace

    ws = await _workspace(client, "full")
    project = await _project(client, ws)
    host = await _deploy_host(client, ws, f"deploy-{uuid.uuid4().hex[:4]}")
    path = f"/v1/projects/{project}/deploys/production"

    agent = await client.put(path, headers=host["headers"], json={"enabled": True})
    assert agent.status_code == 403, agent.text
    no_host = await client.put(path, headers=ws["owner"], json={"enabled": True})
    assert no_host.status_code == 422 and "host_id" in no_host.text
    no_window = await client.put(path, headers=ws["owner"], json={"trigger": "scheduled"})
    assert no_window.status_code == 422 and "schedule" in no_window.text

    made = await client.put(path, headers=ws["owner"], json={
        "enabled": True, "host_id": host["host_id"], "trigger": "scheduled",
        "schedule": {"start": "02:00", "end": "04:00"}, "health_url": "https://example.com/ok"})
    assert made.status_code == 200, made.text
    body = made.json()
    assert body["enabled"] and body["trigger"] == "scheduled"
    assert body["schedule"]["days"] == [1, 2, 3, 4, 5, 6, 7]
    assert body["computer"]["state"] == "online" and not body["has_release_token"]

    off = await client.put(path, headers=ws["owner"], json={"enabled": False})
    assert off.json()["enabled"] is False and off.json()["trigger"] == "scheduled"
    # The agent can read the settings, never the token.
    read = await client.get(f"/v1/projects/{project}/deploys", headers=host["headers"])
    assert read.status_code == 200 and "release_token" not in json.dumps(read.json()).replace(
        "has_release_token", "")


@integration
async def test_a_deploy_computer_takes_no_other_work(client) -> None:
    from tests.test_scheduler import _schedule, _task, _workspace

    ws = await _workspace(client, "full")
    await _deploy_host(client, ws, f"deploy-{uuid.uuid4().hex[:4]}")
    await _task(client, ws, repo=None)
    result = await _schedule(client, ws)
    assert result["placed"] == 0 and result["hosts"] == 0, result


@integration
async def test_a_release_deploys_over_nerve_and_only_changed_services_restart(
        client, api_base_url) -> None:
    from tests.test_nerve_dispatch import _connect
    from tests.test_scheduler import _workspace

    ws = await _workspace(client, "full")
    project = await _project(client, ws)
    host = await _deploy_host(client, ws, f"deploy-{uuid.uuid4().hex[:4]}")
    token = await _target(client, ws, project, host)

    refused = await _release(client, "vdr_wrong", "a" * 40, _manifest(api="a"))
    assert refused.status_code == 401
    socket = await _connect(api_base_url, host)
    try:
        responder = asyncio.create_task(_answer(socket))
        first = await _release(client, token, "a" * 40, _manifest(api="a", kernel="d"))
        assert first.status_code == 201, first.text
        assert first.json()["queued"][0]["queued"] is True
        poke = await responder
        assert poke["payload"]["step"] == "deploy"
        placement_id = poke["payload"]["placement_id"]

        detail = (await client.get(f"/v1/runner/placements/{placement_id}",
                                   headers=host["headers"])).json()
        assert detail["step"] == "deploy" and detail["detail"]["release"]["commit"] == "a" * 40
        assert detail["detail"]["plan"]["changed"] == ["api", "kernel"]

        report = f"/v1/runner/placements/{placement_id}/deploy-report"
        running = await client.post(report, headers=host["headers"], json={"status": "running"})
        assert running.status_code == 200 and running.json()["status"] == "running", running.text
        done = await client.post(report, headers=host["headers"], json={
            "status": "deployed", "note": "2 services up", "log": ["pulled api", "pulled kernel"],
            "services": {"api": "restarted", "kernel": "restarted"}, "health": "ok"})
        assert done.status_code == 200 and done.json()["status"] == "deployed", done.text
        again = await client.post(report, headers=host["headers"], json={"status": "failed"})
        assert again.json()["status"] == "deployed"  # a retried report changes nothing

        # The second release changes only the api: only the api restarts.
        await _idle(socket)
        responder = asyncio.create_task(_answer(socket))
        second = await _release(client, token, "b" * 40, _manifest(api="b", kernel="d"))
        assert second.json()["seq"] == 2
        poke = await responder
        detail = (await client.get(f"/v1/runner/placements/{poke['payload']['placement_id']}",
                                   headers=host["headers"])).json()
        assert detail["detail"]["plan"] == {"changed": ["api"], "unchanged": ["kernel"],
                                            "removed": []}
        assert detail["detail"]["previous"]["commit"] == "a" * 40
    finally:
        await socket.close()

    view = await _deploys(client, ws, project)
    [target] = view["targets"]
    assert target["deployed"]["commit"] == "a" * 40
    assert [run["status"] for run in target["runs"]] == ["offered", "deployed"]
    assert [item["seq"] for item in view["releases"]] == [2, 1]


@integration
async def test_the_gate_branch_monotonic_one_in_flight_and_coalesce(client) -> None:
    from tests.test_scheduler import _workspace

    ws = await _workspace(client, "full")
    project = await _project(client, ws)
    # No socket: the run is offered and stays in flight, which is what the gate needs.
    host = await _deploy_host(client, ws, f"deploy-{uuid.uuid4().hex[:4]}")
    token = await _target(client, ws, project, host)

    other = await _release(client, token, "f" * 40, _manifest(api="f"), branch="feature/x")
    assert other.json()["queued"] == []  # an auto target deploys its own branch only
    one = (await _release(client, token, "1" * 40, _manifest(api="1"))).json()
    assert one["queued"][0]["queued"] is True
    two = (await _release(client, token, "2" * 40, _manifest(api="2"))).json()
    three = (await _release(client, token, "3" * 40, _manifest(api="3"))).json()
    assert two["queued"][0]["queued"] and not two["queued"][0].get("coalesced")
    assert three["queued"][0]["coalesced"] is True
    assert three["queued"][0]["run_id"] == two["queued"][0]["run_id"]
    # CI retrying the same post is the same release.
    retry = (await _release(client, token, "3" * 40, _manifest(api="3"))).json()
    assert retry["seq"] == three["seq"] and retry["queued"][0]["reason"] == "newer_waiting"
    # An older release never queues over a newer waiting one.
    late = (await _release(client, token, "2" * 40, _manifest(api="2"))).json()
    assert late["queued"][0]["queued"] is False

    runs = (await _deploys(client, ws, project))["targets"][0]["runs"]
    assert [(run["status"], run["commit"]) for run in runs] == [
        ("queued", "3" * 40), ("offered", "1" * 40)]


@integration
async def test_a_failed_deploy_waits_for_a_person_and_is_not_retried_by_a_timer(
        client, api_base_url) -> None:
    from tests.test_nerve_dispatch import _connect
    from tests.test_scheduler import _workspace

    ws = await _workspace(client, "full")
    project = await _project(client, ws)
    host = await _deploy_host(client, ws, f"deploy-{uuid.uuid4().hex[:4]}")
    token = await _target(client, ws, project, host)
    socket = await _connect(api_base_url, host)
    try:
        responder = asyncio.create_task(_answer(socket))
        await _release(client, token, "c" * 40, _manifest(api="c"))
        poke = await responder
        failed = await client.post(
            f"/v1/runner/placements/{poke['payload']['placement_id']}/deploy-report",
            headers=host["headers"],
            json={"status": "rolled_back", "note": "health check failed; back on the old images"})
        assert failed.json()["status"] == "rolled_back" and failed.json()["needs_you"]

        overview = (await client.get("/v1/loop/overview", headers=ws["owner"])).json()
        waiting = overview["needs_you"].get("deploys") or []
        assert [item["commit"] for item in waiting] == ["c" * 40], overview["needs_you"]

        # CI posting the same release again does not retry it.
        again = (await _release(client, token, "c" * 40, _manifest(api="c"))).json()
        assert again["queued"][0]["reason"] == "failed_before"

        # A person can: deploy that commit by hand.
        await _idle(socket)
        responder = asyncio.create_task(_answer(socket))
        manual = await client.post(f"/v1/projects/{project}/deploys/production/run",
                                   headers=ws["owner"], json={})
        assert manual.status_code == 200 and manual.json()["queued"], manual.text
        await responder
        agent = await client.post(f"/v1/projects/{project}/deploys/production/run",
                                  headers=host["headers"], json={})
        assert agent.status_code == 403

        seen = await client.post(f"/v1/deploys/runs/{failed.json()['run_id']}/acknowledge",
                                 headers=ws["owner"])
        assert seen.status_code == 200 and not seen.json()["needs_you"]
    finally:
        await socket.close()
