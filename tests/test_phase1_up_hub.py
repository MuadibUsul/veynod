"""Phase 1: one hub per computer and the doctor (board task up-hub-p1-up-hub).

The board doctor says what the Loop is missing, then goes green. The real
`veynod hub` serves both of a computer's identities (builder and reviewer) from
one process and one config, the way `veynod up --run-tasks` leaves them, and
takes a task to DONE with nobody calling the API. `veynod doctor` is then green.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from tests.test_loop_production import CLI, STEP_SCRIPT, _agent
from tests.test_scheduler import _host, _task, _task_row, _workspace

pytestmark = pytest.mark.integration


def _checks(body: dict) -> dict:
    return {item["id"]: item for item in body["checks"]}


async def test_the_board_doctor_names_what_is_missing_until_the_loop_can_run(client):
    ws = await _workspace(client, "semi")
    empty = await client.get("/v1/runner/doctor", headers=ws["owner"])
    assert empty.status_code == 200, empty.text
    body = empty.json()
    assert body["ok"] is False and body["mode"] == "semi"
    checks = _checks(body)
    assert checks["automation"]["ok"] is False and checks["automation"]["fix"]
    assert checks["hosts"]["ok"] is False and "veynod up" in checks["hosts"]["fix"]
    assert checks["supervisor"]["required"] is False

    changed = await client.put("/v1/human/automation", headers=ws["owner"], json={"mode": "full"})
    assert changed.status_code == 200, changed.text
    builder = await _host(client, ws, f"build-{uuid.uuid4().hex[:4]}")
    halfway = (await client.get("/v1/runner/doctor", headers=builder["headers"])).json()
    checks = _checks(halfway)
    assert checks["automation"]["ok"] and checks["hosts"]["ok"] and checks["builder"]["ok"]
    assert checks["reviewer"]["ok"] is False, "a builder alone cannot finish a task"
    assert halfway["ok"] is False

    # A task on a repository no computer works on is a warning, not a blocker.
    await _task(client, ws, repo="acme/elsewhere")
    reviewer = await _agent(client, ws, f"review-{uuid.uuid4().hex[:4]}", "AUDITOR")
    hello = await client.post("/v1/runner/hello", headers=reviewer["headers"], json={"host": {
        "os": "linux", "arch": "x64", "max_concurrency": 1, "repos": [],
        "runtimes": [{"id": "custom", "available": True}]}})
    assert hello.status_code == 200, hello.text
    green = (await client.get("/v1/runner/doctor", headers=ws["owner"])).json()
    checks = _checks(green)
    assert checks["reviewer"]["ok"] and checks["reviewer"]["fix"] is None
    assert checks["repos"]["ok"] is False and "acme/elsewhere" in checks["repos"]["text"]
    assert green["ok"] is True, green

    # The same answer over MCP.
    from app.mcp_server import server as mcp_server

    assert "doctor" in mcp_server._LOOP_ACTIONS


def _home(tmp: Path, host: str, builder: dict, reviewer: dict, script: Path) -> Path:
    """What `veynod up --run-tasks` leaves on a computer: one config, one key file each."""
    home = tmp / "home"
    keys = home / ".veynod" / "keys"
    keys.mkdir(parents=True)
    identities = []
    for role, agent in (("computer", builder), ("review", reviewer)):
        key_file = keys / f"{agent['agent_id']}.key"
        key_file.write_text(agent["key"] + "\n")
        key_file.chmod(0o600)
        identities.append({"role": role, "agent_id": agent["agent_id"],
                           "key_file": str(key_file)})
    config = home / ".veynod" / "config.json"
    config.write_text(json.dumps({
        "version": 1, "host": host, "run_tasks": True, "runtime": "custom",
        "exec": f'"{sys.executable}" "{script}"', "slots": 1, "repos": [],
        "identities": identities}))
    config.chmod(0o600)
    return home


def _env(home: Path, log: Path) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith("VEYNOD_")}
    return {**env, "HOME": str(home), "VEYNOD_E2E_LOG": str(log)}


async def test_one_hub_serves_both_identities_and_the_doctor_goes_green(client, api_base_url):
    ws = await _workspace(client, "full")
    suffix = uuid.uuid4().hex[:6]
    builder = await _agent(client, ws, f"box-{suffix}", "EXECUTOR")
    reviewer = await _agent(client, ws, f"box-{suffix}-review", "AUDITOR")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        script = tmp_path / "step.py"
        script.write_text(STEP_SCRIPT)
        log = tmp_path / "log"
        log.mkdir()
        home = _home(tmp_path, api_base_url, builder, reviewer, script)
        env = _env(home, log)

        # Before the hub runs, the doctor is red: the board has no computer yet.
        before = subprocess.run(["node", str(CLI), "doctor"], env=env, capture_output=True,
                                text=True, timeout=60)
        assert before.returncode == 1, before.stdout + before.stderr

        hub = subprocess.Popen(["node", str(CLI), "hub"], env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(60):
                hosts = (await client.get("/v1/runner/hosts", headers=ws["owner"])).json()["items"]
                if len(hosts) == 2:
                    break
                await asyncio.sleep(0.5)
            assert {host["agent_id"] for host in hosts} == {
                builder["agent_id"], reviewer["agent_id"]}, hosts

            # A second hub on the same keys is refused, so two streams never fight.
            second = subprocess.run(["node", str(CLI), "hub"], env=env, capture_output=True,
                                    text=True, timeout=60)
            assert second.returncode != 0
            assert "another hub" in second.stdout + second.stderr

            key = await _task(client, ws, repo=None)
            status = None
            for _ in range(120):
                status = (await _task_row(client, ws, key))["status"]
                if status == "DONE":
                    break
                await asyncio.sleep(0.5)
            assert status == "DONE", (status, hub.stderr.read() if hub.poll() is not None
                                      else "hub running")

            after = subprocess.run(["node", str(CLI), "doctor"], env=env, capture_output=True,
                                   text=True, timeout=60)
            assert after.returncode == 0, after.stdout + after.stderr
        finally:
            hub.terminate()
            try:
                hub.wait(timeout=10)
            except subprocess.TimeoutExpired:
                hub.kill()

        task = await _task_row(client, ws, key)
        # reviewed_by names the reviewer's session (`<agent>-v1`), never the builder's.
        assert task["reviewed_by"].startswith(reviewer["agent_id"]), task["reviewed_by"]
        work = json.loads((log / "work.json").read_text())
        review = json.loads((log / "review.json").read_text())
        assert work["key_tail"] == builder["key"][-6:]
        assert review["key_tail"] == reviewer["key"][-6:]
        # The hub let go of its locks when it stopped.
        assert not list((home / ".veynod").glob("hub-*.lock"))
