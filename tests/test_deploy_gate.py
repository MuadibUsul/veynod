"""VPS deploy gate: webhook, operator switches, runner protocol (v3.50).

agentgraph/spec-vps-auto-deploy-gate v1, A-01 (only signed merges into main
queue), A-05 (every trigger and outcome is audited), A-06 (trigger, pause,
revoke) and A-08 (the owner report keeps plain-language fields only). The
runner's checks are covered in tests/test_deploy_gate_runner.py.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest

from tests.conftest import run_server

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin"}
SECRET = "gate-webhook-secret"
RUNNER = {"Authorization": "Bearer gate-runner-token"}
REPO = "MuadibUsul/veynod"
GATE_ENV = {
    "DEPLOY_GATE_WEBHOOK_SECRET": SECRET,
    "DEPLOY_GATE_RUNNER_TOKEN": "gate-runner-token",
    "DEPLOY_GATE_REPO": REPO,
    "DEPLOY_GATE_BRANCH": "main",
}


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _sql(query: str, *args):
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


async def _reset() -> None:
    await _sql("DELETE FROM deploy_gate_runs")
    await _sql("UPDATE deploy_gate_state SET paused = false, paused_reason = NULL, "
               "auto_deploy_authorized = false, deployed_commit = NULL, deployed_at = NULL, "
               "runner_last_seen_at = NULL WHERE id = 1")


@pytest.fixture(scope="module")
async def gate(api_base_url) -> AsyncIterator[httpx.AsyncClient]:
    del api_base_url  # the shared server applied the schema
    async with run_server(GATE_ENV, ".server-deploy-gate.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as http:
            yield http


def _sha() -> str:
    return hashlib.sha1(uuid.uuid4().bytes).hexdigest()


async def _deliver(client: httpx.AsyncClient, event: str, payload: dict, *,
                   secret: str | None = SECRET, delivery: str | None = None) -> httpx.Response:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "X-GitHub-Event": event,
               "X-GitHub-Delivery": delivery or str(uuid.uuid4())}
    if secret is not None:
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={digest}"
    return await client.post("/v1/deploy-gate/github", content=body, headers=headers)


def _push(sha: str, ref: str = "refs/heads/main", **extra) -> dict:
    return {"ref": ref, "after": sha, "deleted": False, "forced": False,
            "repository": {"full_name": REPO}, "sender": {"login": "ethan"}, **extra}


def _merged(sha: str, number: int, base: str = "main", merged: bool = True) -> dict:
    return {"action": "closed", "repository": {"full_name": REPO}, "sender": {"login": "ethan"},
            "pull_request": {"number": number, "merged": merged, "merge_commit_sha": sha,
                             "base": {"ref": base}}}


async def _runs() -> list:
    return await _sql("SELECT * FROM deploy_gate_runs ORDER BY created_at")


async def test_an_unconfigured_server_exposes_no_gate(client):
    hook = await client.post("/v1/deploy-gate/github", content=b"{}",
                             headers={"X-GitHub-Event": "push"})
    assert hook.status_code == 404
    beat = await client.post("/v1/deploy-gate/runner/heartbeat", json={"host": "vps"},
                             headers=RUNNER)
    assert beat.status_code == 404
    view = await client.get("/v1/admin/deploy-gate", headers=ADMIN)
    assert view.status_code == 200, view.text
    assert view.json()["state"]["configured"] is False


async def test_only_signed_merges_into_main_queue(gate):
    await _reset()
    sha = _sha()
    # Unsigned and forged deliveries are refused before anything is stored.
    assert (await _deliver(gate, "push", _push(sha), secret=None)).status_code == 401
    assert (await _deliver(gate, "push", _push(sha), secret="wrong")).status_code == 401
    assert await _runs() == []

    ignored = {
        "not_deploy_branch": await _deliver(gate, "push", _push(sha, ref="refs/heads/feature")),
        "force_push": await _deliver(gate, "push", _push(sha, forced=True)),
        "branch_deleted": await _deliver(gate, "push", _push(sha, deleted=True)),
        "not_merged": await _deliver(gate, "pull_request", _merged(sha, 7, merged=False)),
        "other_repository": await _deliver(gate, "push", {**_push(sha),
                                                          "repository": {"full_name": "x/y"}}),
    }
    for reason, response in ignored.items():
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "ignored", "reason": reason}
    other_base = await _deliver(gate, "pull_request", _merged(sha, 7, base="develop"))
    assert other_base.json()["reason"] == "not_deploy_branch"
    assert (await _deliver(gate, "ping", {"zen": "hi"})).json()["reason"] == "ping"
    assert await _runs() == []
    audit = await _sql("SELECT details->>'reason' AS reason FROM deploy_gate_audit "
                       "WHERE action = 'webhook_ignored' ORDER BY id DESC LIMIT 6")
    assert {row["reason"] for row in audit} >= {"not_deploy_branch", "force_push", "not_merged"}

    delivery = str(uuid.uuid4())
    queued = await _deliver(gate, "push", _push(sha), delivery=delivery)
    assert queued.json()["status"] == "queued", queued.text
    again = await _deliver(gate, "push", _push(sha), delivery=delivery)
    assert again.json() == {"status": "duplicate", "reason": "delivery_seen"}
    # The merged-PR delivery for the same commit adds the PR number, not a second run.
    pull = await _deliver(gate, "pull_request", _merged(sha, 42))
    assert pull.json()["status"] == "duplicate"
    runs = await _runs()
    assert len(runs) == 1
    assert runs[0]["commit_sha"] == sha and runs[0]["pr_number"] == 42
    assert runs[0]["trigger_kind"] == "webhook" and runs[0]["triggered_by"] == "github:ethan"


async def test_runner_token_is_separate_from_the_admin_token(gate):
    for headers in ({}, ADMIN, {"Authorization": "Bearer nope"}):
        response = await gate.post("/v1/deploy-gate/runner/heartbeat", json={"host": "vps"},
                                   headers=headers)
        assert response.status_code == 401
    assert (await gate.get("/v1/admin/deploy-gate", headers=RUNNER)).status_code == 401


async def test_pause_skips_and_revoked_authorization_blocks_automatic_deploys(gate):
    await _reset()
    view = (await gate.get("/v1/admin/deploy-gate", headers=ADMIN)).json()
    assert view["state"]["auto_deploy_authorized"] is False  # safe default
    assert view["state"]["runner"]["status"] == "offline"

    paused = await gate.post("/v1/admin/deploy-gate/pause", json={"reason": "release freeze"},
                             headers=ADMIN)
    assert paused.json()["paused"] is True
    await _deliver(gate, "push", _push(_sha()))
    claim = (await gate.post("/v1/deploy-gate/runner/claim", json={"host": "vps"},
                             headers=RUNNER)).json()
    assert claim["run"] is None
    runs = await _runs()
    assert [row["status"] for row in runs] == ["skipped"]
    assert runs[0]["status_reason"] == "the gate is paused"

    await gate.post("/v1/admin/deploy-gate/resume", headers=ADMIN)
    await _deliver(gate, "push", _push(_sha()))
    claim = (await gate.post("/v1/deploy-gate/runner/claim", json={"host": "vps"},
                             headers=RUNNER)).json()
    assert claim["run"]["status"] == "running"
    assert claim["deploy_allowed"] is False  # automatic trigger, not authorized

    await gate.post("/v1/admin/deploy-gate/authorize", headers=ADMIN)
    sha = _sha()
    await _deliver(gate, "push", _push(sha))
    claim = (await gate.post("/v1/deploy-gate/runner/claim", json={"host": "vps"},
                             headers=RUNNER)).json()
    assert claim["deploy_allowed"] is True and claim["run"]["commit_sha"] == sha

    revoked = await gate.post("/v1/admin/deploy-gate/revoke", json={"reason": "manual only"},
                              headers=ADMIN)
    assert revoked.json()["auto_deploy_authorized"] is False
    manual = await gate.post("/v1/admin/deploy-gate/trigger", json={"reason": "try it"},
                             headers=ADMIN)
    assert manual.status_code == 201, manual.text
    claim = (await gate.post("/v1/deploy-gate/runner/claim", json={"host": "vps"},
                             headers=RUNNER)).json()
    # The operator's own trigger is an explicit authorization for that run.
    assert claim["deploy_allowed"] is True and claim["run"]["trigger_kind"] == "manual"

    audit = (await gate.get("/v1/admin/deploy-gate/audit?limit=50", headers=ADMIN)).json()
    actions = [item["action"] for item in audit["items"]]
    for action in ("pause", "resume", "authorize", "revoke", "queued", "claimed",
                   "skipped_paused"):
        assert action in actions, action


async def test_runner_protocol_records_outcome_and_plain_report(gate):
    await _reset()
    beat = await gate.post("/v1/deploy-gate/runner/heartbeat",
                           json={"host": "vps-1", "version": "1"}, headers=RUNNER)
    assert beat.json()["runner"]["status"] == "online"

    old, new = _sha(), _sha()
    first = await gate.post("/v1/deploy-gate/runner/runs", json={"host": "vps-1", "commit": old},
                            headers=RUNNER)
    assert first.status_code == 201 and first.json()["status"] == "queued"
    repeat = await gate.post("/v1/deploy-gate/runner/runs", json={"host": "vps-1", "commit": old},
                             headers=RUNNER)
    assert repeat.json()["status"] == "duplicate"  # a seen commit is not re-run every poll
    await gate.post("/v1/deploy-gate/runner/runs", json={"host": "vps-1", "commit": new},
                    headers=RUNNER)
    claim = (await gate.post("/v1/deploy-gate/runner/claim", json={"host": "vps-1"},
                             headers=RUNNER)).json()
    run_id = claim["run"]["id"]
    assert claim["run"]["commit_sha"] == new
    statuses = {row["commit_sha"]: (row["status"], row["status_reason"]) for row in await _runs()}
    assert statuses[old] == ("skipped", "superseded by a newer trigger")

    other = await gate.post(f"/v1/deploy-gate/runner/runs/{run_id}",
                            json={"host": "vps-2", "status": "deployed"}, headers=RUNNER)
    assert other.status_code == 409  # only the runner that claimed it reports

    done = await gate.post(f"/v1/deploy-gate/runner/runs/{run_id}", headers=RUNNER, json={
        "host": "vps-1", "status": "deployed", "commit_sha": new,
        "checks": [{"id": f"G-0{n}", "ok": True, "summary": "ok"} for n in range(1, 6)]
        + [{"id": "X-99", "ok": False, "summary": "not a gate check"}],
        "deploy": {"stamp": "20261028T000000Z", "result": "ok", "secret": "leak"},
        "report": {"changed": ["The task board shows who is online."],
                   "how_to_try": ["Open the console and look at the task board."],
                   "unfinished": [], "pytest": "418 passed"},
        "log_ref": "/var/log/veynod-deploy-gate/x.log"})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "deployed" and body["finished_at"]
    assert [check["id"] for check in body["checks"]] == ["G-01", "G-02", "G-03", "G-04", "G-05"]
    assert "secret" not in body["deploy"]
    assert set(body["report"]) == {"changed", "how_to_try", "rolled_back", "unfinished"}
    assert body["report"]["rolled_back"] is False

    again = await gate.post(f"/v1/deploy-gate/runner/runs/{run_id}",
                            json={"host": "vps-1", "status": "failed"}, headers=RUNNER)
    assert again.status_code == 409  # a finished run cannot be rewritten

    view = (await gate.get("/v1/admin/deploy-gate", headers=ADMIN)).json()
    assert view["state"]["deployed_commit"] == new
    detail = (await gate.get(f"/v1/admin/deploy-gate/runs/{run_id}", headers=ADMIN)).json()
    assert [item["action"] for item in detail["audit"]][-1] == "finished"
    assert detail["triggered_by"] == "timer:vps-1"


async def test_a_silent_runner_shows_offline(gate):
    await gate.post("/v1/deploy-gate/runner/heartbeat", json={"host": "vps-1"}, headers=RUNNER)
    await _sql("UPDATE deploy_gate_state SET runner_last_seen_at = NOW() - INTERVAL '1 hour'")
    view = (await gate.get("/v1/admin/deploy-gate", headers=ADMIN)).json()
    assert view["state"]["runner"]["status"] == "offline"


async def test_audit_rows_are_append_only(gate):
    await gate.post("/v1/admin/deploy-gate/pause", headers=ADMIN)
    await gate.post("/v1/admin/deploy-gate/resume", headers=ADMIN)
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await _sql("DELETE FROM deploy_gate_audit")


async def test_task_evidence_reads_the_board_without_changing_it(client, admin_headers):
    created = (await client.post("/v1/admin/workspaces", headers=admin_headers,
                                 json={"name": f"board-{uuid.uuid4().hex[:6]}"})).json()
    key = {"Authorization": f"Bearer {created['api_key']}"}
    task = await client.post("/v1/tasks", headers=key, json={
        "task_key": "gate-evidence", "title": "gate evidence", "assigned_role": "EXECUTOR"})
    assert task.status_code == 201, task.text
    env = {**GATE_ENV, "DEPLOY_GATE_BOARD_WORKSPACE_ID": created["workspace"]["id"]}
    async with run_server(env, ".server-deploy-gate-board.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as board:
            evidence = await board.post("/v1/deploy-gate/runner/task-evidence", headers=RUNNER,
                                        json={"task_keys": ["gate-evidence", "missing-one"]})
    assert evidence.status_code == 200, evidence.text
    tasks = {item["task_key"]: item for item in evidence.json()["tasks"]}
    assert tasks["gate-evidence"] == {"task_key": "gate-evidence", "found": True,
                                      "status": "READY", "verdict": None, "failing_gates": []}
    assert tasks["missing-one"] == {"task_key": "missing-one", "found": False}
    after = await client.get("/v1/tasks/gate-evidence", headers=key)
    assert after.json()["status"] == "READY" and after.json()["version"] == 1
