"""The real messenger and its real workers against the real server.

Ten supervise rounds in a row on one computer, each of which must end
``finished`` on the worker's own report, with the three times of every delivery
recorded: the computer read the poke, a worker was ready, the work began. The
workers are the CLI's own processes (prepared ahead by the messenger); only the
agent they would start is replaced by a command that exits 0.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from tests.test_loop_reliability import _fetch, _placements, _until
from tests.test_scheduler import ADMIN, REPO, _schedule, _sql, _task, _workspace

pytestmark = pytest.mark.integration

CLI = Path(__file__).resolve().parent.parent / "cli" / "bin" / "veynod-messenger.mjs"
ROUNDS = 10


async def _messenger(api_base_url: str, client, ws: dict, state: str, *, warm: int) -> tuple:
    name = f"e2e-{uuid.uuid4().hex[:4]}"
    minted = await client.post(
        f"/v1/admin/workspaces/{ws['id']}/agent-tokens", headers=ADMIN,
        json={"role_scope": "EXECUTOR", "allowed_namespaces": ["*"]})
    assert minted.status_code == 201, minted.text
    key = minted.json()["api_key"]
    headers = {"Authorization": f"Bearer {key}"}
    named = await client.post("/v1/agents/register", headers=headers, json={
        "agent_id": name, "capabilities": ["supervisor"]})
    assert named.status_code in (200, 201), named.text
    hello = await client.post("/v1/runner/hello", headers=headers, json={"host": {
        "os": "linux", "arch": "x64", "max_concurrency": 1, "repos": [REPO],
        "runner_version": "0.9.0", "runtimes": [{"id": "custom", "available": True}]}})
    assert hello.status_code == 200, hello.text
    log = open(os.path.join(state, "hub.log"), "w", encoding="utf-8")  # noqa: SIM115
    process = await asyncio.create_subprocess_exec(
        shutil.which("node") or "node", str(CLI), "--host", api_base_url, "--key", key,
        "--agent", name, "--slots", "1", "--warm", str(warm),
        env={**os.environ, "VEYNOD_STATE_DIR": state,
             "VEYNOD_RUNNER_OPTIONS": json.dumps({
                 "runtime": "custom", "exec": f'"{sys.executable}" -c "raise SystemExit(0)"'})},
        stdout=log, stderr=log)
    return process, {"headers": headers, "agent_id": name, "host_id": hello.json()["host_id"]}


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
async def test_ten_rounds_in_a_row_finish_and_meet_the_start_targets(api_base_url, client) -> None:
    ws = await _workspace(client, "full")
    await _task(client, ws, repo="acme/unreachable")  # something for the supervisor to look at
    state = tempfile.mkdtemp(prefix="veynod-e2e-")
    process, host = await _messenger(api_base_url, client, ws, state, warm=2)
    try:
        for index in range(ROUNDS):
            await _schedule(client, ws)

            async def ended(count: int = index + 1):
                rounds = [item for item in await _placements(client, ws, step="supervise")
                          if item["host_id"] == host["host_id"] and item["ended_at"]]
                return rounds if len(rounds) >= count else None

            rounds = await _until(ended, timeout=40)
            latest = max(rounds, key=lambda item: item["created_at"])
            assert (latest["status"], latest["stage"]) == ("delivered", "finished"), latest
            # The next round is due: rounds are a set interval apart.
            await _sql("UPDATE runner_placements SET created_at = created_at - INTERVAL '1 hour' "
                       "WHERE workspace_id = $1::uuid AND step = 'supervise'", ws["id"])

        metrics = (await client.get("/v1/runner/metrics", headers=ws["owner"])).json()
        supervise = metrics["supervise"]
        assert supervise["finished"] == ROUNDS, supervise
        assert supervise["finished_in_a_row"] == ROUNDS
        assert supervise["skipped"] == supervise["failed"] == supervise["not_delivered"] == 0
        assert supervise["finished_rate"] == 1.0
        assert metrics["pokes"]["worker_start_failure_rate"] == 0.0
        timings = metrics["timings"]
        assert timings["delivered_ms"]["count"] == ROUNDS
        assert timings["execution_started_ms"]["count"] == ROUNDS
        # The three second-level targets, on prepared workers.
        assert timings["delivered_ms"]["p95"] <= 1000, timings
        assert timings["execution_started_warm_ms"]["count"] >= ROUNDS - 1, timings
        assert timings["execution_started_warm_ms"]["p95"] <= 2000, timings
        assert timings["execution_started_warm_ms"]["p99"] <= 5000, timings
        assert metrics["meets_targets"] == {"delivered_p95": True, "execution_started_p95": True,
                                            "execution_started_p99": True}

        events = [row["event_type"] for row in await _fetch(
            "SELECT event_type FROM timeline_streams WHERE workspace_id = $1::uuid "
            "AND event_type LIKE 'LOOP_ROUND%'", ws["id"])]
        assert events.count("LOOP_ROUND") == events.count("LOOP_ROUND_FINISHED") == ROUNDS
        assert not [kind for kind in events if kind not in ("LOOP_ROUND", "LOOP_ROUND_FINISHED")]
        # The worker's own receipts say the same: started, then finished, never skipped.
        receipts = [json.loads(line) for line in
                    Path(state, "runner-receipts.jsonl").read_text().splitlines()]
        assert [item["stage"] for item in receipts].count("finished") == ROUNDS
        assert "skipped" not in {item["stage"] for item in receipts}
        print("\nLOOP_E2E_METRICS " + json.dumps({"supervise": supervise, "timings": timings}))
    finally:
        process.terminate()
        await process.wait()
        print(Path(state, "hub.log").read_text()[-3000:])
