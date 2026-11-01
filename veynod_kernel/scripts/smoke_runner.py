"""End-to-end smoke for the Node runner against a live kernel.

Starts uvicorn on :8011, creates a bare repo + workspace + frozen stub task,
then runs cli/bin/runner.mjs with a fake coding agent that fills the stub.
Asserts the kernel gate committed the patch and the worktree was cleaned up.

Usage:
    python scripts/smoke_runner.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_test",
)
API = "http://127.0.0.1:8011"
ADMIN_KEY = "runner-smoke-admin"


def run(cmd: list[str], cwd: Path, env: dict | None = None) -> str:
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, **(env or {})},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{cmd} failed ({result.returncode})\n{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def reset_database() -> None:
    import asyncio

    import asyncpg

    async def reset() -> None:
        conn = await asyncpg.connect(
            DB_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
        )
        try:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        finally:
            await conn.close()

    asyncio.run(reset())


def make_repo(base: Path) -> Path:
    work = base / "work"
    work.mkdir(parents=True)
    run(["git", "init", "-b", "main"], work)
    run(["git", "config", "user.name", "RunnerSmoke"], work)
    run(["git", "config", "user.email", "runner@example.com"], work)
    (work / "src").mkdir()
    (work / "src" / "__init__.py").write_text("", encoding="utf-8")
    (work / "src" / "calc.py").write_text(
        '"""calc"""\n\n\ndef add(a: int, b: int) -> int:\n    """add"""\n    ...\n',
        encoding="utf-8",
    )
    (work / "tests").mkdir()
    (work / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    run(["git", "add", "-A"], work)
    run(["git", "commit", "-m", "seed"], work)
    bare = base / "bare.git"
    run(["git", "clone", "--bare", str(work), str(bare)], base)
    return bare


def fake_agent(path: Path) -> None:
    path.write_text(
        """import pathlib
import sys

target = pathlib.Path("src/calc.py")
content = target.read_text(encoding="utf-8")
if "    ..." not in content:
    print("stub already filled", file=sys.stderr)
    sys.exit(3)
target.write_text(content.replace("    ...", "    return a + b"), encoding="utf-8")
print("fake agent implemented add()")
""",
        encoding="utf-8",
    )


def wait_health(client: httpx.Client) -> None:
    for _ in range(120):
        try:
            if client.get(f"{API}/healthz").status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("kernel did not become healthy")


def _assert_test_database() -> None:
    name = DB_URL.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in name.lower() and os.environ.get(
        "VEYNOD_ALLOW_DESTRUCTIVE_TESTS"
    ) != "1":
        raise SystemExit(
            f"Refusing to DROP SCHEMA on non-test database {name!r}. "
            "Point TEST_DATABASE_URL at a disposable test database or set "
            "VEYNOD_ALLOW_DESTRUCTIVE_TESTS=1."
        )


def main() -> None:
    _assert_test_database()
    reset_database()
    base = Path(tempfile.mkdtemp(prefix="veynod-runner-smoke-"))
    server_env = {
        **os.environ,
        "DATABASE_URL": DB_URL,
        "VN_AUTH_MODE": "prod",
        "VN_ADMIN_API_KEY": ADMIN_KEY,
        "VN_SKIP_TYPECHECK": "true",
        "VN_SKIP_LINT": "true",
        "VN_TEST_CMD": "pytest -q --tb=short",
        "VN_REAPER_INTERVAL_SECONDS": "3600",
    }
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8011",
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT),
        env=server_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    success = False
    try:
        with httpx.Client(timeout=30) as client:
            wait_health(client)
            bare = make_repo(base)
            admin = {"X-Admin-Key": ADMIN_KEY}

            workspace = client.post(
                f"{API}/v1/admin/workspaces",
                headers=admin,
                json={"name": "runner-smoke", "repo_path": str(bare), "main_branch": "main"},
            )
            workspace.raise_for_status()
            workspace_id = workspace.json()["workspace_id"]

            token_response = client.post(
                f"{API}/v1/admin/tokens", headers=admin, json={"label": "runner-smoke"}
            )
            token_response.raise_for_status()
            token = token_response.json()["token"]

            for symbol, kind in [
                ("src/calc.py::add", "stub"),
                ("tests/test_calc.py::test_add", "test"),
            ]:
                artifact = client.post(
                    f"{API}/v1/admin/artifacts",
                    headers=admin,
                    json={
                        "workspace_id": workspace_id,
                        "symbol_path": symbol,
                        "kind": kind,
                        "status": "FROZEN",
                    },
                )
                artifact.raise_for_status()

            task_response = client.post(
                f"{API}/v1/admin/tasks",
                headers=admin,
                json={
                    "workspace_id": workspace_id,
                    "title": "Implement add via runner",
                    "spec_prompt": "Implement add(a,b) so the frozen test passes.",
                    "type": "IMPLEMENT_BODY",
                    "target_symbols": ["src/calc.py::add"],
                    "test_symbols": ["tests/test_calc.py::test_add"],
                    "write_set_symbols": ["src/calc.py::add"],
                },
            )
            task_response.raise_for_status()
            task_id = task_response.json()["task_id"]

            agent_script = base / "fake_agent.py"
            fake_agent(agent_script)

            runner_env = {
                **os.environ,
                "VEYNOD_API_URL": API,
                "VEYNOD_AGENT_TOKEN": token,
                "VEYNOD_REPO_ROOT": str(bare),
                "VEYNOD_LOCAL_TEST_CMD": "pytest -q --tb=short",
            }
            runner = subprocess.run(
                [
                    "node",
                    "cli/bin/runner.mjs",
                    "--once",
                    "--runner-id",
                    "smoke-runner",
                    "--agent",
                    sys.executable,
                    "--agent-args",
                    str(agent_script),
                ],
                cwd=str(ROOT),
                env=runner_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=300,
            )
            print(runner.stdout[-4000:])
            if runner.returncode != 0:
                print(runner.stderr[-4000:], file=sys.stderr)
            assert runner.returncode == 0, runner.returncode

            task = client.get(f"{API}/v1/admin/tasks/{task_id}", headers=admin).json()
            assert task["status"] == "COMMITTED", task
            merged = run(["git", "show", "main:src/calc.py"], bare)
            assert "return a + b" in merged, merged
            worktrees = run(["git", "worktree", "list", "--porcelain"], bare)
            # The runner's per-task worktree must be gone; the gate keeps a
            # small persistent pool by design.
            assert "veynod-wt-" not in worktrees, worktrees
            success = True
            print("RUNNER SMOKE OK")
    finally:
        server.terminate()
        try:
            output = server.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            server.kill()
            output = ""
        if not success:
            print(output or "", file=sys.stderr)
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
