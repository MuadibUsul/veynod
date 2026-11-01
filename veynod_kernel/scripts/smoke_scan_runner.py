"""Cold-start red/green smoke: scan stubs -> runner writes a red test -> runner implements.

This validates the WRITE_TEST pre-flight semantics (collection only) and the
subsequent IMPLEMENT_BODY green gate through the real Node runner.

Usage:
    python scripts/smoke_scan_runner.py
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
API = "http://127.0.0.1:8012"
ADMIN_KEY = "scan-runner-admin"


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
    run(["git", "config", "user.name", "ScanSmoke"], work)
    run(["git", "config", "user.email", "scan@example.com"], work)
    (work / "src").mkdir()
    (work / "src" / "__init__.py").write_text("", encoding="utf-8")
    (work / "src" / "lib.py").write_text(
        '"""lib"""\n\n\ndef alpha(x: int) -> int:\n    """alpha"""\n    ...\n',
        encoding="utf-8",
    )
    run(["git", "add", "-A"], work)
    run(["git", "commit", "-m", "seed"], work)
    bare = base / "bare.git"
    run(["git", "clone", "--bare", str(work), str(bare)], base)
    return bare


def fake_agent(path: Path) -> None:
    path.write_text(
        """import json
import os
import pathlib
import sys

bundle = json.load(open(os.environ["VEYNOD_TASK_FILE"], encoding="utf-8"))
task_type = bundle.get("task_type")
if task_type == "WRITE_TEST":
    target = bundle["mutable_stubs"][0]["symbol_path"]
    test_file, test_name = target.split("::", 1)
    source = bundle["read_only_signatures"][0]["symbol_path"]
    source_file, function = source.split("::", 1)
    module = source_file[:-3].replace("/", ".")
    path = pathlib.Path(test_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"from {module} import {function}\\n\\n\\n"
        f"def {test_name.split('::')[-1]}():\\n"
        f"    assert {function}(2) == 3\\n",
        encoding="utf-8",
    )
    print("fake agent wrote a red test")
elif task_type == "IMPLEMENT_BODY":
    target = bundle["mutable_stubs"][0]["symbol_path"].split("::", 1)[0]
    path = pathlib.Path(target)
    content = path.read_text(encoding="utf-8")
    if "    ..." not in content:
        print("stub already filled", file=sys.stderr)
        sys.exit(3)
    path.write_text(content.replace("    ...", "    return x + 1"), encoding="utf-8")
    print("fake agent implemented alpha")
else:
    print(f"unexpected task type {task_type!r}", file=sys.stderr)
    sys.exit(4)
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


def run_runner_once(token: str, bare: Path, agent: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "VEYNOD_API_URL": API,
        "VEYNOD_AGENT_TOKEN": token,
        "VEYNOD_REPO_ROOT": str(bare),
        "VEYNOD_LOCAL_TEST_CMD": "pytest -q --tb=short",
        "VEYNOD_WRITE_TEST_CMD": "pytest --collect-only -q",
    }
    result = subprocess.run(
        [
            "node",
            "cli/bin/runner.mjs",
            "--once",
            "--runner-id",
            "scan-smoke-runner",
            "--agent",
            sys.executable,
            "--agent-args",
            str(agent),
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    print(result.stdout[-4000:])
    if result.returncode != 0:
        print(result.stderr[-4000:], file=sys.stderr)
    assert result.returncode == 0, result.returncode
    return result


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
    base = Path(tempfile.mkdtemp(prefix="veynod-scan-smoke-"))
    server_env = {
        **os.environ,
        "DATABASE_URL": DB_URL,
        "VN_AUTH_MODE": "prod",
        "VN_ADMIN_API_KEY": ADMIN_KEY,
        "VN_SKIP_TYPECHECK": "true",
        "VN_SKIP_LINT": "true",
        "VN_TEST_CMD": "pytest -q --tb=short",
        "VN_COLLECT_CMD": "pytest --collect-only -q",
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
            "8012",
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
                json={"name": "scan-smoke", "repo_path": str(bare), "main_branch": "main"},
            )
            workspace.raise_for_status()
            workspace_id = workspace.json()["workspace_id"]

            token_response = client.post(
                f"{API}/v1/admin/tokens", headers=admin, json={"label": "scan-runner"}
            )
            token_response.raise_for_status()
            token = token_response.json()["token"]

            scan = client.post(
                f"{API}/v1/admin/workspaces/{workspace_id}/scan-stubs",
                headers=admin,
                json={"dry_run": False},
            )
            scan.raise_for_status()
            assert scan.json()["created_write_tests"] == 1, scan.text
            assert scan.json()["created_implementations"] == 1, scan.text

            agent = base / "fake_agent.py"
            fake_agent(agent)

            run_runner_once(token, bare, agent)
            run_runner_once(token, bare, agent)

            tasks = client.get(
                f"{API}/v1/admin/tasks?workspace_id={workspace_id}", headers=admin
            ).json()
            by_type = {task["type"]: task for task in tasks}
            assert by_type["WRITE_TEST"]["status"] == "COMMITTED", by_type
            assert by_type["IMPLEMENT_BODY"]["status"] == "COMMITTED", by_type

            source = run(["git", "show", "main:src/lib.py"], bare)
            assert "return x + 1" in source, source
            tests = run(["git", "ls-tree", "-r", "--name-only", "main"], bare)
            assert "tests/" in tests and "test_src_lib" in tests, tests
            success = True
            print("SCAN + RED/GREEN RUNNER SMOKE OK")
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
