"""True process-restart test for durable v2 patch submissions.

Runs a real uvicorn process with submission workers disabled, accepts a patch
(202), closes the client (disconnect), kills the server, restarts it with
workers enabled, and proves the persisted submission reaches COMMITTED.

Every database is freshly created and dropped by this test; the existing
`veynod_test` database is never touched.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_test",
)
ADMIN_KEY = "restart-admin-0123456789abcdef0123"


def _admin_url() -> str:
    if os.environ.get("BENCH_ADMIN_DATABASE_URL"):
        return os.environ["BENCH_ADMIN_DATABASE_URL"]
    raw = TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
    parts = urlsplit(raw)
    return urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))


def _run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise RuntimeError(f"{cmd} failed\n{result.stdout}\n{result.stderr}")
    return result.stdout


def _make_repo(base: Path) -> tuple[Path, str]:
    work = base / "work"
    work.mkdir(parents=True)
    _run(["git", "init", "-b", "main"], work)
    _run(["git", "config", "user.name", "Restart"], work)
    _run(["git", "config", "user.email", "restart@example.com"], work)
    (work / "src").mkdir()
    (work / "src" / "__init__.py").write_text("", encoding="utf-8")
    (work / "src" / "calc.py").write_text(
        '"""Calculator."""\n\n\ndef add(a: int, b: int) -> int:\n'
        '    """Add two integers."""\n    ...\n',
        encoding="utf-8",
    )
    (work / "tests").mkdir()
    (work / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (work / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add() -> None:\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    _run(["git", "add", "-A"], work)
    _run(["git", "commit", "-m", "seed"], work)
    head = _run(["git", "rev-parse", "HEAD"], work).strip()
    bare = base / "bare.git"
    _run(["git", "clone", "--bare", str(work), str(bare)], base)
    return bare, head


def _patch(base: Path, bare: Path, head: str) -> str:
    worktree = base / "patch-wt"
    _run(["git", "worktree", "add", "--detach", str(worktree), head], bare)
    try:
        target = worktree / "src" / "calc.py"
        target.write_text(
            target.read_text(encoding="utf-8").replace("    ...", "    return a + b"),
            encoding="utf-8",
        )
        return _run(["git", "diff", "HEAD"], worktree)
    finally:
        _run(["git", "worktree", "remove", "--force", str(worktree)], bare)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_server(
    port: int, db_url: str, log_path: Path, workers: int
) -> subprocess.Popen:
    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "VN_AUTH_MODE": "prod",
        "VN_ADMIN_API_KEY": ADMIN_KEY,
        "VN_SKIP_TYPECHECK": "true",
        "VN_SKIP_LINT": "true",
        "VN_FAST_FORWARD_MERGE": "true",
        "VN_GATE_WORKTREE_POOL_SIZE": "1",
        "VN_REAPER_INTERVAL_SECONDS": "3600",
        "VN_SUBMISSION_WORKERS": str(workers),
        "VN_SUBMISSION_POLL_SECONDS": "0.05",
        "VN_TEST_CMD": "pytest -q --tb=short",
        "PYTHONUNBUFFERED": "1",
    }
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    handle = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            "--backlog",
            "128",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        **kwargs,
    )
    proc._veynod_log = handle  # type: ignore[attr-defined]
    deadline = time.monotonic() + 60
    with httpx.Client(timeout=5.0) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited: {log_path.read_text(errors='replace')}")
            try:
                if client.get(f"http://127.0.0.1:{port}/healthz").status_code == 200:
                    return proc
            except Exception:
                pass
            time.sleep(0.1)
    raise RuntimeError(f"server did not become healthy: {log_path}")


def _stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
            )
        else:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    handle = getattr(proc, "_veynod_log", None)
    if handle is not None:
        handle.flush()
        handle.close()


def _start_gate_worker(db_url: str, log_path: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "VN_SKIP_TYPECHECK": "true",
        "VN_SKIP_LINT": "true",
        "VN_TEST_CMD": "pytest -q --tb=short",
        "VN_GATE_TIMEOUT_SECONDS": "300",
        "VN_WORK_LEASE_SECONDS": "120",
        "VN_WORK_HEARTBEAT_SECONDS": "10",
        "VN_WORKER_POLL_SECONDS": "0.05",
        "VN_GATE_SANDBOX": "local",
        "VN_ALLOW_LOCAL_GATE_IN_PROD": "true",
        "PYTHONUNBUFFERED": "1",
    }
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    handle = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "app.worker",
            "--workers",
            "1",
            "--database-url",
            db_url,
            "--work-lease-seconds",
            "120",
            "--poll-seconds",
            "0.05",
            "--sandbox",
            "local",
            "--metrics-port",
            "0",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        **kwargs,
    )
    proc._veynod_log = handle  # type: ignore[attr-defined]
    time.sleep(1.0)
    if proc.poll() is not None:
        raise RuntimeError(
            "gate worker exited during startup:\n"
            + log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        )
    return proc


def _stop_gate_worker(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
            )
        else:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    handle = getattr(proc, "_veynod_log", None)
    if handle is not None:
        handle.flush()
        handle.close()


def _available() -> bool:
    async def probe() -> bool:
        try:
            conn = await asyncpg.connect(_admin_url(), timeout=3)
            await conn.close()
            return True
        except Exception:
            return False

    return asyncio.run(probe())


pytestmark = pytest.mark.integration


@pytest.mark.skipif(not _available(), reason="PostgreSQL admin database unavailable")
def test_submission_survives_real_server_restart_and_client_disconnect(
    tmp_path: Path,
) -> None:
    try:
        import veynod_core  # noqa: F401
    except Exception:
        pytest.skip("veynod_core extension is not built")

    db_name = f"veynod_restart_test_{int(time.time())}_{os.getpid()}"
    admin_url = _admin_url()
    admin_dsn = admin_url.replace("postgresql://", "postgresql://", 1)
    bare, head = _make_repo(tmp_path)
    port = _free_port()
    log_path = tmp_path / "server.log"
    db_url = TEST_DB_URL.replace(
        TEST_DB_URL.rsplit("/", 1)[-1].split("?")[0], db_name
    )
    proc1 = proc2 = worker = None
    try:
        async def create_db() -> None:
            conn = await asyncpg.connect(admin_dsn)
            try:
                await conn.execute(f'CREATE DATABASE "{db_name}"')
            finally:
                await conn.close()

        asyncio.run(create_db())
        proc1 = _start_server(port, db_url, log_path, workers=0)
        api = f"http://127.0.0.1:{port}"
        with httpx.Client(timeout=30.0) as client:
            admin = {"X-Admin-Key": ADMIN_KEY}
            workspace = client.post(
                f"{api}/v1/admin/workspaces",
                headers=admin,
                json={"name": "restart", "repo_path": str(bare), "main_branch": "main"},
            )
            workspace.raise_for_status()
            workspace_id = workspace.json()["workspace_id"]
            token = client.post(
                f"{api}/v1/admin/tokens",
                headers=admin,
                json={"label": "restart-runner"},
            ).json()["token"]
            headers = {"Authorization": f"Bearer {token}"}
            for symbol, kind in (
                ("src/calc.py::add", "stub"),
                ("tests/test_calc.py::test_add", "test"),
            ):
                response = client.post(
                    f"{api}/v1/admin/artifacts",
                    headers=admin,
                    json={
                        "workspace_id": workspace_id,
                        "symbol_path": symbol,
                        "kind": kind,
                        "status": "FROZEN",
                    },
                )
                response.raise_for_status()
            task = client.post(
                f"{api}/v1/admin/tasks",
                headers=admin,
                json={
                    "workspace_id": workspace_id,
                    "title": "Restart add",
                    "spec_prompt": "Implement add so the frozen test passes.",
                    "type": "IMPLEMENT_BODY",
                    "target_symbols": ["src/calc.py::add"],
                    "test_symbols": ["tests/test_calc.py::test_add"],
                    "write_set_symbols": ["src/calc.py::add"],
                },
            )
            task.raise_for_status()
            task_id = task.json()["task_id"]
            bundle = client.post(
                f"{api}/v1/syscalls/pull_task_bundle",
                headers=headers,
                json={
                    "worker_hub_id": "restart-runner",
                    "supported_task_types": ["IMPLEMENT_BODY"],
                    "max_context_tokens": 6000,
                },
            ).json()
            assert bundle["leased"] is True, bundle
            diff = _patch(tmp_path, bare, head)
            key = hashlib.sha256(
                f"{task_id}:{bundle['lease_token']}:{diff}".encode("utf-8")
            ).hexdigest()
            accepted = client.post(
                f"{api}/v2/syscalls/submit_patch",
                headers={**headers, "Idempotency-Key": key},
                json={
                    "task_id": task_id,
                    "lease_token": bundle["lease_token"],
                    "git_diff": diff,
                    "touched_symbols": ["src/calc.py::add"],
                    "local_test_passed": True,
                },
            )
            assert accepted.status_code == 202, accepted.text
            submission_id = accepted.json()["submission_id"]

        # Client is now closed (disconnect), then the server is killed.
        _stop_server(proc1)
        proc1 = None
        proc2 = _start_server(port, db_url, log_path, workers=1)
        worker = _start_gate_worker(db_url, log_path.with_name("worker.log"))
        deadline = time.monotonic() + 120
        final = None
        with httpx.Client(timeout=30.0) as client:
            while time.monotonic() < deadline:
                response = client.get(
                    f"{api}/v2/submissions/{submission_id}",
                    headers=headers,
                )
                assert response.status_code == 200, response.text
                final = response.json()
                if final["final"]:
                    break
                time.sleep(0.1)
        assert final is not None and final["final"], final
        assert final["status"] == "COMMITTED", final
        assert final["result_commit_sha"], final
        assert "return a + b" in _run(["git", "show", "main:src/calc.py"], bare)
    finally:
        if proc1 is not None:
            _stop_server(proc1)
        if proc2 is not None:
            _stop_server(proc2)
        if worker is not None:
            _stop_gate_worker(worker)

        async def drop_db() -> None:
            conn = await asyncpg.connect(admin_dsn)
            try:
                await conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = $1 AND pid <> pg_backend_pid()",
                    db_name,
                )
                await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
            finally:
                await conn.close()

        try:
            asyncio.run(drop_db())
        except Exception:
            pass
        shutil.rmtree(tmp_path, ignore_errors=True)
