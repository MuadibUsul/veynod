"""One production smoke: a real submission through the Docker gate sandbox.

Runs inside a throwaway container on the VPS (network agentgraph_internal,
REPOS_DIR mounted at /repos). Expects KERNEL_URL, KERNEL_WORKSPACE_ID,
KERNEL_SERVICE_KEY and KERNEL_RUNTIME_TOKEN in the environment.

Flow: runtime task -> pull bundle (lease worktree) -> edit calc.add comment ->
POST /v2/syscalls/submit_patch -> poll GET /v2/submissions/{id} until final.
Success is task_status == COMMITTED with gate_results recorded.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx

BASE = os.environ["KERNEL_URL"].rstrip("/")
WORKSPACE = os.environ["KERNEL_WORKSPACE_ID"]
SYMBOL = "src/calc.py::add"


def fail(message: str) -> None:
    print("SMOKE-FAIL:", message, flush=True)
    sys.exit(1)


def main() -> None:
    service = httpx.Client(
        base_url=BASE, timeout=60,
        headers={"X-Veynod-Service-Key": os.environ["KERNEL_SERVICE_KEY"],
                 "X-Workspace-Id": WORKSPACE})
    runner = httpx.Client(
        base_url=BASE, timeout=60,
        headers={"Authorization": f"Bearer {os.environ['KERNEL_RUNTIME_TOKEN']}",
                 "X-Workspace-Id": WORKSPACE})

    created = service.post("/v1/runtime/tasks", json={
        "request_key": f"smoke-deploy-{int(time.time())}",
        "title": "Deploy smoke: Docker gate sandbox",
        "spec_prompt": "Keep calc.add behaving the same; touch only the write set.",
        "type": "IMPLEMENT_BODY",
        "target_symbols": [SYMBOL],
        "write_set_symbols": [SYMBOL],
        "priority": 100,
    })
    if created.status_code not in (200, 201):
        fail(f"task create {created.status_code}: {created.text}")
    task_id = created.json()["task_id"]
    print(f"task={task_id}", flush=True)

    pulled = runner.post("/v1/syscalls/pull_task_bundle", json={
        "task_id": task_id,
        "worker_hub_id": f"smoke-deploy-{int(time.time())}",
        "supported_task_types": ["IMPLEMENT_BODY"],
        "max_context_tokens": 4000,
        "wait_seconds": 0,
    })
    if pulled.status_code != 200:
        fail(f"pull {pulled.status_code}: {pulled.text}")
    bundle = pulled.json()
    if not bundle.get("leased"):
        fail(f"pull did not lease: {bundle}")
    worktree = bundle["worktree_path"]
    base_sha = bundle["base_commit_sha"]
    lease_token = bundle["lease_token"]
    print(f"worktree={worktree} base={base_sha}", flush=True)

    calc = os.path.join(worktree, "src", "calc.py")
    text = open(calc, encoding="utf-8").read()
    marker = f"# smoke_{int(time.time())}"
    if "return a + b" not in text:
        fail("expected calc.add to contain 'return a + b'")
    open(calc, "w", encoding="utf-8").write(
        text.replace("return a + b  #", f"return a + b  {marker}\n    #", 1)
    )
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "smoke", "GIT_AUTHOR_EMAIL": "smoke@example.test",
           "GIT_COMMITTER_NAME": "smoke", "GIT_COMMITTER_EMAIL": "smoke@example.test"}
    # An unstaged diff keeps this probe from writing loose objects into the
    # shared bare repo as root: the gate must be the one staging (as the
    # repository owner), exactly like the real submission path.
    diff = subprocess.run(["git", "diff"], cwd=worktree, check=True,
                          capture_output=True, text=True, env=env).stdout
    if not diff.strip():
        fail("empty diff after edit")

    submitted = runner.post("/v2/syscalls/submit_patch", json={
        "task_id": task_id,
        "lease_token": lease_token,
        "git_diff": diff,
        "touched_symbols": [SYMBOL],
        "local_test_passed": True,
    })
    if submitted.status_code != 202:
        fail(f"submit {submitted.status_code}: {submitted.text}")
    submission_id = submitted.json()["submission_id"]
    print(f"submission={submission_id}", flush=True)

    for _ in range(180):
        status = runner.get(f"/v2/submissions/{submission_id}")
        if status.status_code != 200:
            fail(f"submission status {status.status_code}: {status.text}")
        body = status.json()
        if body.get("final"):
            print(f"task_status={body.get('task_status')} "
                  f"merge={body.get('merge_mode')} "
                  f"sha={body.get('result_commit_sha')} "
                  f"attempts={body.get('attempts')} "
                  f"error={body.get('error_code')}", flush=True)
            gates = [{"stage": g.get("stage"), "status": g.get("status")}
                     for g in body.get("gate_results", [])]
            print(f"gates={gates}", flush=True)
            if body.get("task_status") == "COMMITTED" and body.get("result_commit_sha"):
                print(f"SMOKE-OK: committed {body['result_commit_sha']}", flush=True)
                return
            fail(f"gate did not commit: {body.get('error_detail') or gates}")
        time.sleep(2)
    fail("timed out waiting for the gate")


if __name__ == "__main__":
    main()
