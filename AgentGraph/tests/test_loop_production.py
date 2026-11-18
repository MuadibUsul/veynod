"""The Loop end to end with the real runner program (up-hub-p0-cli, WAKE_AND_DISPATCH §16).

Two real `veynod runner` processes, a builder (EXECUTOR) and a reviewer (AUDITOR),
follow the real server. A task with no repository is placed on the builder, worked
in a scratch directory, handed off, placed on the reviewer for review and approved,
with nobody calling the API in between. The runtime is `--runtime custom`: a script
that does what an agent session would do over REST, with the runner's own key.
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

from tests.test_scheduler import ADMIN, _placements, _task, _task_row, _workspace

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "cli" / "bin" / "veynod.mjs"

# What an agent session does for each step, with the key the runner gave it.
STEP_SCRIPT = r'''
import json, os, urllib.request

host, key = os.environ["VEYNOD_HOST"], os.environ["VEYNOD_KEY"]
task, step = os.environ["VEYNOD_TASK_KEY"], os.environ["VEYNOD_STEP"]


def call(method, path, body=None):
    request = urllib.request.Request(
        host + path, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read() or b"{}")


with open(os.path.join(os.environ["VEYNOD_E2E_LOG"], f"{step}.json"), "w") as log:
    json.dump({"cwd": os.getcwd(), "key_tail": key[-6:]}, log)
if step == "work":
    claimed = call("POST", f"/v1/tasks/{task}/claim", {})
    call("POST", f"/v1/tasks/{task}/handoff", {
        "next_status": "REVIEWING", "lease_epoch": claimed["task"]["lease_epoch"],
        "brief": {"summary": "done by the runner", "key_metrics": {},
                  "unverified_hypotheses": [], "risk_flag": False}})
elif step == "review":
    call("POST", f"/v1/tasks/{task}/review", {"decision": "approve", "feedback": "checked"})
'''


async def _agent(client, ws: dict, name: str, role: str) -> dict:
    minted = await client.post(f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
                               json={"role_scope": role, "allowed_namespaces": ["*"]})
    assert minted.status_code == 201, minted.text
    key = minted.json()["api_key"]
    headers = {"Authorization": f"Bearer {key}"}
    named = await client.post("/v1/agents/register", headers=headers, json={"agent_id": name})
    assert named.status_code in (200, 201), named.text
    return {"key": key, "headers": headers, "agent_id": name}


def _runner(base_url: str, key: str, script: Path, log: Path, state: Path) -> subprocess.Popen:
    env = {**os.environ, "VEYNOD_STATE_DIR": str(state), "VEYNOD_E2E_LOG": str(log)}
    return subprocess.Popen(
        ["node", str(CLI), "runner", "--host", base_url, "--key", key, "--runtime", "custom",
         "--exec", f'"{sys.executable}" "{script}"', "--slots", "1"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


async def test_runner_cli_e2e_custom_runtime(client, api_base_url):
    ws = await _workspace(client, "full")
    suffix = uuid.uuid4().hex[:6]
    builder = await _agent(client, ws, f"build-{suffix}", "EXECUTOR")
    reviewer = await _agent(client, ws, f"review-{suffix}", "AUDITOR")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        script = tmp_path / "step.py"
        script.write_text(STEP_SCRIPT)
        logs = {name: tmp_path / f"log-{name}" for name in ("build", "review")}
        for path in logs.values():
            path.mkdir()
        procs = [
            _runner(api_base_url, agent["key"], script, logs[name], tmp_path / f"state-{name}")
            for name, agent in (("build", builder), ("review", reviewer))
        ]
        try:
            # Both computers say hello on their own.
            for _ in range(60):
                hosts = (await client.get("/v1/runner/hosts", headers=ws["owner"])).json()["items"]
                if len(hosts) == 2:
                    break
                await asyncio.sleep(0.5)
            assert len(hosts) == 2, hosts
            by_agent = {host["agent_id"]: host for host in hosts}

            # From here nobody calls the API: the server's own scheduling pass (every
            # reaper cycle) places each step and the runners do the rest.
            key = await _task(client, ws, repo=None)
            status = None
            for _ in range(120):
                status = (await _task_row(client, ws, key))["status"]
                if status == "DONE":
                    break
                await asyncio.sleep(0.5)
            errors = [p.stderr.read() if p.poll() is not None else "running" for p in procs]
            assert status == "DONE", (status, errors)
        finally:
            for proc in procs:
                proc.terminate()
            for proc in procs:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()

        task = await _task_row(client, ws, key)
        # The work was claimed with the builder's own key (a claim by anyone else is
        # refused: the placement assigned the task to the builder's agent). Under
        # full automation its hand-off ended the task: no review was placed on the
        # other computer and nobody had to approve it (docs/LOOP_SIMPLE_MODEL.md).
        assert task["reviewed_by"] is None, task["reviewed_by"]
        work = json.loads((logs["build"] / "work.json").read_text())
        assert work["key_tail"] == builder["key"][-6:]
        assert not (logs["review"] / "review.json").exists()
        # Repo-less work runs in the runner's scratch directory, never the service's cwd.
        assert work["cwd"].startswith(str(tmp_path / "state-build" / "runner" / "scratch")), work
        assert not Path(work["cwd"]).exists()

        # The placement catches up on the next scheduling pass.
        steps: list = []
        for _ in range(40):
            steps = sorted((item["step"], item["status"], item["host_id"])
                           for item in await _placements(client, ws, key))
            if all(status == "delivered" for _, status, _ in steps):
                break
            await asyncio.sleep(0.5)
        assert steps == [("work", "delivered", by_agent[builder["agent_id"]]["host_id"])], steps
