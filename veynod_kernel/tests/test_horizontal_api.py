"""Multi-API claim competition, SSE replay and 1000 long-lived SSE streams."""

from __future__ import annotations

import asyncio
import json
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
ADMIN_KEY = "horizontal-admin-0123456789abcdef"
pytestmark = pytest.mark.integration


def _admin_url() -> str:
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


def _make_repo(base: Path) -> Path:
    work = base / "work"
    work.mkdir()
    _run(["git", "init", "-b", "main"], work)
    _run(["git", "config", "user.name", "Horiz"], work)
    _run(["git", "config", "user.email", "horiz@example.com"], work)
    (work / "src").mkdir()
    (work / "src" / "__init__.py").write_text("", encoding="utf-8")
    for index in range(10):
        (work / "src" / f"f{index:03d}.py").write_text(
            "def f() -> int:\n    ...\n", encoding="utf-8"
        )
    _run(["git", "add", "-A"], work)
    _run(["git", "commit", "-m", "seed"], work)
    bare = base / "bare.git"
    _run(["git", "clone", "--bare", str(work), str(bare)], base)
    return bare


def _free_port(used: set[int]) -> int:
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in used:
            used.add(port)
            return port
    raise RuntimeError("could not reserve two distinct ports")


def _db_dsn(db_name: str) -> str:
    raw = _admin_url()
    parts = urlsplit(raw)
    return urlunsplit((parts.scheme, parts.netloc, f"/{db_name}", "", ""))


def _start(port: int, db_url: str, log_path: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "VN_AUTH_MODE": "prod",
        "VN_ADMIN_API_KEY": ADMIN_KEY,
        "VN_SKIP_TYPECHECK": "true",
        "VN_SKIP_LINT": "true",
        "VN_SINGLE_KERNEL_CLAIM": "true",
        "VN_REAPER_INTERVAL_SECONDS": "3600",
        "VN_AUTO_INIT_DB": "true",
        "PYTHONUNBUFFERED": "1",
    }
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    handle = log_path.open("w", encoding="utf-8")
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
            "4096",
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
                raise RuntimeError(log_path.read_text(errors="replace")[-4000:])
            try:
                if client.get(f"http://127.0.0.1:{port}/healthz").status_code == 200:
                    return proc
            except Exception:
                pass
            time.sleep(0.1)
    raise RuntimeError("API did not become healthy")


def _stop(proc: subprocess.Popen) -> None:
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


