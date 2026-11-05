"""One computer, one identity, one hub (board task up-hub-p1-up-hub).

The board doctor says what the Loop is missing, then goes green. The real
`veynod hub` puts the computer on the Loop over its nerve socket, the way
`veynod up --run-tasks` leaves it, and builds a task with nobody calling the
API. A computer never reviews its own work: with no other computer, the review
waits for the person under "needs you", and the person's approval finishes it.
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
    assert checks["hosts"]["ok"] is False and "npx veynod --run-tasks" in checks["hosts"]["fix"]
    assert checks["supervisor"]["required"] is False

    changed = await client.put("/v1/human/automation", headers=ws["owner"], json={"mode": "full"})
    assert changed.status_code == 200, changed.text
    builder = await _host(client, ws, f"build-{uuid.uuid4().hex[:4]}")
    # One builder is enough: the Kernel gate reviews code and a person reviews
    # what no other computer can, so a reviewer computer is never required.
    await _task(client, ws, repo="acme/elsewhere")
    green = (await client.get("/v1/runner/doctor", headers=builder["headers"])).json()
    checks = _checks(green)
    assert checks["automation"]["ok"] and checks["hosts"]["ok"] and checks["builder"]["ok"]
    assert checks["reviewer"]["ok"] and checks["reviewer"]["required"] is False
    # A task on a repository no computer works on is a warning, not a blocker.
    assert checks["repos"]["ok"] is False and "acme/elsewhere" in checks["repos"]["text"]
    assert green["ok"] is True, green

    # The same answer over MCP.
    from app.mcp_server import server as mcp_server

    assert "doctor" in mcp_server._LOOP_ACTIONS


async def test_a_host_that_cannot_pin_its_sessions_is_named_by_the_doctor(client):
    ws = await _workspace(client, "full")
    agent = await _agent(client, ws, f"codex-{uuid.uuid4().hex[:4]}", "EXECUTOR")

    async def hello(report):
        said = await client.post("/v1/runner/hello", headers=agent["headers"], json={"host": {
            "os": "linux", "arch": "x64", "max_concurrency": 1, "repos": [],
            "runtimes": [{"id": "codex", "available": True}], "capabilities_report": report}})
        assert said.status_code == 200, said.text
        hosts = (await client.get("/v1/runner/hosts", headers=ws["owner"])).json()["items"]
        return next(host for host in hosts if host["agent_id"] == agent["agent_id"])

    async def pinning() -> dict:
        board = await client.get("/v1/runner/doctor", headers=ws["owner"])
        return _checks(board.json())["pinning"]

    host = await hello({"mcp_pinning": False, "runtime": "codex", "secret": "agt_x"})
    assert host["capabilities_report"] == {"mcp_pinning": False, "runtime": "codex"}
    red = await pinning()
    assert red["ok"] is False and red["required"] is False
    assert agent["agent_id"] in red["text"] and "Node 24" in red["fix"]

    await hello({"mcp_pinning": True, "runtime": "oar:codex"})
    green = await pinning()
    assert green["ok"] is True and green["fix"] is None


def _home(tmp: Path, host: str, agent: dict, script: Path) -> Path:
    """What `veynod up --run-tasks` leaves on a computer: one config, one key file."""
    home = tmp / "home"
    keys = home / ".veynod" / "keys"
    keys.mkdir(parents=True)
    key_file = keys / f"{agent['agent_id']}.key"
    key_file.write_text(agent["key"] + "\n")
    key_file.chmod(0o600)
    config = home / ".veynod" / "config.json"
    config.write_text(json.dumps({
        "version": 1, "host": host, "run_tasks": True, "runtime": "custom",
        "exec": f'"{sys.executable}" "{script}"', "slots": 1, "repos": [],
        "identities": [{"role": "computer", "agent_id": agent["agent_id"],
                        "key_file": str(key_file)}]}))
    config.chmod(0o600)
    return home


def _env(home: Path, log: Path) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith("VEYNOD_")}
    return {**env, "HOME": str(home), "VEYNOD_E2E_LOG": str(log)}


async def _until(client, ws: dict, key: str, wanted: str) -> str:
    status = None
    for _ in range(120):
        status = (await _task_row(client, ws, key))["status"]
        if status == wanted:
            break
        await asyncio.sleep(0.5)
    return status


async def _person_reviews(client, ws: dict, key: str) -> None:
    """With one computer the review is the person's: it says so, then they approve."""
    overview = (await client.get("/v1/loop/overview", headers=ws["owner"])).json()
    waiting = {item["key"]: item for item in overview["needs_you"]["waiting"]}
    for _ in range(20):
        if key in waiting:
            break
        await asyncio.sleep(0.5)
        overview = (await client.get("/v1/loop/overview", headers=ws["owner"])).json()
        waiting = {item["key"]: item for item in overview["needs_you"]["waiting"]}
    assert waiting[key]["why"] == "review", overview["needs_you"]
    approved = await client.post(f"/v1/tasks/{key}/review", headers=ws["owner"],
                                 json={"decision": "approve"})
    assert approved.status_code == 200, approved.text


