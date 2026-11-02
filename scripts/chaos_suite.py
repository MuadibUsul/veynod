"""Real fault-injection suite for the unified Runtime -> Kernel chain.

Every scenario runs the real services (Kernel API + Gate Worker, Runtime API +
Worker, AgentGraph API) against real PostgreSQL, with an HTTP scripted model.
No fault is mocked: workers and APIs are killed with SIGKILL, database
connections are terminated, duplicate events are delivered.

    python scripts/chaos_suite.py            # all scenarios
    python scripts/chaos_suite.py --only worker_kill,gate_kill
    python scripts/chaos_suite.py --keep

Results are printed as a matrix and written to docs/CHAOS_TEST_MATRIX.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "AgentGraph", ROOT / "veynod_kernel", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from e2e_unified import ensure_databases, free_port, start, wait_for  # noqa: E402
from scripted_model_server import build_handler  # noqa: E402

FUNCTIONS = 12
KERNEL = "veynod_kernel"
TARGET_RE = re.compile(r"(src/f\d+\.py)::(compute)")


# --------------------------------------------------------------- fixtures
def _suite_files() -> dict[str, str]:
    files: dict[str, str] = {"src/__init__.py": "", "tests/__init__.py": ""}
    for index in range(FUNCTIONS):
        files[f"src/f{index}.py"] = (
            '"""Generated suite module."""\n\n\n'
            "def compute(a: int, b: int) -> int:\n"
            '    """Return a combined value."""\n'
            "    ...\n"
        )
        files[f"tests/test_f{index}.py"] = (
            f"from src.f{index} import compute\n\n\n"
            "def test_compute() -> None:\n"
            "    assert compute(2, 3) == 5\n"
        )
    return files


def build_suite_repo(root: Path) -> Path:
    work = root / "work"
    work.mkdir(parents=True)
    for name, content in _suite_files().items():
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=work, check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "chaos@veynod.test")
    git("config", "user.name", "Veynod Chaos")
    git("add", "-A")
    git("commit", "-m", "suite fixture")
    bare = root / "suite.git"
    subprocess.run(["git", "clone", "--bare", str(work), str(bare)], check=True, capture_output=True)
    return bare


class ChaosModel:
    """Deterministic decisions that derive the symbol from the task goal."""

    def __init__(self) -> None:
        self.delay = 0.0
        self.calls: dict[str, int] = {}

    async def decide(self, run: dict[str, Any], context: dict[str, Any]):
        from agent_runtime.loop import ModelAction

        goal = str(run.get("goal") or "")
        match = TARGET_RE.search(goal)
        if not match:
            return ModelAction(kind="finish", output={"error": "no symbol in goal"}, reason="no symbol")
        file_path, func = match.group(1), match.group(2)
        index = file_path.split("f")[-1].split(".")[0]
        checkpoint: dict[str, Any] = {}
        for section in context.get("sections", []):
            if section.get("kind") == "checkpoint" and isinstance(
                section.get("content"), dict
            ):
                checkpoint = section["content"]
        if "external_result" in checkpoint:
            result = checkpoint.get("external_result") or {}
            if str(result.get("status", "")).upper() in {"COMMITTED", "MERGED"}:
                return ModelAction(
                    kind="finish",
                    output={"commit_sha": result.get("result_commit_sha")},
                    reason="gate passed",
                )
            task_id = result.get("task_id") or checkpoint.get("kernel_task_id") or ""
            if not task_id:
                return ModelAction(kind="finish", output={"evidence": result}, reason="gate failed")
            return ModelAction(
                kind="tool",
                tool="get_task_evidence",
                arguments={"task_id": task_id},
                idempotency_key=f"ev:{run['run_id']}:{index}:{self.calls.get(run['run_id'], 0)}",
                reason="gate failed; read evidence",
            )
        if not checkpoint:
            return ModelAction(
                kind="tool",
                tool="create_task",
                arguments={
                    "title": f"Implement compute in {file_path}",
                    "spec_prompt": f"Implement {func} in {file_path} so the frozen test passes.",
                    "type": "IMPLEMENT_BODY",
                    "target_symbols": [f"{file_path}::{func}"],
                    "test_symbols": [f"tests/test_f{index}.py::test_compute"],
                    "write_set_symbols": [f"{file_path}::{func}"],
                    "priority": 100,
                },
                idempotency_key=f"create:{run['run_id']}",
                reason="create the Kernel node",
            )
        tool = checkpoint.get("tool")
        output = checkpoint.get("output") or {}
        if tool == "create_task":
            return ModelAction(
                kind="tool",
                tool="pull_task_bundle",
                arguments={"max_context_tokens": 6000},
                idempotency_key=f"pull:{run['run_id']}",
                reason="lease the node",
            )
        if tool == "pull_task_bundle":
            return ModelAction(
                kind="tool",
                tool="read_file",
                arguments={"path": file_path},
                idempotency_key=f"read:{run['run_id']}",
                reason="read the base file",
            )
        if tool == "read_file":
            content = str(output.get("content") or "")
            self.calls[run["run_id"]] = self.calls.get(run["run_id"], 0) + 1
            return ModelAction(
                kind="tool",
                tool="submit_patch",
                arguments={
                    "git_diff": _diff(file_path, content),
                    "touched_symbols": [f"{file_path}::{func}"],
                    "local_test_passed": True,
                },
                idempotency_key=f"patch:{run['run_id']}:{self.calls[run['run_id']]}",
                reason="submit to the deterministic gate",
            )
        return ModelAction(kind="finish", output={"checkpoint": checkpoint}, reason="done")


def _diff(path: str, content: str) -> str:
    import difflib

    lines = content.splitlines(keepends=True)
    changed: list[str] = []
    seen_def = False
    for line in lines:
        if line.lstrip().startswith("def compute("):
            seen_def = True
            changed.append(line)
            continue
        if seen_def and line.strip() == "...":
            changed.append("    return a + b\n")
            seen_def = False
            continue
        changed.append(line)
    return "".join(
        difflib.unified_diff(lines, changed, fromfile=f"a/{path}", tofile=f"b/{path}")
    )


# ------------------------------------------------------------------ http
def http(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"{method} {url} -> {exc.code}: {exc.read().decode('utf-8', 'replace')[:800]}"
        ) from exc


# ---------------------------------------------------------------- stack
class Stack:
    def __init__(self, temp: Path, logs: Path) -> None:
        self.temp = temp
        self.logs = logs
        self.api_port = free_port(8100)
        self.kernel_port = free_port(8101)
        self.runtime_port = free_port(8102)
        self.model_port = free_port(8103)
        self.api = f"http://127.0.0.1:{self.api_port}"
        self.kernel = f"http://127.0.0.1:{self.kernel_port}"
        self.runtime = f"http://127.0.0.1:{self.runtime_port}"
        self.service_key = "chaos-service-key-0123456789abcdef01234567"
        self.admin_key = "chaos-admin-key-0123456789abcdef0123456789"
        self.runtime_key = "chaos-runtime-key-0123456789abcdef0123456"
        self.admin_token = "chaos-admin-token-000000000000000000000000"
        self.product_python = ROOT / "AgentGraph" / (
            ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
        )
        if not self.product_python.exists():
            self.product_python = Path(sys.executable)
        self.model = ChaosModel()
        self.gate_workers = 1
        self.worker_count = 2
        self.gate_env: dict[str, str] = {}
        self.model_server: ThreadingHTTPServer | None = None
        self.processes: dict[str, subprocess.Popen] = {}
        self.repo = build_suite_repo(temp)
        self.kernel_workspace = ""
        self.runtime_token = ""
        self.auth: dict[str, str] = {}
        self.project = "chaos"

    # -- processes
    def _env(self, extra: dict[str, str]) -> dict[str, str]:
        return {**os.environ, **extra}

    def start_kernel(self) -> None:
        self.processes["kernel"] = start(
            ROOT / "veynod_kernel",
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(self.kernel_port),
             "--log-level", "warning"],
            self._env({
                "DATABASE_URL": self.kernel_dsn,
                "VN_AUTO_INIT_DB": "true",
                "VN_AUTH_MODE": "prod",
                "VN_ADMIN_API_KEY": self.admin_key,
                "VN_RUNTIME_SERVICE_KEY": self.service_key,
                "VN_REQUIRE_BOUND_TOKENS": "true",
                "VN_GATE_SANDBOX": "local",
                "VN_ALLOW_LOCAL_GATE_IN_PROD": "1",
                "VN_FAST_FORWARD_MERGE": "true",
            }),
            self.logs / "kernel.log",
        )
        wait_for(self.kernel + "/healthz", 120)

    def start_gate(self) -> None:
        self.processes["gate"] = start(
            ROOT / "veynod_kernel",
            [sys.executable, "-m", "app.worker", "--workers", str(self.gate_workers),
             "--database-url", self.kernel_dsn,
             "--metrics-port", str(free_port(8035))],
            self._env({
                "DATABASE_URL": self.kernel_dsn,
                "VN_AUTO_INIT_DB": "true",
                "VN_AUTH_MODE": "prod",
                "VN_ADMIN_API_KEY": self.admin_key,
                "VN_RUNTIME_SERVICE_KEY": self.service_key,
                "VN_GATE_SANDBOX": "local",
                "VN_ALLOW_LOCAL_GATE_IN_PROD": "1",
                "VN_FAST_FORWARD_MERGE": "true",
                **self.gate_env,
            }),
            self.logs / "gate.log",
        )

    def start_runtime_api(self) -> None:
        self.processes["runtime"] = start(
            ROOT,
            [sys.executable, "-m", "uvicorn", "agent_runtime.api:app",
             "--host", "127.0.0.1", "--port", str(self.runtime_port),
             "--log-level", "warning"],
            self._env({
                "RUNTIME_DATABASE_URL": self.runtime_dsn,
                "RUNTIME_API_KEY": self.runtime_key,
            }),
            self.logs / "runtime.log",
        )
        wait_for(self.runtime + "/healthz", 60)

    def start_product(self) -> None:
        self.processes["product"] = start(
            ROOT / "AgentGraph",
            [str(self.product_python), "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(self.api_port),
             "--log-level", "warning"],
            self._env({
                "DATABASE_URL": self.product_dsn,
                "ENVIRONMENT": "test",
                "EMBEDDING_PROVIDER": "hash",
                "ADMIN_TOKEN": self.admin_token,
                "AUTO_INIT_DB": "true",
                "MCP_ENABLED": "false",
                "AGENT_RUNTIME_URL": self.runtime,
                "AGENT_RUNTIME_KEY": self.runtime_key,
                "KERNEL_URL": self.kernel,
                "KERNEL_ADMIN_KEY": self.admin_key,
                "KERNEL_SERVICE_KEY": self.service_key,
                "KERNEL_WORKSPACE_ID": "",
                "AGENT_RUNTIME_PROJECTION_ENABLED": "false",
                "CODE_PROJECTION_ENABLED": "false",
                "EVENT_DISPATCH_ENABLED": "true",
                "SCHEDULER_ENABLED": "false",
                "CORS_ORIGINS": '["*"]',
            }),
            self.logs / "product.log",
        )
        wait_for(self.api + "/healthz", 180)

    def start_worker(self, name: str = "worker") -> None:
        self._worker_metrics = getattr(self, "_worker_metrics", 8041) + 1
        self.processes[name] = start(
            ROOT,
            [sys.executable, "-m", "agent_runtime.worker_main"],
            self._env({
                "RUNTIME_DATABASE_URL": self.runtime_dsn,
                "RUNTIME_MODEL_URL": f"http://127.0.0.1:{self.model_port}/v1",
                "RUNTIME_MODEL_API_KEY": "chaos",
                "RUNTIME_MODEL_NAME": "scripted",
                "RUNTIME_MODEL_TIMEOUT_SECONDS": "2",
                "RUNTIME_MODEL_MAX_RETRIES": "0",
                "RUNTIME_TOOL_MODE": "kernel",
                "KERNEL_URL": self.kernel,
                "KERNEL_WORKSPACE_ID": self.kernel_workspace,
                "KERNEL_RUNTIME_TOKEN": self.runtime_token,
                "KERNEL_SERVICE_KEY": self.service_key,
                "KERNEL_WORKER_HUB_ID": "chaos-runtime",
                "RUNTIME_WORKER_COUNT": str(self.worker_count),
                "RUNTIME_WORKER_LEASE_SECONDS": "10",
                "RUNTIME_WORKER_METRICS_PORT": str(self._worker_metrics),
                "RUNTIME_WORKER_POLL_SECONDS": "0.25",
                "RUNTIME_KERNEL_EVENT_INTERVAL_SECONDS": "0.5",
            }),
            self.logs / f"{name}.log",
        )

    def kill(self, name: str) -> None:
        process = self.processes.get(name)
        if process is None:
            return
        process.kill()
        try:
            process.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        self.processes.pop(name, None)

    # -- setup
    def boot(self, temp: Path) -> None:
        names = ["veynod_chaos_product", "veynod_chaos_kernel", "veynod_chaos_runtime"]
        asyncio.run(ensure_databases(names))
        base = "postgresql+asyncpg://agentgraph:agentgraph@127.0.0.1:55433/"
        self.product_dsn = base + names[0]
        self.kernel_dsn = base + names[1]
        self.runtime_dsn = base + names[2]
        self.model_server = ThreadingHTTPServer(
            ("127.0.0.1", self.model_port), build_handler(0.0, self.model)
        )
        threading.Thread(target=self.model_server.serve_forever, daemon=True).start()

        self.start_kernel()
        self.start_gate()
        self.start_runtime_api()
        self.start_product()

        workspace = http(
            "POST", self.kernel + "/v1/admin/workspaces",
            headers={"X-Admin-Key": self.admin_key},
            body={"name": "chaos", "repo_path": str(self.repo), "main_branch": "main"},
        )
        self.kernel_workspace = workspace["workspace_id"]
        self.runtime_token = http(
            "POST", self.kernel + "/v1/admin/tokens",
            headers={"X-Admin-Key": self.admin_key},
            body={"label": "chaos", "workspace_id": self.kernel_workspace},
        )["token"]
        product_workspace = http(
            "POST", self.api + "/v1/admin/workspaces",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            body={"name": "chaos", "tier": "developer_5usd"},
        )
        self.auth = {"Authorization": f"Bearer {product_workspace['api_key']}"}
        http("POST", self.api + "/v1/projects", headers=self.auth,
             body={"name": self.project, "repo": str(self.repo), "description": "chaos"})
        http("POST", self.api + f"/v1/projects/{self.project}/repositories", headers=self.auth,
             body={"repo_path": str(self.repo), "default_branch": "main",
                   "kernel_workspace_id": self.kernel_workspace,
                   "kernel_base_url": self.kernel})
        self.start_worker()

    # -- task helpers
    def new_task(self, index: int, *, run: bool = True) -> str:
        key = f"chaos-{index}-{uuid.uuid4().hex[:6]}"
        http("POST", self.api + "/v1/tasks", headers=self.auth, body={
            "task_key": key,
            "title": f"Implement compute in src/f{index}.py",
            "project": self.project,
            "repo": str(self.repo),
            "description": (
                f"Implement compute in src/f{index}.py so the frozen test "
                f"tests/test_f{index}.py passes. Symbol src/f{index}.py::compute."
            ),
            "status": "READY",
        })
        if run:
            http("POST", self.api + f"/v1/tasks/{key}/run", headers=self.auth, body={})
        return key

    def task_status(self, key: str) -> str:
        return http("GET", self.api + f"/v1/tasks/{key}", headers=self.auth)["status"]

    def wait_status(self, key: str, wanted: set[str], timeout: float = 180) -> str:
        deadline = time.monotonic() + timeout
        status = "READY"
        while time.monotonic() < deadline:
            status = self.task_status(key)
            if status in wanted:
                return status
            time.sleep(0.5)
        return status

    def execution(self, key: str) -> dict:
        return http("GET", self.api + f"/v1/tasks/{key}/execution", headers=self.auth)

    def commits_for(self, key: str) -> int:
        execution = self.execution(key)
        code = execution.get("code") or {}
        if not code.get("commit_sha"):
            return 0
        # One delivery record per task is the Kernel's own fact.
        evidence = http(
            "GET",
            self.kernel + f"/v1/runtime/tasks/{code['kernel_task_id']}/evidence",
            headers={"X-Veynod-Service-Key": self.service_key,
                     "X-Workspace-Id": self.kernel_workspace},
        )
        return 1 if evidence.get("delivery") else 0

    def stop(self) -> None:
        for name in list(self.processes):
            self.kill(name)
        if self.model_server is not None:
            self.model_server.shutdown()


# -------------------------------------------------------------- scenarios
def scenario_worker_kill(stack: Stack) -> dict:
    key = stack.new_task(0)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        steps = (stack.execution(key).get("loop") or {}).get("steps") or []
        if any(step.get("kind") == "MODEL" for step in steps):
            break
        time.sleep(0.2)
    stack.kill("worker")          # SIGKILL mid-flight
    stack.start_worker("worker2")
    status = stack.wait_status(key, {"REVIEWING", "FAILED", "CANCELLED"}, 240)
    return {
        "status": status,
        "pass": status == "REVIEWING" and stack.commits_for(key) == 1,
        "detail": f"worker killed mid-step, restarted; final={status}",
    }


def scenario_gate_kill(stack: Stack) -> dict:
    key = stack.new_task(1)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        loop = stack.execution(key).get("loop") or {}
        if (loop.get("externals") or []):
            break
        time.sleep(0.2)
    stack.kill("gate")
    stack.start_gate()
    status = stack.wait_status(key, {"REVIEWING", "FAILED", "CANCELLED"}, 240)
    return {
        "status": status,
        "pass": status == "REVIEWING" and stack.commits_for(key) == 1,
        "detail": "gate worker killed while a submission was in flight",
    }


def scenario_kernel_restart(stack: Stack) -> dict:
    key = stack.new_task(2)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if (stack.execution(key).get("loop") or {}).get("externals"):
            break
        time.sleep(0.2)
    stack.kill("kernel")
    time.sleep(1)
    stack.start_kernel()
    status = stack.wait_status(key, {"REVIEWING", "FAILED", "CANCELLED"}, 300)
    return {
        "status": status,
        "pass": status == "REVIEWING" and stack.commits_for(key) == 1,
        "detail": "Kernel API killed after submission acceptance, restarted",
    }


def scenario_duplicate_event(stack: Stack) -> dict:
    key = stack.new_task(3)
    status = stack.wait_status(key, {"REVIEWING", "FAILED", "CANCELLED"}, 240)
    execution = stack.execution(key)
    correlation = execution.get("correlation") or {}
    run_id = str(correlation.get("run_id") or "")
    code = execution.get("code") or {}
    submission = str(code.get("submission_id") or "")
    deliveries = stack.commits_for(key)
    # Deliver the same terminal event again, after the Run already finished.
    for _ in range(3):
        try:
            http(
                "POST",
                stack.runtime + f"/v1/runs/{run_id}/wakeups",
                headers={"X-Runtime-Key": stack.runtime_key},
                body={"submission_id": submission, "payload": {"submission_id": submission}},
            )
        except RuntimeError:
            pass
    time.sleep(4)
    after = stack.task_status(key)
    return {
        "status": f"{status}->{after}",
        "pass": (
            status == "REVIEWING"
            and after == "REVIEWING"
            and deliveries == 1
            and stack.commits_for(key) == 1
        ),
        "detail": "terminal event re-delivered 3x after completion",
    }


def scenario_cancel(stack: Stack) -> dict:
    key = stack.new_task(4)
    time.sleep(1.5)  # Run is executing; the Kernel node exists or is being created
    http("POST", stack.api + f"/v1/tasks/{key}/run/cancel",
         headers=stack.auth, body={"reason": "chaos cancel"})
    status = stack.wait_status(key, {"CANCELLED", "FAILED", "REVIEWING"}, 90)
    kernel_status = ""
    execution = stack.execution(key)
    code = execution.get("code") or {}
    if code.get("kernel_task_id"):
        kernel_status = http(
            "GET", stack.kernel + f"/v1/runner/tasks/{code['kernel_task_id']}",
            headers={"Authorization": f"Bearer {stack.runtime_token}"},
        ).get("status", "")
    return {
        "status": status,
        "pass": status == "CANCELLED" and kernel_status in {"CANCELLED", ""},
        "detail": f"cancel while running; kernel task={kernel_status or 'none'}",
    }


def scenario_model_timeout(stack: Stack) -> dict:
    stack.model.delay = 30.0
    try:
        key = stack.new_task(5)
        status = stack.wait_status(key, {"FAILED", "REVIEWING", "CANCELLED"}, 180)
        run = http(
            "GET",
            stack.api + f"/v1/tasks/{key}/run",
            headers=stack.auth,
        )
        last_error = str((run.get("runtime") or {}).get("last_error") or "")
        return {
            "status": status,
            "pass": status == "FAILED" and "unavailable" in last_error.lower(),
            "detail": f"model timed out; final={status} error={last_error[:80]}",
        }
    finally:
        stack.model.delay = 0.0


def scenario_pg_blip(stack: Stack) -> dict:
    key = stack.new_task(6)
    time.sleep(3)
    import asyncpg

    async def terminate() -> int:
        conn = await asyncpg.connect(
            stack.runtime_dsn.replace("postgresql+asyncpg://", "postgresql://")
        )
        try:
            rows = await conn.fetch(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
            return len(rows)
        finally:
            await conn.close()

    killed = asyncio.run(terminate())
    status = stack.wait_status(key, {"REVIEWING", "FAILED", "CANCELLED"}, 300)
    return {
        "status": status,
        "pass": status == "REVIEWING" and stack.commits_for(key) == 1,
        "detail": f"terminated {killed} database backend(s) mid-run",
    }


def scenario_two_tasks_same_repo(stack: Stack) -> dict:
    first = stack.new_task(7)
    second = stack.new_task(8)
    one = stack.wait_status(first, {"REVIEWING", "FAILED", "CANCELLED"}, 300)
    two = stack.wait_status(second, {"REVIEWING", "FAILED", "CANCELLED"}, 300)
    distinct = True
    ids = []
    for key in (first, second):
        code = stack.execution(key).get("code") or {}
        ids.append(code.get("kernel_task_id"))
    distinct = len({value for value in ids if value}) == 2
    return {
        "status": f"{one}/{two}",
        "pass": one == "REVIEWING" and two == "REVIEWING" and distinct,
        "detail": f"two tasks on one repo; kernel nodes={len(set(ids))}",
    }


def scenario_runtime_api_restart(stack: Stack) -> dict:
    key = stack.new_task(9)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if (stack.execution(key).get("loop") or {}).get("externals"):
            break
        time.sleep(0.2)
    stack.kill("runtime")
    time.sleep(1)
    stack.start_runtime_api()
    status = stack.wait_status(key, {"REVIEWING", "FAILED", "CANCELLED"}, 300)
    return {
        "status": status,
        "pass": status == "REVIEWING" and stack.commits_for(key) == 1,
        "detail": "Runtime API restarted while the Run waited for its gate",
    }


SCENARIOS = {
    "worker_kill": scenario_worker_kill,
    "gate_kill": scenario_gate_kill,
    "kernel_restart": scenario_kernel_restart,
    "duplicate_event": scenario_duplicate_event,
    "cancel": scenario_cancel,
    "model_timeout": scenario_model_timeout,
    "pg_blip": scenario_pg_blip,
    "two_tasks_same_repo": scenario_two_tasks_same_repo,
    "runtime_api_restart": scenario_runtime_api_restart,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--out", default="docs/CHAOS_TEST_MATRIX.md")
    args = parser.parse_args()
    selected = [
        name for name in (args.only.split(",") if args.only else SCENARIOS)
        if name
    ]
    unknown = [name for name in selected if name not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenarios: {unknown}")

    temp = Path(tempfile.mkdtemp(prefix="veynod-chaos-"))
    logs = temp / "logs"
    logs.mkdir()
    stack = Stack(temp, logs)
    results: list[dict] = []
    exit_code = 0
    try:
        stack.boot(temp)
        print(f"stack up: api={stack.api} kernel={stack.kernel} runtime={stack.runtime}", flush=True)
        for name in selected:
            print(f"--- {name} ---", flush=True)
            try:
                result = SCENARIOS[name](stack)
            except Exception as exc:  # noqa: BLE001 - a scenario failure is a result
                result = {"status": "error", "pass": False, "detail": str(exc)[:300]}
            result["name"] = name
            results.append(result)
            print(json.dumps(result, default=str), flush=True)
            # A crashed bit must not poison the next scenario.
            for component, starter in (
                ("kernel", "start_kernel"),
                ("gate", "start_gate"),
                ("runtime", "start_runtime_api"),
                ("product", "start_product"),
                ("worker", "start_worker"),
            ):
                if component not in stack.processes:
                    try:
                        getattr(stack, starter)()
                    except Exception:  # noqa: BLE001
                        pass
            stack.model.delay = 0.0
    finally:
        stack.stop()
        passed = sum(1 for item in results if item.get("pass"))
        lines = [
            "# Chaos test matrix",
            "",
            "Generated by `python scripts/chaos_suite.py` — real processes, real",
            "PostgreSQL, real SIGKILL; no mocked fault.",
            "",
            "| Scenario | Result | Final state | Detail |",
            "|---|---|---|---|",
        ]
        for item in results:
            lines.append(
                f"| `{item['name']}` | {'PASS' if item.get('pass') else 'FAIL'} | "
                f"{item.get('status')} | {item.get('detail')} |"
            )
        lines.append("")
        lines.append(f"**{passed}/{len(results)} scenarios passed.**")
        target = ROOT / args.out
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines[-2:]))
        if not args.keep:
            import shutil

            shutil.rmtree(temp, ignore_errors=True)
        else:
            print(f"logs kept at {logs}")
        if passed != len(results):
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
