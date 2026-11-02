"""End-to-end proof of the unified execution chain.

    Create Project -> Create Task -> Start Runtime -> Model decision
      -> KernelToolRuntime (create_task / pull_task_bundle / submit_patch)
      -> Kernel DAG scheduling -> deterministic gate -> commit -> evidence
      -> durable event -> Runtime resume -> product Task REVIEWING

Everything is real except the model: a scripted ModelGateway replaces the
provider so the run is reproducible. The Kernel, the deterministic gate, the
git merge, the Runtime loop, the wakeup watcher, the AgentGraph projection and
all three databases are the real services.

    python scripts/e2e_unified.py            # full run
    python scripts/e2e_unified.py --keep     # keep the work tree for inspection

Prereqs: PostgreSQL reachable at TEST_DATABASE_URL's host (default
127.0.0.1:55433, user agentgraph/agentgraph), `git` on PATH, pyright + pytest
importable (the Kernel gate runs them).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "AgentGraph", ROOT / "veynod_kernel"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

CALC_STUB = (
    '"""Calculator module."""\n\n\n'
    "def add(a: int, b: int) -> int:\n"
    '    """Add two integers."""\n'
    "    ...\n"
)
CALC_TESTS = (
    "from src.calc import add\n\n\n"
    "def test_add() -> None:\n"
    "    assert add(2, 3) == 5\n"
)
CALC_FIXED = (
    '"""Calculator module."""\n\n\n'
    "def add(a: int, b: int) -> int:\n"
    '    """Add two integers."""\n'
    "    return a + b\n"
)


def diff_for_calc() -> str:
    import difflib

    return "".join(
        difflib.unified_diff(
            CALC_STUB.splitlines(keepends=True),
            CALC_FIXED.splitlines(keepends=True),
            fromfile="a/src/calc.py",
            tofile="b/src/calc.py",
        )
    )


# --------------------------------------------------------------------- utils
def free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sock.connect_ex(("127.0.0.1", preferred)) != 0:
            return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1500]
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from exc


def wait_for(url: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            http("GET", url, timeout=3)
            return
        except Exception:  # noqa: BLE001 - retry until the deadline
            time.sleep(0.5)
    raise RuntimeError(f"{url} did not become healthy in {timeout}s")


def run_git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


# ---------------------------------------------------------------- databases
async def ensure_databases(names: list[str], fresh: bool = True) -> None:
    """Create (or reset) the dedicated E2E databases.

    Only ever touches names under the ``veynod_e2e_`` prefix; a run must start
    from empty state so a previous run cannot mask a regression.
    """
    import asyncpg

    for name in names:
        if not (name.startswith("veynod_e2e_") or name.startswith("veynod_chaos_")):
            raise RuntimeError(f"refusing to reset non-test database {name!r}")
    dsn = os.environ.get(
        "E2E_PG",
        "postgresql://agentgraph:agentgraph@127.0.0.1:55433/postgres",
    )
    conn = await asyncpg.connect(dsn)
    try:
        existing = {
            row[0] for row in await conn.fetch("SELECT datname FROM pg_database")
        }
        for name in names:
            if name in existing and fresh:
                await conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = $1 AND pid <> pg_backend_pid()",
                    name,
                )
                await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
                existing.discard(name)
            if name not in existing:
                await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


# ------------------------------------------------------------------ fixtures
def build_repo(root: Path) -> Path:
    work = root / "work"
    work.mkdir(parents=True)
    (work / "src").mkdir()
    (work / "tests").mkdir()
    (work / "src" / "__init__.py").write_text("", encoding="utf-8")
    (work / "src" / "calc.py").write_text(CALC_STUB, encoding="utf-8")
    (work / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (work / "tests" / "test_calc.py").write_text(CALC_TESTS, encoding="utf-8")
    run_git("init", "-b", "main", cwd=work)
    run_git("config", "user.email", "e2e@veynod.test", cwd=work)
    run_git("config", "user.name", "Veynod E2E", cwd=work)
    run_git("add", "-A", cwd=work)
    run_git("commit", "-m", "fixture", cwd=work)
    bare = root / "bare.git"
    run_git("clone", "--bare", str(work), str(bare), cwd=root)
    run_git("config", "user.email", "e2e@veynod.test", cwd=bare)
    run_git("config", "user.name", "Veynod E2E", cwd=bare)
    return bare


def start(cwd: Path, args: list[str], env: dict[str, str], log: Path) -> subprocess.Popen:
    handle = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        args, cwd=str(cwd), env={**os.environ, **env},
        stdout=handle, stderr=subprocess.STDOUT,
    )
    return process


# -------------------------------------------------------------- scripted model
def _checkpoint(context: dict[str, Any]) -> dict[str, Any]:
    for section in context.get("sections", []):
        if section.get("kind") == "checkpoint":
            content = section.get("content")
            if isinstance(content, dict):
                return content
    return {}


class ScriptedKernelModel:
    """A deterministic decision sequence for the Kernel tool protocol."""

    def __init__(self, git_diff: str) -> None:
        self.git_diff = git_diff
        self.calls: dict[str, int] = {}

    async def decide(self, run: dict[str, Any], context: dict[str, Any]):
        from agent_runtime.loop import ModelAction

        run_id = run["run_id"]
        attempt = self.calls.get(run_id, 0)
        self.calls[run_id] = attempt + 1
        checkpoint = _checkpoint(context)
        goal = str(run.get("goal") or "")

        if "external_result" in checkpoint:
            result = checkpoint.get("external_result") or {}
            status = str(result.get("status") or "").upper()
            if status in {"COMMITTED", "MERGED", "COMPLETED"}:
                return ModelAction(
                    kind="finish",
                    output={
                        "commit_sha": result.get("result_commit_sha"),
                        "submission_id": result.get("submission_id"),
                    },
                    reason="the deterministic gate committed the patch",
                )
            task_id = result.get("task_id") or checkpoint.get("kernel_task_id") or ""
            if not task_id:
                return ModelAction(
                    kind="finish",
                    output={"evidence": result},
                    reason="the gate failed and no task id is available",
                )
            return ModelAction(
                kind="tool",
                tool="get_task_evidence",
                arguments={"task_id": task_id},
                idempotency_key=f"evidence:{run_id}:{attempt}",
                reason="the gate rejected the patch; read the evidence",
            )

        if not checkpoint:
            return ModelAction(
                kind="tool",
                tool="create_task",
                arguments={
                    "title": "Implement add",
                    "spec_prompt": goal
                    or "Implement add(a, b) so the frozen test test_add passes.",
                    "type": "IMPLEMENT_BODY",
                    "target_symbols": ["src/calc.py::add"],
                    "test_symbols": ["tests/test_calc.py::test_add"],
                    "write_set_symbols": ["src/calc.py::add"],
                    "priority": 100,
                },
                idempotency_key=f"create:{run_id}",
                reason="create the Kernel DAG node for this product task",
            )

        tool = checkpoint.get("tool")
        output = checkpoint.get("output") or {}
        if tool == "create_task":
            return ModelAction(
                kind="tool",
                tool="pull_task_bundle",
                arguments={"max_context_tokens": 6000},
                idempotency_key=f"pull:{run_id}",
                reason="lease the created task",
            )
        if tool == "pull_task_bundle":
            if not output.get("leased"):
                return ModelAction(
                    kind="pause",
                    reason="no Kernel task was leasable",
                )
            return ModelAction(
                kind="tool",
                tool="submit_patch",
                arguments={
                    "task_id": output.get("task_id"),
                    "git_diff": self.git_diff,
                    "touched_symbols": ["src/calc.py::add"],
                    "local_test_passed": True,
                },
                idempotency_key=f"patch:{run_id}:{attempt}",
                reason="submit the implementation to the deterministic gate",
            )
        if tool == "get_task_evidence":
            return ModelAction(
                kind="finish",
                output={"evidence": output},
                reason="the gate evidence was read",
            )
        return ModelAction(
            kind="finish",
            output={"checkpoint": checkpoint},
            reason="nothing left to do",
        )


# -------------------------------------------------------------------- checks
async def drive_workers(
    runtime_dsn: str,
    kernel_url: str,
    service_key: str,
    runtime_token: str,
    git_diff: str,
    stop: threading.Event,
    *,
    real_model: bool = False,
) -> None:
    from agent_runtime.context import ContextBuilder
    from agent_runtime.pool import RuntimeWorkerPool
    from agent_runtime.store import RunStore
    from agent_runtime.tools import KernelToolConfig, KernelToolRuntime
    from agent_runtime.wakeups import KernelEventSource, KernelEventWatcher

    os.environ["KERNEL_RUNTIME_TOKEN"] = runtime_token
    store = RunStore(runtime_dsn)
    await store.init_schema()
    tools = KernelToolRuntime(
        KernelToolConfig(
            service_key=service_key,
            timeout_seconds=60,
            max_retries=2,
        ),
        store=store,
    )
    if real_model:
        from agent_runtime.gateway import (
            HTTPModelGateway,
            ModelGatewayConfig,
        )

        model = HTTPModelGateway(
            ModelGatewayConfig(
                base_url=os.environ["MODEL_BASE_URL"],
                api_key=os.environ["MODEL_API_KEY"],
                model=os.environ["MODEL_NAME"],
                timeout_seconds=120,
                max_retries=2,
            ),
            tool_catalog=tools.describe(),
        )
    else:
        model = ScriptedKernelModel(git_diff)
    pool = RuntimeWorkerPool(
        store,
        model,
        tools,
        workers=2,
        lease_seconds=60,
        poll_seconds=0.25,
        context_builder=ContextBuilder(token_budget=8000),
    )
    watcher = KernelEventWatcher(
        store,
        KernelEventSource(
            base_url=kernel_url, service_key=service_key, interval_seconds=0.5
        ),
    )
    await pool.start()
    watcher_task = asyncio.create_task(watcher.run_forever())
    try:
        while not stop.is_set():
            await asyncio.sleep(0.25)
    finally:
        watcher.stop()
        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass
        await pool.stop()
        await watcher.close()
        await tools.close()
        await store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="keep the temp tree")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument(
        "--real-model",
        action="store_true",
        help="use the configured OpenAI-compatible provider instead of the script",
    )
    parser.add_argument(
        "--restart-gate",
        action="store_true",
        help="kill and restart the gate worker while the submission is in flight",
    )
    args = parser.parse_args()

    api_port = free_port(8100)
    kernel_port = free_port(8101)
    runtime_port = free_port(8102)
    api_url = f"http://127.0.0.1:{api_port}"
    kernel_url = f"http://127.0.0.1:{kernel_port}"
    runtime_url = f"http://127.0.0.1:{runtime_port}"

    dbs = ["veynod_e2e_product", "veynod_e2e_kernel", "veynod_e2e_runtime"]
    asyncio.run(ensure_databases(dbs))
    product_dsn = (
        f"postgresql+asyncpg://agentgraph:agentgraph@127.0.0.1:55433/{dbs[0]}"
    )
    kernel_dsn = (
        f"postgresql+asyncpg://agentgraph:agentgraph@127.0.0.1:55433/{dbs[1]}"
    )
    runtime_dsn = (
        f"postgresql+asyncpg://agentgraph:agentgraph@127.0.0.1:55433/{dbs[2]}"
    )

    temp = Path(tempfile.mkdtemp(prefix="veynod-e2e-"))
    bare = build_repo(temp)
    logs = temp / "logs"
    logs.mkdir()
    service_key = "e2e-service-key-0123456789abcdef0123456789abcdef"
    admin_key = "e2e-admin-key-0123456789abcdef0123456789abcdef"
    runtime_key = "e2e-runtime-key-0123456789abcdef0123456789abcdef"
    admin_token = "e2e-admin-token-00000000000000000000"

    product_python = ROOT / "AgentGraph" / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    if not product_python.exists():
        product_python = Path(sys.executable)

    processes: list[subprocess.Popen] = []
    runtime_token = ""
    try:
        kernel = start(
            ROOT / "veynod_kernel",
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(kernel_port),
             "--log-level", "warning"],
            {
                "DATABASE_URL": kernel_dsn,
                "VN_AUTO_INIT_DB": "true",
                "VN_AUTH_MODE": "prod",
                "VN_ADMIN_API_KEY": admin_key,
                "VN_RUNTIME_SERVICE_KEY": service_key,
                "VN_REQUIRE_BOUND_TOKENS": "true",
                "VN_GATE_SANDBOX": "local",
                "VN_ALLOW_LOCAL_GATE_IN_PROD": "1",
            },
            logs / "kernel.log",
        )
        processes.append(kernel)
        wait_for(kernel_url + "/healthz", 120)

        gate = start(
            ROOT / "veynod_kernel",
            [sys.executable, "-m", "app.worker",
             "--workers", "2",
             "--database-url", kernel_dsn,
             "--metrics-port", str(free_port(8030))],
            {
                "DATABASE_URL": kernel_dsn,
                "VN_AUTO_INIT_DB": "true",
                "VN_AUTH_MODE": "prod",
                "VN_ADMIN_API_KEY": admin_key,
                "VN_RUNTIME_SERVICE_KEY": service_key,
                "VN_GATE_SANDBOX": "local",
                "VN_ALLOW_LOCAL_GATE_IN_PROD": "1",
            },
            logs / "gate.log",
        )
        processes.append(gate)

        runtime_api = start(
            ROOT,
            [sys.executable, "-m", "uvicorn", "agent_runtime.api:app",
             "--host", "127.0.0.1", "--port", str(runtime_port),
             "--log-level", "warning"],
            {
                "RUNTIME_DATABASE_URL": runtime_dsn,
                "RUNTIME_API_KEY": runtime_key,
            },
            logs / "runtime.log",
        )
        processes.append(runtime_api)
        wait_for(runtime_url + "/healthz", 60)

        product = start(
            ROOT / "AgentGraph",
            [str(product_python), "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(api_port),
             "--log-level", "warning"],
            {
                "DATABASE_URL": product_dsn,
                "ENVIRONMENT": "test",
                "EMBEDDING_PROVIDER": "hash",
                "ADMIN_TOKEN": admin_token,
                "AUTO_INIT_DB": "true",
                "MCP_ENABLED": "false",
                "AGENT_RUNTIME_URL": runtime_url,
                "AGENT_RUNTIME_KEY": runtime_key,
                "KERNEL_URL": kernel_url,
                "KERNEL_ADMIN_KEY": admin_key,
                # Clear the developer .env value: this E2E has its own workspace.
                "KERNEL_WORKSPACE_ID": "",
                "AGENT_RUNTIME_PROJECTION_ENABLED": "false",
                "CODE_PROJECTION_ENABLED": "false",
                "EVENT_DISPATCH_ENABLED": "true",
                "SCHEDULER_ENABLED": "false",
                "CORS_ORIGINS": '["*"]',
            },
            logs / "product.log",
        )
        processes.append(product)
        wait_for(api_url + "/healthz", 180)

        # 1. Kernel workspace + the Runtime's own runner token.
        kernel_workspace = http(
            "POST",
            kernel_url + "/v1/admin/workspaces",
            headers={"X-Admin-Key": admin_key},
            body={"name": "e2e", "repo_path": str(bare), "main_branch": "main"},
        )
        runtime_token = http(
            "POST",
            kernel_url + "/v1/admin/tokens",
            headers={"X-Admin-Key": admin_key},
            body={
                "label": "e2e-runtime",
                "workspace_id": kernel_workspace["workspace_id"],
            },
        )["token"]

        # 2. Product workspace, project and repository binding.
        product_workspace = http(
            "POST",
            api_url + "/v1/admin/workspaces",
            headers={"Authorization": f"Bearer {admin_token}"},
            body={"name": "e2e", "tier": "developer_5usd"},
        )
        auth = {"Authorization": f"Bearer {product_workspace['api_key']}"}
        http(
            "POST",
            api_url + "/v1/projects",
            headers=auth,
            body={"name": "calc", "repo": str(bare), "description": "e2e"},
        )
        http(
            "POST",
            api_url + "/v1/projects/calc/repositories",
            headers=auth,
            body={
                "repo_path": str(bare),
                "default_branch": "main",
                "kernel_workspace_id": kernel_workspace["workspace_id"],
                "kernel_base_url": kernel_url,
                "credential_ref": "KERNEL_RUNTIME_TOKEN",
            },
        )

        # 3. Product task + its one Agent Run.
        task_key = f"e2e-{int(time.time())}"
        http(
            "POST",
            api_url + "/v1/tasks",
            headers=auth,
            body={
                "task_key": task_key,
                "title": "Implement add",
                "project": "calc",
                "repo": str(bare),
                "description": (
                    "Implement add(a, b) in src/calc.py so the frozen test "
                    "tests/test_calc.py::test_add passes. Only "
                    "src/calc.py::add is writable."
                ),
                "status": "READY",
            },
        )

        stop = threading.Event()

        def _run_workers() -> None:
            try:
                asyncio.run(
                    drive_workers(
                        runtime_dsn,
                        kernel_url,
                        service_key,
                        runtime_token,
                        diff_for_calc(),
                        stop,
                        real_model=args.real_model,
                    )
                )
            except Exception:  # noqa: BLE001 - surfaced by the assertions
                import traceback

                traceback.print_exc()

        worker_thread = threading.Thread(target=_run_workers, daemon=True)
        worker_thread.start()
        time.sleep(1.5)
        started = http(
            "POST",
            api_url + f"/v1/tasks/{task_key}/run",
            headers=auth,
            body={},
        )
        print(f"run started: {started['run_id']} trace={started['trace_id']}")

        if args.restart_gate:
            # Requirement D.3: a Gate Worker restart must not lose or duplicate
            # the submission. Kill it as soon as the Runtime has a submission.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                rows = http(
                    "GET",
                    runtime_url + f"/v1/runs/{started['run_id']}/external",
                    headers={"X-Runtime-Key": runtime_key},
                )
                if rows:
                    break
                time.sleep(0.2)
            gate.terminate()
            gate.wait(timeout=20)
            print("gate worker stopped mid-submission; restarting")
            gate = start(
                ROOT / "veynod_kernel",
                [sys.executable, "-m", "app.worker",
                 "--workers", "2",
                 "--database-url", kernel_dsn,
                 "--metrics-port", str(free_port(8031))],
                {
                    "DATABASE_URL": kernel_dsn,
                    "VN_AUTO_INIT_DB": "true",
                    "VN_AUTH_MODE": "prod",
                    "VN_ADMIN_API_KEY": admin_key,
                    "VN_RUNTIME_SERVICE_KEY": service_key,
                    "VN_GATE_SANDBOX": "local",
                    "VN_ALLOW_LOCAL_GATE_IN_PROD": "1",
                },
                logs / "gate-restart.log",
            )
            processes.append(gate)

        # 4. Wait for the product task to reach human review.
        deadline = time.monotonic() + args.timeout
        status = None
        while time.monotonic() < deadline:
            detail = http("GET", api_url + f"/v1/tasks/{task_key}", headers=auth)
            status = detail["status"]
            if status in {"REVIEWING", "FAILED", "CANCELLED"}:
                break
            time.sleep(1.0)
        stop.set()
        worker_thread.join(timeout=30)

        execution = http(
            "GET", api_url + f"/v1/tasks/{task_key}/execution", headers=auth
        )
        runtime_headers = {"X-Runtime-Key": runtime_key}
        run_state = http(
            "GET", runtime_url + f"/v1/runs/{started['run_id']}", headers=runtime_headers
        )
        run_steps = http(
            "GET",
            runtime_url + f"/v1/runs/{started['run_id']}/steps?limit=200",
            headers=runtime_headers,
        )
        tool_errors = sum(
            1
            for step in run_steps
            if step.get("kind") == "TOOL" and step.get("status") == "FAILED"
        )
        report = {
            "task_status": status,
            "run_id": started["run_id"],
            "correlation": execution["correlation"],
            "loop_status": (execution.get("loop") or {}).get("status"),
            "code": execution.get("code"),
            "gates": (execution.get("verification") or {}).get("gate_results"),
            "evidence_complete": (execution.get("verification") or {}).get(
                "evidence_complete"
            ),
            "delivery": execution.get("delivery"),
            "usage": run_state.get("usage") or {},
            "model_calls": int((run_state.get("usage") or {}).get("model_calls") or 0),
            "steps": len(run_steps),
            "tool_errors": tool_errors,
        }
        print(json.dumps(report, indent=2, default=str))

        ok = (
            bool((execution or {}).get("code"))
            and status == "REVIEWING"
            and report["evidence_complete"]
            and report["delivery"] is not None
            and all(
                item.get("passed") is True
                for item in (report["gates"] or [])
            )
        )
        if not ok:
            print("FAILED: see", logs, file=sys.stderr)
            return 1
        commit = (report["code"] or {}).get("commit_sha")
        print(f"OK: merged {commit} and the task is in REVIEWING")
        return 0
    except Exception as exc:  # noqa: BLE001 - report, do not swallow
        print(f"ERROR: {exc}", file=sys.stderr)
        for name in ("kernel", "runtime", "product"):
            path = logs / f"{name}.log"
            if path.exists():
                print(f"--- {path.name} (tail) ---", file=sys.stderr)
                print(path.read_text(encoding="utf-8")[-4000:], file=sys.stderr)
        return 2
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except Exception:  # noqa: BLE001
                process.kill()
        if not args.keep:
            import shutil

            shutil.rmtree(temp, ignore_errors=True)
        else:
            print(f"work tree kept at {temp}")


if __name__ == "__main__":
    raise SystemExit(main())
