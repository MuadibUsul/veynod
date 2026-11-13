"""Does the Loop run unattended? One computer, a mixed board, nobody touching it.

This is the question the audit of 2026-11-11 asked (docs/LOOP_UNATTENDED_AUDIT.md),
answered with the real server, the real CLI hub on its nerve socket and a
stand-in for the agent that only does what an agent session does: claim the
task, then hand it off. After the board is set up nobody calls the API except
to read it.

The board holds the kinds of work that stopped in production:

* plain work, an ANALYST task, a task that names a capability (``frontend``);
* a chain where the second task waits on the first;
* work whose author asked for a person's look (MANUAL acceptance), and work the
  agent flags as risky: both must finish and be flagged, not wait;
* an agent run that fails once and succeeds on the next attempt;
* a task the scheduler stopped after three failed rounds, days ago;
* a task no computer here may take (``required_role`` AUDITOR), which the
  supervisor must be given a round about, and the round must finish.

Not covered: tasks with a repository (they go through the Kernel, which is not
running here).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import pytest

from tests.conftest import run_server
from tests.test_loop_reliability import _fetch
from tests.test_scheduler import ADMIN, PASSWORD, _sql

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "cli" / "bin" / "veynod.mjs"

AGENT = r'''
import json, os, sys, urllib.request

host, key = os.environ["VEYNOD_HOST"], os.environ["VEYNOD_KEY"]
task, step = os.environ["VEYNOD_TASK_KEY"], os.environ["VEYNOD_STEP"]
log = os.environ["VEYNOD_E2E_LOG"]


def call(method, path, body=None):
    request = urllib.request.Request(
        host + path, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read() or b"{}")


with open(os.path.join(log, "steps.jsonl"), "a") as out:
    out.write(json.dumps({"step": step, "task": task}) + "\n")
if step != "work":
    sys.exit(0)  # a supervise round has nothing to change on this board
row = call("GET", f"/v1/tasks/{task}")
marker = os.path.join(log, f"failed-{task}")
if "[flaky]" in row["title"] and not os.path.exists(marker):
    open(marker, "w").close()
    sys.exit(1)  # the agent crashed; the runner tries again
claimed = call("POST", f"/v1/tasks/{task}/claim", {})
call("POST", f"/v1/tasks/{task}/handoff", {
    "next_status": "REVIEWING", "lease_epoch": claimed["task"]["lease_epoch"],
    "brief": {"summary": "done", "key_metrics": {}, "unverified_hypotheses": [],
              "risk_flag": "[risky]" in row["title"]}})
'''


async def _board(client: httpx.AsyncClient) -> dict:
    created = (await client.post("/v1/admin/workspaces", headers=ADMIN, json={
        "name": f"unattended-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
    wid = created["workspace"]["id"]
    email = f"owner-{uuid.uuid4().hex[:8]}@example.test"
    await client.post(f"/v1/admin/workspaces/{wid}/human-users", headers=ADMIN, json={
        "email": email, "display_name": "Owner", "role": "OWNER", "password": PASSWORD})
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    owner = {"Authorization": f"Bearer {login.json()['token']}"}
    assert (await client.put("/v1/human/automation", headers=owner,
                             json={"mode": "full"})).status_code == 200
    return {"id": wid, "owner": owner}


async def _task(client, ws: dict, title: str, **fields) -> str:
    key = f"u-{uuid.uuid4().hex[:8]}"
    made = await client.post("/v1/tasks", headers=ws["owner"], json={
        "task_key": key, "title": title, "assigned_role": "EXECUTOR", "namespace": "default",
        **fields})
    assert made.status_code == 201, made.text
    return key


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
async def test_one_computer_works_a_mixed_board_with_nobody_touching_it() -> None:
    env = {"LEASE_REAPER_INTERVAL_SECONDS": "5", "RUNNER_SUPERVISE_EVERY_SECONDS": "60"}
    async with run_server(env, ".server-unattended.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
            await _run(client, base_url)


async def _run(client: httpx.AsyncClient, base_url: str) -> None:
    ws = await _board(client)
    minted = await client.post(f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
                               json={"role_scope": "EXECUTOR", "allowed_namespaces": ["*"]})
    key = minted.json()["api_key"]
    name = f"pc-{uuid.uuid4().hex[:4]}"
    registered = await client.post("/v1/agents/register", headers={
        "Authorization": f"Bearer {key}"}, json={"agent_id": name, "capabilities": ["supervisor"]})
    assert registered.status_code in (200, 201), registered.text

    tasks = {
        "plain": await _task(client, ws, "Write the changelog entry"),
        "analyst": await _task(client, ws, "Compare three vendors", assigned_role="ANALYST"),
        "frontend": await _task(client, ws, "Mobile shell",
                                required_capabilities=["frontend"]),
        "manual": await _task(client, ws, "Draft the landing copy", acceptance=[
            {"id": "tone", "text": "the owner likes the tone", "kind": "MANUAL"}]),
        "risky": await _task(client, ws, "Migrate the settings [risky]"),
        "flaky": await _task(client, ws, "Rename the field [flaky]"),
    }
    # Judged by the platform, not a person: a command the computer runs, and a
    # document the stand-in agent never writes.
    tasks["checked"] = await _task(client, ws, "Fix the parser", acceptance=[
        {"id": "tests", "text": "the tests pass", "kind": "AUTO",
         "check": {"type": "command", "run": f'"{sys.executable}" -c "print(1)"'}}])
    tasks["missing_doc"] = await _task(client, ws, "Write the plan document", acceptance=[
        {"id": "plan", "text": "the plan is written", "kind": "AUTO",
         "check": {"type": "document_exists", "doc_key": "plans/never-written"}}])
    tasks["first"] = await _task(client, ws, "Collect the requirements")
    tasks["second"] = await _task(client, ws, "Write the plan", depends_on=[tasks["first"]],
                                  status="TODO")
    tasks["stopped"] = await _task(client, ws, "Stopped three times last week")
    stopped_id = (await _fetch("SELECT id FROM collab_tasks WHERE task_key = $1",
                               tasks["stopped"]))[0]["id"]
    for _ in range(3):
        await _sql(
            "INSERT INTO runner_placements (workspace_id, task_id, task_key, mode, status, step, "
            "reason, created_at, ended_at) VALUES ($1::uuid, $2, $3, 'assign', 'failed', 'work', "
            "'not_claimed', NOW() - INTERVAL '5 days', NOW() - INTERVAL '5 days')",
            ws["id"], stopped_id, tasks["stopped"])
    await _sql("UPDATE collab_tasks SET status = 'HANDOFF' WHERE task_key = $1", tasks["stopped"])
    # Nobody here may take this one: the supervisor is told, in a round that must finish.
    nobody = await _task(client, ws, "Audit the payments", assigned_role="AUDITOR",
                         required_role="AUDITOR")

    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp, "agent.py")
        script.write_text(AGENT)
        log = Path(tmp, "log")
        log.mkdir()
        hub = subprocess.Popen(
            ["node", str(CLI), "runner", "--host", base_url, "--key", key, "--runtime", "custom",
             "--exec", f'"{sys.executable}" "{script}"', "--slots", "1"],
            env={**os.environ, "VEYNOD_STATE_DIR": str(Path(tmp, "state")),
                 "VEYNOD_E2E_LOG": str(log)},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        started = time.monotonic()
        try:
            statuses: dict[str, str] = {}
            rounds: list[dict] = []
            all_done_at = None
            for _ in range(360):
                rows = (await client.get("/v1/tasks", headers=ws["owner"], params={
                    "task_key": ",".join(tasks.values()), "detail": "summary"})).json()["items"]
                statuses = {row["task_key"]: row["status"] for row in rows}
                if all_done_at is None and all(status == "DONE" for status in statuses.values()):
                    all_done_at = time.monotonic() - started
                rounds = [item for item in (await client.get(
                    "/v1/runner/placements", headers=ws["owner"],
                    params={"step": "supervise"})).json()["items"] if item["host_id"]]
                # Run on past the last task: a supervise round that keeps seeing the
                # same unchangeable agenda must not be started again and again.
                if all_done_at is not None and time.monotonic() - started > all_done_at + 150:
                    break
                assert hub.poll() is None, hub.stderr.read()
                await asyncio.sleep(1)
            elapsed = time.monotonic() - started
        finally:
            hub.terminate()
            try:
                hub.wait(timeout=10)
            except subprocess.TimeoutExpired:
                hub.kill()
        steps = [json.loads(line) for line in Path(log, "steps.jsonl").read_text().splitlines()]

    named = {label: statuses.get(key) for label, key in tasks.items()}
    assert all(status == "DONE" for status in named.values()), named
    finished_rounds = [item for item in rounds if item["reported"] == "finished"]
    assert finished_rounds, rounds
    # Rounds are a minute apart here; the agenda (the AUDITOR task) stops changing, so
    # rounds stop too instead of spending an agent session a minute on the same thing.
    assert len(finished_rounds) <= 3, [(item["created_at"], item["reported"]) for item in rounds]
    # The flaky agent failed once and was run again inside the same placement.
    assert [step["task"] for step in steps].count(tasks["flaky"]) == 2, steps

    rows = {row["task_key"]: row for row in (await client.get("/v1/tasks", headers=ws["owner"],
            params={"task_key": ",".join(tasks.values())})).json()["items"]}
    flagged = {label for label, key in tasks.items() if rows[key]["needs_attention"]}
    # Exactly what should be looked at is flagged (plus whatever the 10% sample drew).
    assert {"manual", "risky", "missing_doc"} <= flagged, flagged
    assert "checked" not in flagged or rows[tasks["checked"]]["attention_reasons"][0].startswith(
        "sampled"), rows[tasks["checked"]]
    # The command check ran on the computer and its result reached the board.
    assert rows[tasks["checked"]]["check_results"] == [
        {"id": "tests", "type": "command", "passed": True, "detail": "exit 0"}]
    assert rows[tasks["missing_doc"]]["attention_reasons"] == [
        "check_failed: plan: document plans/never-written was not written"]
    assert all(rows[key]["reviewed_by"] is None for key in tasks.values()), "nobody approved"
    # The one task no computer here may take is still waiting, and says why.
    waiting = (await client.get("/v1/runner/placements", headers=ws["owner"],
                                params={"task_key": nobody})).json()["items"]
    assert [(item["status"], item["reason"]) for item in waiting] == [
        ("unplaced", "no_host_for_role")]

    doctor = (await client.get("/v1/runner/doctor", headers=ws["owner"])).json()
    checks = {item["id"]: item for item in doctor["checks"]}
    assert checks["rounds"]["ok"], checks["rounds"]
    metrics = (await client.get("/v1/runner/metrics", headers=ws["owner"])).json()
    assert metrics["supervise"]["finished"] >= 1 and metrics["supervise"]["skipped"] == 0
    assert metrics["pokes"]["not_delivered"] == 0, metrics["not_delivered_reasons"]
    print("\nUNATTENDED " + json.dumps({
        "all_done_s": round(all_done_at or -1), "observed_s": round(elapsed),
        "tasks_done": len(named), "flagged": sorted(flagged),
        "rounds_started": len(rounds),
        "rounds_finished": metrics["supervise"]["finished"],
        "delivered_p95_ms": metrics["timings"]["delivered_ms"]["p95"],
        "execution_started_p95_ms": metrics["timings"]["execution_started_ms"]["p95"]}))