async def test_one_hub_builds_over_its_nerve_socket_and_the_person_reviews(client, api_base_url):
    ws = await _workspace(client, "full")
    agent = await _agent(client, ws, f"box-{uuid.uuid4().hex[:6]}", "EXECUTOR")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        script = tmp_path / "step.py"
        script.write_text(STEP_SCRIPT)
        log = tmp_path / "log"
        log.mkdir()
        home = _home(tmp_path, api_base_url, agent, script)
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
                if hosts:
                    break
                await asyncio.sleep(0.5)
            assert [host["agent_id"] for host in hosts] == [agent["agent_id"]], hosts

            # A second hub on the same key is refused, so two sockets never fight.
            second = subprocess.run(["node", str(CLI), "hub"], env=env, capture_output=True,
                                    text=True, timeout=60)
            assert second.returncode != 0
            assert "another hub" in second.stdout + second.stderr

            key = await _task(client, ws, repo=None)
            status = await _until(client, ws, key, "REVIEWING")
            assert status == "REVIEWING", (status, hub.stderr.read() if hub.poll() is not None
                                           else "hub running")
            await _person_reviews(client, ws, key)
            assert await _until(client, ws, key, "DONE") == "DONE"

            after = subprocess.run(["node", str(CLI), "doctor"], env=env, capture_output=True,
                                   text=True, timeout=60)
            assert after.returncode == 0, after.stdout + after.stderr
        finally:
            hub.terminate()
            try:
                hub.wait(timeout=10)
            except subprocess.TimeoutExpired:
                hub.kill()

        work = json.loads((log / "work.json").read_text())
        assert work["key_tail"] == agent["key"][-6:]
        # The hub let go of its lock when it stopped. Windows terminates a
        # process without running exit handlers, so a stale file may remain;
        # the next hub checks the pid and overwrites it.
        if sys.platform != "win32":
            assert not list((home / ".veynod").glob("hub-*.lock"))


async def test_veynod_up_connects_one_identity_then_the_loop_runs(client, api_base_url):
    """The release bar (D-3) without the computer: one `veynod up --run-tasks`, one
    approval, the hub, the doctor green and one task DONE with only the person's review."""
    import re

    ws = await _workspace(client, "full")
    name = f"box-{uuid.uuid4().hex[:6]}"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        script = tmp_path / "step.py"
        script.write_text(STEP_SCRIPT)
        log = tmp_path / "log"
        log.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        env = _env(home, log)
        up = await asyncio.create_subprocess_exec(
            "node", str(CLI), "up", "--run-tasks", "--yes", "--name", name, "--host", api_base_url,
            "--client", "print", "--no-open", "--no-service", "--runtime", "custom",
            "--exec", f'"{sys.executable}" "{script}"',
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        hub = None
        printed: list[str] = []
        try:
            code = None
            while code is None:
                line = (await asyncio.wait_for(up.stdout.readline(), 30)).decode()
                assert line, "".join(printed)
                printed.append(line)
                found = re.search(r"Your code\s+([A-Z0-9]{4}-[A-Z0-9]{4})", line)
                code = found.group(1) if found else None

            view = (await client.get(f"/v1/auth/cli/sessions/{code}", headers=ws["owner"])).json()
            assert [(item["name"], item["role_scope"]) for item in view["identities"]] == [
                (name, "EXECUTOR")]
            approved = await client.post(f"/v1/auth/cli/sessions/{code}/approve",
                                         headers=ws["owner"],
                                         json={"agent_signature": name, "also": []})
            assert approved.status_code == 200, approved.text

            config_file = home / ".veynod" / "config.json"
            for _ in range(60):
                if config_file.exists():
                    break
                await asyncio.sleep(0.5)
            config = json.loads(config_file.read_text())
            assert [(item["role"], item["agent_id"]) for item in config["identities"]] == [
                ("computer", name)]
            if sys.platform != "win32":
                assert all((Path(item["key_file"]).stat().st_mode & 0o777) == 0o600
                           for item in config["identities"])
            hub = subprocess.Popen(["node", str(CLI), "hub"], env=env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True)
            rest, _ = await asyncio.wait_for(up.communicate(), 90)
            output = "".join(printed) + rest.decode()
            assert up.returncode == 0, output

            key = await _task(client, ws, repo=None)
            assert await _until(client, ws, key, "REVIEWING") == "REVIEWING", output
            await _person_reviews(client, ws, key)
            assert await _until(client, ws, key, "DONE") == "DONE"
        finally:
            if up.returncode is None:
                up.kill()
                await up.wait()
            if hub is not None:
                hub.terminate()
                try:
                    hub.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    hub.kill()