@pytest.mark.skipif(not _available(), reason="PostgreSQL admin database unavailable")
def test_two_apis_compete_for_claims_and_sse_replays(tmp_path: Path) -> None:
    try:
        import veynod_core  # noqa: F401
    except Exception:
        pytest.skip("veynod_core extension is not built")

    db_name = f"veynod_horizontal_test_{int(time.time())}_{os.getpid()}"
    admin_dsn = _admin_url()
    db_url = TEST_DB_URL.replace(
        TEST_DB_URL.rsplit("/", 1)[-1].split("?")[0], db_name
    )
    bare = _make_repo(tmp_path)
    used_ports: set[int] = set()
    port1, port2 = _free_port(used_ports), _free_port(used_ports)
    proc1 = proc2 = None
    try:
        async def create_db() -> None:
            conn = await asyncpg.connect(admin_dsn)
            try:
                await conn.execute(f'CREATE DATABASE "{db_name}"')
            finally:
                await conn.close()

        asyncio.run(create_db())
        proc1 = _start(port1, db_url, tmp_path / "api1.log")
        proc2 = _start(port2, db_url, tmp_path / "api2.log")
        api1, api2 = f"http://127.0.0.1:{port1}", f"http://127.0.0.1:{port2}"
        admin = {"X-Admin-Key": ADMIN_KEY}
        with httpx.Client(timeout=30.0) as client:
            workspace = client.post(
                f"{api1}/v1/admin/workspaces",
                headers=admin,
                json={"name": "horizontal", "repo_path": str(bare), "main_branch": "main"},
            )
            workspace.raise_for_status()
            workspace_id = workspace.json()["workspace_id"]
            tokens = [
                client.post(
                    f"{api1}/v1/admin/tokens",
                    headers=admin,
                    json={"label": f"h-{i}"},
                ).json()["token"]
                for i in range(2)
            ]
            task_ids = []
            for index in range(10):
                response = client.post(
                    f"{api1}/v1/admin/tasks",
                    headers=admin,
                    json={
                        "workspace_id": workspace_id,
                        "title": f"Implement f{index}",
                        "spec_prompt": "Implement the function deterministically.",
                        "type": "IMPLEMENT_BODY",
                        "target_symbols": [f"src/f{index:03d}.py::f"],
                        "write_set_symbols": [f"src/f{index:03d}.py::f"],
                    },
                )
                response.raise_for_status()
                task_ids.append(response.json()["task_id"])

        async def pull(api: str, token: str) -> dict:
            deadline = time.monotonic() + 20.0
            async with httpx.AsyncClient(timeout=30.0) as client:
                while True:
                    response = await client.post(
                        f"{api}/v1/syscalls/pull_task_bundle",
                        headers={"Authorization": f"Bearer {token}"},
                        json={
                            "worker_hub_id": "horizontal",
                            "supported_task_types": ["IMPLEMENT_BODY"],
                            "max_context_tokens": 6000,
                        },
                    )
                    assert response.status_code in (200, 429), response.text
                    if response.status_code == 200:
                        return response.json()
                    # 429 is admission backpressure (reservation slots busy);
                    # the real Runner retries after Retry-After.
                    if time.monotonic() >= deadline:
                        return {}
                    retry_after = float(response.headers.get("Retry-After", "1"))
                    await asyncio.sleep(min(1.0, max(0.05, retry_after)))

        async def compete() -> list[dict]:
            calls = []
            for index in range(10):
                calls.append(pull(api1, tokens[0]))
                calls.append(pull(api2, tokens[1]))
            return await asyncio.gather(*calls)

        results = asyncio.run(compete())
        leased = [row["task_id"] for row in results if row.get("leased")]
        assert len(leased) == 10, results
        assert len(set(leased)) == 10, leased

        async def authority_count() -> int:
            conn = await asyncpg.connect(_db_dsn(db_name))
            try:
                return int(
                    await conn.fetchval(
                        "SELECT count(*) FROM scheduler_authorities WHERE workspace_id = $1",
                        workspace_id,
                    )
                )
            finally:
                await conn.close()

        assert asyncio.run(authority_count()) == 1

        # Directly append replayable events; SSE must catch up from its cursor.
        async def seed_events() -> tuple[int, int]:
            conn = await asyncpg.connect(_db_dsn(db_name))
            try:
                rows = []
                for index in range(5):
                    rows.append(
                        await conn.fetchval(
                            "INSERT INTO kernel_events (workspace_id, event_type, payload) "
                            "VALUES ($1, 'REPLAY_TEST', $2) RETURNING event_seq",
                            workspace_id,
                            json.dumps({"index": index}),
                        )
                    )
                return int(rows[0]), int(rows[-1])
            finally:
                await conn.close()

        first_seq, last_seq = asyncio.run(seed_events())

        async def replay() -> list[int]:
            seen: list[int] = []
            async with httpx.AsyncClient(timeout=30.0) as client:
                async with client.stream(
                    "GET",
                    f"{api1}/v1/events/stream",
                    params={"since": first_seq - 1},
                    headers={"Authorization": f"Bearer {tokens[0]}"},
                ) as response:
                    assert response.status_code == 200
                    async for line in response.aiter_lines():
                        if line.startswith("id:"):
                            seen.append(int(line.split(":", 1)[1].strip()))
                            if seen[-1] >= last_seq:
                                break
            return seen

        assert asyncio.run(replay()) == list(range(first_seq, last_seq + 1))

        # 1000 long-lived SSE connections.
        async def open_many() -> int:
            limits = httpx.Limits(
                max_connections=1100, max_keepalive_connections=0
            )
            opened = 0
            lock = asyncio.Lock()
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=30.0),
                limits=limits,
            ) as client:

                async def one() -> None:
                    nonlocal opened
                    async with client.stream(
                        "GET",
                        f"{api1}/v1/events/stream",
                        params={"since": last_seq},
                        headers={"Authorization": f"Bearer {tokens[0]}"},
                    ) as response:
                        if response.status_code == 200:
                            async with lock:
                                opened += 1
                            await asyncio.sleep(1.0)

                tasks = [asyncio.create_task(one()) for _ in range(1000)]
                await asyncio.gather(*tasks, return_exceptions=True)
            return opened

        assert asyncio.run(open_many()) == 1000
    finally:
        if proc1 is not None:
            _stop(proc1)
        if proc2 is not None:
            _stop(proc2)

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
