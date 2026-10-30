"""Loop A-03: under full automation a code task is DONE only after a verified deploy.

spec agentgraph/spec-auto-loop-mode v2, step 5 and R-04: an agent's approval
of a task that ships through the deploy gate leaves it in HANDOFF until a
deploy that carried it passed its smoke test (G-05); the deploy is announced
to the task's executor and reviewer, and the next approval is DONE with that
run as evidence. Tasks that ship nothing, and other modes, are unchanged.
"""

from __future__ import annotations

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
RUNNER = {"Authorization": "Bearer gate-runner-token"}
PASSWORD = "correct-horse-battery"
REPO = "MuadibUsul/veynod"
BRIEF = {"summary": "built it", "key_metrics": {}, "unverified_hypotheses": [],
         "risk_flag": False,
         "owner_report": {"what_changed": "The board shows who is online.",
                          "how_to_try": "Open the board.", "not_yet_done": ""}}
SMOKE_OK = [{"id": f"G-0{n}", "ok": True, "summary": "ok"} for n in range(1, 6)]


def _dsn() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def _sql(query: str, *args):
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


@pytest.fixture(scope="module")
async def board(api_base_url) -> AsyncIterator[dict]:
    """A deploy gate whose board workspace has an owner, a reviewer key and an executor."""
    async with httpx.AsyncClient(base_url=api_base_url, timeout=60) as shared:
        created = (await shared.post("/v1/admin/workspaces", headers=ADMIN, json={
            "name": f"evidence-{uuid.uuid4().hex[:6]}", "tier": "pro"})).json()
        wid = created["workspace"]["id"]
        email = f"owner-{uuid.uuid4().hex[:8]}@example.test"
        made = await shared.post(f"/v1/admin/workspaces/{wid}/human-users", headers=ADMIN,
                                 json={"email": email, "display_name": "Owner",
                                       "role": "OWNER", "password": PASSWORD})
        assert made.status_code in (200, 201), made.text
        signature = f"exec-{uuid.uuid4().hex[:6]}"
        minted = await shared.post(f"/v1/admin/workspaces/{wid}/agent-tokens", headers=ADMIN,
                                   json={"agent_signature": signature,
                                         "role_scope": "EXECUTOR",
                                         "allowed_namespaces": ["*"]})
        assert minted.status_code == 201, minted.text
    env = {"DEPLOY_GATE_WEBHOOK_SECRET": "secret", "DEPLOY_GATE_RUNNER_TOKEN":
           "gate-runner-token", "DEPLOY_GATE_REPO": REPO, "DEPLOY_GATE_BRANCH": "main",
           "DEPLOY_GATE_BOARD_WORKSPACE_ID": wid, "DEPLOY_EVIDENCE_REQUIRED": "true"}
    async with run_server(env, ".server-deploy-evidence.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as http:
            login = await http.post("/v1/auth/login",
                                    json={"email": email, "password": PASSWORD})
            assert login.status_code == 200, login.text
            yield {"http": http, "id": wid, "signature": signature,
                   "key": {"Authorization": f"Bearer {created['api_key']}"},
                   "agent": {"Authorization": f"Bearer {minted.json()['api_key']}"},
                   "owner": {"Authorization": f"Bearer {login.json()['token']}"}}


async def _mode(board: dict, mode: str) -> None:
    changed = await board["http"].put("/v1/human/automation", headers=board["owner"], json={
        "mode": mode, "auto_deploy_allowed": mode != "manual", "reason": "test"})
    assert changed.status_code == 200, changed.text


async def _submitted(board: dict, **extra) -> str:
    """A task the executor built and handed to review."""
    http = board["http"]
    key = f"EV-{uuid.uuid4().hex[:6].upper()}"
    created = await http.post("/v1/tasks", headers=board["key"], json={
        "task_key": key, "title": "show who is online", "assigned_role": "EXECUTOR",
        **extra})
    assert created.status_code == 201, created.text
    await _resubmit(board, key)
    return key


async def _resubmit(board: dict, key: str) -> None:
    http = board["http"]
    claimed = await http.post(f"/v1/tasks/{key}/claim", headers=board["agent"], json={})
    assert claimed.status_code == 200, claimed.text
    handed = await http.post(f"/v1/tasks/{key}/handoff", headers=board["agent"], json={
        "next_status": "REVIEWING", "brief": BRIEF})
    assert handed.status_code == 200, handed.text


async def _approve(board: dict, key: str) -> dict:
    reviewed = await board["http"].post(f"/v1/tasks/{key}/review", headers=board["key"],
                                        json={"decision": "approve"})
    assert reviewed.status_code == 200, reviewed.text
    return reviewed.json()


async def _deploy(board: dict, task_keys: list[str], *, smoke_ok: bool = True) -> str:
    """The runner claims a run and reports it, naming the tasks it carried."""
    http = board["http"]
    await _sql("DELETE FROM deploy_gate_runs WHERE status IN ('queued', 'running')")
    beat = await http.post("/v1/deploy-gate/runner/heartbeat",
                           json={"host": "vps", "version": "1"}, headers=RUNNER)
    assert "task_keys" in beat.json()["runner_fields"]
    commit = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    queued = await http.post("/v1/deploy-gate/runner/runs",
                             json={"host": "vps", "commit": commit}, headers=RUNNER)
    assert queued.status_code == 201, queued.text
    run = (await http.post("/v1/deploy-gate/runner/claim", json={"host": "vps"},
                           headers=RUNNER)).json()["run"]
    checks = SMOKE_OK if smoke_ok else [*SMOKE_OK[:4], {"id": "G-05", "ok": False,
                                                        "summary": "smoke failed"}]
    reported = await http.post(f"/v1/deploy-gate/runner/runs/{run['id']}", headers=RUNNER,
                               json={"host": "vps", "status": "deployed" if smoke_ok
                                     else "rolled_back", "commit_sha": commit,
                                     "checks": checks, "rolled_back": not smoke_ok,
                                     "task_keys": task_keys})
    assert reported.status_code == 200, reported.text
    assert reported.json()["task_keys"] == task_keys
    return run["id"]


async def test_full_automation_waits_for_a_verified_deploy_before_done(board):
    await _mode(board, "full")
    key = await _submitted(board, repo=REPO)

    first = await _approve(board, key)
    assert first["task"]["status"] == "HANDOFF"
    assert first["awaiting_deploy"] is True
    assert first["deploy_evidence"] is None
    verdict = await _sql("SELECT verdict FROM audits a JOIN collab_tasks t ON t.id = a.task_id "
                         "WHERE t.task_key = $1", key)
    assert [row["verdict"] for row in verdict] == ["APPROVE"]  # the gate's G-03 sees it

    # A deploy whose smoke test failed (rolled back) is not evidence.
    await _deploy(board, [key], smoke_ok=False)
    await _resubmit(board, key)
    still = await _approve(board, key)
    assert still["task"]["status"] == "HANDOFF" and still["awaiting_deploy"] is True

    run_id = await _deploy(board, [key])
    events = await _sql("SELECT payload FROM timeline_streams WHERE workspace_id = $1::uuid "
                        "AND event_type = 'DEPLOY_VERIFIED'", board["id"])
    payloads = [json.loads(row["payload"]) if isinstance(row["payload"], str)
                else row["payload"] for row in events]
    assert [item["run_id"] for item in payloads] == [run_id]
    assert payloads[0]["task_key"] == key
    assert board["signature"] in str(payloads[0]["targets"])

    await _resubmit(board, key)
    done = await _approve(board, key)
    assert done["task"]["status"] == "DONE"
    assert done["deploy_evidence"]["run_id"] == run_id
    assert done.get("awaiting_deploy") is None


async def test_tasks_that_ship_nothing_and_other_modes_are_unchanged(board):
    await _mode(board, "full")
    plain = await _submitted(board)  # no repo: nothing to deploy
    assert (await _approve(board, plain))["task"]["status"] == "DONE"

    await _mode(board, "semi")
    code = await _submitted(board, repo=REPO)
    assert (await _approve(board, code))["task"]["status"] == "DONE"


async def test_deploy_evidence_is_off_unless_enabled(monkeypatch):
    """A merged pull request finishes a task; shipping is a roadmap item."""
    from app.config import get_settings
    from app.services.tasks import _deploy_evidence_for_done

    monkeypatch.setattr(get_settings(), "deploy_evidence_required", False)

    class _Task:
        repo = REPO
        task_key = "chore-deploy-gate-out-of-task-path"

    assert await _deploy_evidence_for_done(None, None, _Task()) is None
