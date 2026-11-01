"""End-to-end smoke test against a live PostgreSQL and a real bare Git repo.

Usage:
    python scripts/smoke_e2e.py

Environment defaults target the docker-compose style test instance:
    TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_test
The script resets the public schema, boots the app, creates a workspace, seeds
one IMPLEMENT_BODY task with a frozen stub + frozen test, leases it through the
syscall surface, submits a real patch, and asserts the merge + downstream
unlock.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_test",
)
os.environ["DATABASE_URL"] = DB_URL
os.environ.setdefault("VN_AUTH_MODE", "prod")
os.environ.setdefault("VN_ADMIN_API_KEY", "smoke-admin-key")
os.environ.setdefault("VN_SKIP_TYPECHECK", "true")
os.environ.setdefault("VN_SKIP_LINT", "true")
os.environ.setdefault("VN_REAPER_INTERVAL_SECONDS", "3600")
os.environ.setdefault("VN_TEST_CMD", "pytest -q --tb=short")
os.environ.setdefault("VN_COLLECT_CMD", "pytest --collect-only -q")
os.environ.setdefault("VN_SUBMISSION_WORKERS", "1")
os.environ.setdefault("VN_SUBMISSION_POLL_SECONDS", "0.05")

import asyncpg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{cmd} failed ({result.returncode})\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result.stdout


def _make_repo(base: Path) -> Path:
    work = base / "work"
    work.mkdir(parents=True)
    _run(["git", "init", "-b", "main"], work)
    _run(["git", "config", "user.name", "Smoke"], work)
    _run(["git", "config", "user.email", "smoke@example.com"], work)

    (work / "src").mkdir()
    (work / "src" / "__init__.py").write_text("", encoding="utf-8")
    (work / "src" / "calc.py").write_text(
        '''"""Calculator module."""\n\n\ndef add(a: int, b: int) -> int:\n    """Add two integers."""\n    ...\n''',
        encoding="utf-8",
    )
    (work / "tests").mkdir()
    (work / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    _run(["git", "add", "-A"], work)
    _run(["git", "commit", "-m", "seed"], work)

    bare = base / "bare.git"
    _run(["git", "clone", "--bare", str(work), str(bare)], base)
    return bare


def _patch_for_add(base: Path, bare: Path, head: str) -> str:
    wt = base / "patch-wt"
    _run(["git", "worktree", "add", "--detach", str(wt), head], bare)
    try:
        path = wt / "src" / "calc.py"
        content = path.read_text(encoding="utf-8")
        path.write_text(content.replace("    ...", "    return a + b"), encoding="utf-8")
        return _run(["git", "diff", "HEAD"], wt)
    finally:
        _run(["git", "worktree", "remove", "--force", str(wt)], bare)


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
    dsn = DB_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
    import asyncio

    async def reset() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        finally:
            await conn.close()

    asyncio.run(reset())

    base = Path(tempfile.mkdtemp(prefix="veynod-smoke-"))
    try:
        bare = _make_repo(base)
        headers = {"X-Admin-Key": "smoke-admin-key"}
        worker_log = (base / "worker.log").open("w", encoding="utf-8")
        worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.worker",
                "--workers",
                "1",
                "--database-url",
                DB_URL,
                "--metrics-port",
                "8031",
                "--sandbox",
                "local",
            ],
            cwd=str(ROOT),
            env={
                **os.environ,
                "DATABASE_URL": DB_URL,
                "VN_SKIP_TYPECHECK": "true",
                "VN_SKIP_LINT": "true",
                "VN_TEST_CMD": "pytest -q --tb=short",
                "VN_WORKER_POLL_SECONDS": "0.05",
                "PYTHONUNBUFFERED": "1",
            },
            stdout=worker_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        with TestClient(app) as client:
            r = client.post(
                "/v1/admin/workspaces",
                headers=headers,
                json={"name": "smoke", "repo_path": str(bare), "main_branch": "main"},
            )
            assert r.status_code == 201, r.text
            workspace_id = r.json()["workspace_id"]
            head = r.json()["head_commit_sha"]

            r = client.post("/v1/admin/tokens", headers=headers, json={"label": "smoke"})
            assert r.status_code == 201, r.text
            token = r.json()["token"]
            runner_headers = {"Authorization": f"Bearer {token}"}

            # Freeze the stub contract and its test.
            for symbol, kind in [
                ("src/calc.py::add", "stub"),
                ("tests/test_calc.py::test_add", "test"),
            ]:
                r = client.post(
                    "/v1/admin/artifacts",
                    headers=headers,
                    json={
                        "workspace_id": workspace_id,
                        "symbol_path": symbol,
                        "kind": kind,
                        "status": "FROZEN",
                    },
                )
                assert r.status_code == 201, r.text

            r = client.post(
                "/v1/admin/tasks",
                headers=headers,
                json={
                    "workspace_id": workspace_id,
                    "title": "Implement add",
                    "spec_prompt": "Implement add(a,b) so the frozen unit test passes.",
                    "type": "IMPLEMENT_BODY",
                    "target_symbols": ["src/calc.py::add"],
                    "test_symbols": ["tests/test_calc.py::test_add"],
                    "write_set_symbols": ["src/calc.py::add"],
                },
            )
            assert r.status_code == 201, r.text
            task_id = r.json()["task_id"]
            assert r.json()["status"] == "READY", r.text

            downstream = client.post(
                "/v1/admin/tasks",
                headers=headers,
                json={
                    "workspace_id": workspace_id,
                    "title": "Downstream implement sub",
                    "spec_prompt": "Implement sub(a,b) after add is merged.",
                    "type": "IMPLEMENT_BODY",
                    "target_symbols": ["src/calc.py::sub"],
                    "write_set_symbols": ["src/calc.py::sub"],
                    "depends_on_task_ids": [task_id],
                },
            )
            assert downstream.status_code == 201, downstream.text
            downstream_id = downstream.json()["task_id"]
            assert downstream.json()["status"] == "BLOCKED", downstream.text

            r = client.post(
                "/v1/syscalls/pull_task_bundle",
                headers=runner_headers,
                json={
                    "worker_hub_id": "smoke",
                    "supported_task_types": ["IMPLEMENT_BODY"],
                    "max_context_tokens": 6000,
                },
            )
            assert r.status_code == 200, r.text
            bundle = r.json()
            assert bundle["leased"] is True, bundle
            assert str(bundle["task_id"]) == task_id, bundle
            assert bundle["write_set"] == ["src/calc.py::add"], bundle
            assert bundle["mutable_stubs"], bundle
            assert bundle["frozen_tests"], bundle
            assert bundle["base_commit_sha"] == head, bundle
            print("bundle tokens:", bundle["estimated_tokens"])

            patch = _patch_for_add(base, bare, bundle["base_commit_sha"])
            idempotency_key = hashlib.sha256(
                f"{task_id}:{bundle['lease_token']}:{patch}".encode("utf-8")
            ).hexdigest()
            r = client.post(
                "/v2/syscalls/submit_patch",
                headers={**runner_headers, "Idempotency-Key": idempotency_key},
                json={
                    "task_id": task_id,
                    "lease_token": bundle["lease_token"],
                    "git_diff": patch,
                    "touched_symbols": ["src/calc.py::add"],
                    "local_test_passed": True,
                },
            )
            assert r.status_code == 202, r.text
            accepted = r.json()
            submit = None
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                status = client.get(
                    accepted["status_url"], headers=runner_headers
                )
                assert status.status_code == 200, status.text
                submit = status.json()
                if submit["final"]:
                    break
                time.sleep(0.05)
            assert submit is not None and submit["final"], submit
            print("submit:", submit["status"], submit.get("result_commit_sha"))
            for gate in submit["gate_results"]:
                print(" -", gate["stage"], gate["passed"], gate["exit_code"], gate["diagnostics"][:600])
            assert submit["status"] == "COMMITTED", submit
            assert str(downstream_id) in [
                str(value) for value in submit["unlocked_downstream_task_ids"]
            ], submit

            r = client.get(f"/v1/admin/tasks/{downstream_id}", headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "READY", r.text

            merged = _run(["git", "show", "main:src/calc.py"], bare)
            assert "return a + b" in merged, merged

            r = client.post(
                "/mcp",
                headers=runner_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {},
                },
            )
            assert r.status_code == 200, r.text
            names = [tool["name"] for tool in r.json()["result"]["tools"]]
            assert len(names) == 6, names
            print("mcp tools:", names)

            r = client.post(
                "/v1/syscalls/query_symbol_signature",
                headers=runner_headers,
                json={"task_id": task_id, "symbol_queries": ["src/calc.py"]},
            )
            assert r.status_code == 200, r.text
            assert r.json()["results"], r.text
            print("signature lookup results:", len(r.json()["results"]))

            r = client.get("/v1/dag")
            assert r.status_code == 200, r.text
            print("dag stats:", r.json()["stats"])

        print("SMOKE OK")
    finally:
        if "worker" in locals() and worker.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(worker.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                )
            else:
                worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
