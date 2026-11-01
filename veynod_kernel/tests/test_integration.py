"""Integration tests: real PostgreSQL 16 + real Git worktrees + pytest gate.

They are skipped unless TEST_DATABASE_URL points at a reachable database
(default: the docker container used by scripts/smoke_e2e.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from uuid import UUID

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import configure_database
from app.engine import rust_bridge
from app.engine.scheduler_core import scheduler_core_manager
from app.main import app

ROOT = Path(__file__).resolve().parents[1]

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_test",
)
ADMIN_KEY = "integration-admin-key-0123456789abcdef"

pytestmark = pytest.mark.integration


def _asyncpg_dsn() -> str:
    return TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


def _database_available() -> bool:
    async def probe() -> bool:
        try:
            conn = await asyncpg.connect(_asyncpg_dsn())
            await conn.close()
            return True
        except Exception:
            return False

    return asyncio.run(probe())


def _assert_test_database() -> None:
    name = _asyncpg_dsn().rsplit("/", 1)[-1].split("?")[0]
    if "test" not in name.lower() and os.environ.get(
        "VEYNOD_ALLOW_DESTRUCTIVE_TESTS"
    ) != "1":
        pytest.exit(
            f"Refusing to DROP SCHEMA on non-test database {name!r}. "
            "Point TEST_DATABASE_URL at a disposable test database or set "
            "VEYNOD_ALLOW_DESTRUCTIVE_TESTS=1.",
            returncode=2,
        )


def _reset_schema() -> None:
    async def reset() -> None:
        conn = await asyncpg.connect(_asyncpg_dsn())
        try:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        finally:
            await conn.close()

    asyncio.run(reset())


def _run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{cmd} failed ({result.returncode})\n{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def _make_repo(root: Path, files: dict[str, str]) -> tuple[Path, str]:
    work = root / "work"
    work.mkdir(parents=True)
    _run(["git", "init", "-b", "main"], work)
    _run(["git", "config", "user.name", "Integration"], work)
    _run(["git", "config", "user.email", "integration@example.com"], work)
    for relative, content in files.items():
        destination = work / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    _run(["git", "add", "-A"], work)
    _run(["git", "commit", "-m", "seed"], work)
    head = _run(["git", "rev-parse", "HEAD"], work).strip()
    bare = root / "bare.git"
    _run(["git", "clone", "--bare", str(work), str(bare)], root)
    return bare, head


def _patch(
    base: Path,
    bare: Path,
    head: str,
    file_path: str,
    old: str,
    new: str,
    *,
    replacements: list[tuple[str, str]] | None = None,
) -> str:
    worktree = base / f"patch-{abs(hash((file_path, old, new, str(replacements))))}"
    _run(["git", "worktree", "add", "--detach", str(worktree), head], bare)
    try:
        target = worktree / file_path
        content = target.read_text(encoding="utf-8")
        pairs = replacements or [(old, new)]
        for source, destination in pairs:
            assert source in content, content
            content = content.replace(source, destination, 1)
        target.write_text(content, encoding="utf-8")
        return _run(["git", "diff", "HEAD"], worktree)
    finally:
        _run(["git", "worktree", "remove", "--force", str(worktree)], bare)


@dataclass
class Harness:
    client: TestClient
    workspace_id: str
    token: str
    bare: Path
    head: str
    base: Path

    def admin(self, method: str, path: str, body: dict | None = None) -> dict:
        response = self.client.request(
            method,
            path,
            headers={"X-Admin-Key": ADMIN_KEY},
            json=body,
        )
        assert response.status_code < 300, response.text
        return response.json()

    def issue_token(self, label: str = "runner") -> str:
        return self.admin("POST", "/v1/admin/tokens", {"label": label})["token"]

    def artifact(
        self, symbol: str, kind: str, status: str = "FROZEN"
    ) -> dict:
        return self.admin(
            "POST",
            "/v1/admin/artifacts",
            {
                "workspace_id": self.workspace_id,
                "symbol_path": symbol,
                "kind": kind,
                "status": status,
            },
        )

    def create_task(self, **overrides) -> dict:
        body = {
            "workspace_id": self.workspace_id,
            "title": "Task",
            "spec_prompt": "Implement the declared symbol correctly and deterministically.",
            "type": "IMPLEMENT_BODY",
            **overrides,
        }
        return self.admin("POST", "/v1/admin/tasks", body)

    def syscall(self, path: str, body: dict, token: str | None = None) -> dict:
        response = self.client.post(
            path,
            headers={"Authorization": f"Bearer {token or self.token}"},
            json=body,
        )
        assert response.status_code < 300, response.text
        return response.json()

    def pull(self, token: str | None = None, types: list[str] | None = None) -> dict:
        return self.syscall(
            "/v1/syscalls/pull_task_bundle",
            {
                "worker_hub_id": "integration",
                "supported_task_types": types or ["IMPLEMENT_BODY", "WRITE_TEST"],
                "max_context_tokens": 6000,
            },
            token,
        )

    def submit(
        self,
        task_id: str,
        lease_token: str,
        diff: str,
        touched: list[str],
        token: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Submit through v2 and wait for the durable final submission state."""
        auth = token or self.token
        key = idempotency_key or hashlib.sha256(
            f"{task_id}:{lease_token}:{diff}".encode("utf-8")
        ).hexdigest()
        response = self.client.post(
            "/v2/syscalls/submit_patch",
            headers={
                "Authorization": f"Bearer {auth}",
                "Idempotency-Key": key,
            },
            json={
                "task_id": task_id,
                "lease_token": lease_token,
                "git_diff": diff,
                "touched_symbols": touched,
                "local_test_passed": True,
            },
        )
        assert response.status_code == 202, response.text
        accepted = response.json()
        deadline = time.monotonic() + 180
        payload = None
        while time.monotonic() < deadline:
            status_response = self.client.get(
                accepted["status_url"],
                headers={"Authorization": f"Bearer {auth}"},
            )
            assert status_response.status_code == 200, status_response.text
            payload = status_response.json()
            if payload["final"]:
                break
            time.sleep(0.05)
        assert payload is not None and payload["final"], payload
        return {
            "submission_id": accepted["submission_id"],
            "submission_status": payload["status"],
            "new_status": payload.get("task_status")
            or ("COMMITTED" if payload["status"] == "COMMITTED" else "FAILED"),
            "committed_revision_hash": payload.get("result_commit_sha"),
            "unlocked_downstream_task_ids": payload.get(
                "unlocked_downstream_task_ids", []
            ),
            "gate_results": payload.get("gate_results", []),
            "merge_mode": payload.get("merge_mode"),
            "error_code": payload.get("error_code"),
            "error_detail": payload.get("error_detail"),
        }

    def task(self, task_id: str) -> dict:
        return self.admin("GET", f"/v1/admin/tasks/{task_id}")

    def workspace_tasks(self, status: str | None = None) -> list[dict]:
        query = f"?workspace_id={self.workspace_id}"
        if status:
            query += f"&status={status}"
        return self.admin("GET", f"/v1/admin/tasks{query}")


@contextmanager
def gate_worker(db_url: str, workers: int = 2, extra_env: dict[str, str] | None = None):
    """Run the real independent gate worker process (no API process gate)."""
    worker_env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "VN_AUTH_MODE": "prod",
        "VN_SKIP_TYPECHECK": "true",
        "VN_SKIP_LINT": "true",
        "VN_TEST_CMD": "pytest -q --tb=short",
        "VN_COLLECT_CMD": "pytest --collect-only -q",
        "VN_GATE_TIMEOUT_SECONDS": "300",
        "VN_WORK_LEASE_SECONDS": "300",
        "VN_WORK_HEARTBEAT_SECONDS": "10",
        "VN_WORKER_POLL_SECONDS": "0.05",
        "VN_GATE_SANDBOX": "local",
        "VN_ALLOW_LOCAL_GATE_IN_PROD": "true",
        "PYTHONUNBUFFERED": "1",
    }
    if extra_env:
        worker_env.update(extra_env)
    log_dir = Path(tempfile.mkdtemp(prefix="veynod-worker-log-"))
    log_path = log_dir / "worker.log"
    handle = log_path.open("w", encoding="utf-8")
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "app.worker",
            "--workers",
            str(workers),
            "--database-url",
            db_url,
            "--work-lease-seconds",
            "300",
            "--poll-seconds",
            "0.05",
            "--sandbox",
            "local",
            "--metrics-port",
            "0",
        ],
        cwd=str(ROOT),
        env=worker_env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        **kwargs,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    "gate worker exited during startup:\n"
                    + log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                )
            try:
                if "gate workers started" in log_path.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    break
            except OSError:
                pass
            time.sleep(0.05)
        yield proc
    finally:
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
        handle.flush()
        handle.close()
        shutil.rmtree(log_dir, ignore_errors=True)

@contextmanager
def kernel(
    tmp_path: Path,
    files: dict[str, str],
    worker_env: dict[str, str] | None = None,
) -> Iterator[Harness]:
    if not _database_available():
        pytest.skip(
            f"PostgreSQL is not reachable at {TEST_DB_URL}; "
            "start the docker test container or set TEST_DATABASE_URL."
        )

    _assert_test_database()
    _reset_schema()
    settings.database_url = TEST_DB_URL
    settings.auth_mode = "prod"
    settings.admin_api_key = ADMIN_KEY
    settings.skip_typecheck = True
    settings.skip_lint = True
    settings.test_cmd = "pytest -q --tb=short"
    settings.collect_cmd = "pytest --collect-only -q"
    settings.reaper_interval_seconds = 3600
    settings.max_merge_attempts = 3
    settings.gate_timeout_seconds = 300
    settings.submission_worker_concurrency = 2
    settings.submission_poll_seconds = 0.05
    settings.legacy_submit_mode = "reject"
    asyncio.run(configure_database(TEST_DB_URL))

    base = Path(tempfile.mkdtemp(prefix="veynod-it-", dir=str(tmp_path)))
    bare, head = _make_repo(base, files)
    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/workspaces",
            headers={"X-Admin-Key": ADMIN_KEY},
            json={"name": "it", "repo_path": str(bare), "main_branch": "main"},
        )
        assert response.status_code == 201, response.text
        workspace_id = response.json()["workspace_id"]
        token_response = client.post(
            "/v1/admin/tokens",
            headers={"X-Admin-Key": ADMIN_KEY},
            json={"label": "runner-1"},
        )
        assert token_response.status_code == 201, token_response.text
        token = token_response.json()["token"]
        with gate_worker(TEST_DB_URL, workers=2, extra_env=worker_env):
            yield Harness(
                client=client,
                workspace_id=workspace_id,
                token=token,
                bare=bare,
                head=head,
                base=base,
            )
    shutil.rmtree(base, ignore_errors=True)


CALC_FILES = {
    "src/__init__.py": "",
    "src/calc.py": (
        '"""Calculator module."""\n\n\n'
        'def add(a: int, b: int) -> int:\n'
        '    """Add two integers."""\n'
        "    ...\n"
    ),
    "tests/__init__.py": "",
    "tests/test_calc.py": (
        "from src.calc import add\n\n\n"
        "def test_add() -> None:\n"
        "    assert add(2, 3) == 5\n"
    ),
}


def _freeze_calc(harness: Harness) -> None:
    harness.artifact("src/calc.py::add", "stub")
    harness.artifact("tests/test_calc.py::test_add", "test")


def _calc_task(harness: Harness, **overrides) -> dict:
    return harness.create_task(
        title="Implement add",
        spec_prompt="Implement add(a,b) so the frozen test test_add passes.",
        target_symbols=["src/calc.py::add"],
        test_symbols=["tests/test_calc.py::test_add"],
        write_set_symbols=["src/calc.py::add"],
        **overrides,
    )


# ---------------------------------------------------------------------------
# 1. Full happy path
# ---------------------------------------------------------------------------
def test_full_implementation_flow(tmp_path: Path) -> None:
    with kernel(tmp_path, CALC_FILES) as harness:
        _freeze_calc(harness)
        task = _calc_task(harness)
        assert task["status"] == "READY"
        downstream = harness.create_task(
            title="Implement sub",
            spec_prompt="Implement sub after add is merged.",
            target_symbols=["src/calc.py::sub"],
            write_set_symbols=["src/calc.py::sub"],
            depends_on_task_ids=[task["task_id"]],
        )
        assert downstream["status"] == "BLOCKED"

        bundle = harness.pull()
        assert bundle["leased"] is True
        assert bundle["task_id"] == task["task_id"]
        assert bundle["write_set"] == ["src/calc.py::add"]
        assert bundle["base_commit_sha"] == harness.head
        assert bundle["mutable_stubs"] and bundle["frozen_tests"]
        assert bundle["estimated_tokens"] <= 6000
        if rust_bridge.available():
            core_stats = scheduler_core_manager.stats(UUID(harness.workspace_id))
            assert core_stats["active"] >= 1, core_stats

        diff = _patch(
            harness.base,
            harness.bare,
            harness.head,
            "src/calc.py",
            "    ...",
            "    return a + b",
        )
        result = harness.submit(
            task["task_id"], bundle["lease_token"], diff, ["src/calc.py::add"]
        )
        assert result["new_status"] == "COMMITTED", result
        assert result["committed_revision_hash"]
        assert downstream["task_id"] in result["unlocked_downstream_task_ids"]
        assert all(gate["passed"] for gate in result["gate_results"])

        evidence = harness.admin(
            "GET", f"/v1/admin/tasks/{task['task_id']}/evidence"
        )
        assert evidence["evidence_complete"] is True, evidence
        assert evidence["merge_intent"]["status"] == "MERGED", evidence
        assert len(evidence["gate_results"]) == 5, evidence
        snapshot = harness.admin(
            "GET", f"/v1/admin/tasks/{task['task_id']}/snapshot"
        )
        assert snapshot["base_sha"] == harness.head, snapshot
        assert snapshot["manifest"]["artifacts"], snapshot
        delivery = harness.admin(
            "GET", f"/v1/admin/tasks/{task['task_id']}/delivery"
        )
        assert delivery["commit_sha"] == result["committed_revision_hash"]
        assert delivery["merge_mode"]
        assert delivery["evidence_hash"]
        event_page = harness.client.get(
            f"/v1/dag/events?workspace_id={harness.workspace_id}&limit=1000",
            headers={"X-Admin-Key": ADMIN_KEY},
        ).json()
        assert event_page["events"], event_page
        assert all(event["event_id"] for event in event_page["events"])
        assert all(event["producer"] == "kernel" for event in event_page["events"])
        if rust_bridge.available():
            core_stats = scheduler_core_manager.stats(UUID(harness.workspace_id))
            assert core_stats["active"] == 0, core_stats
            assert core_stats["ready"] >= 1, core_stats

        assert harness.task(downstream["task_id"])["status"] == "READY"
        merged = _run(["git", "show", "main:src/calc.py"], harness.bare)
        assert "return a + b" in merged

        plan = harness.client.get(
            f"/v1/dag/plan?workspace_id={harness.workspace_id}",
            headers={"X-Admin-Key": ADMIN_KEY},
        ).json()
        assert plan["engine"] in {"rust", "python"}
        assert downstream["task_id"] in plan["claimable"], plan

        health = harness.client.get("/healthz").json()
        assert health["core"]["available"] is rust_bridge.available()

        # Frozen contract hash was conserved and the target became COMMITTED.
        artifacts = harness.admin(
            "GET", f"/v1/admin/artifacts?workspace_id={harness.workspace_id}"
        )
        add_artifact = next(
            artifact
            for artifact in artifacts
            if artifact["symbol_path"] == "src/calc.py::add"
        )
        assert add_artifact["status"] == "COMMITTED"
        assert add_artifact["kind"] == "implementation"

        # MCP bridge is mounted and exposes exactly the 6 primitives.
        mcp = harness.client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {harness.token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert mcp.status_code == 200
        assert len(mcp.json()["result"]["tools"]) == 6


# ---------------------------------------------------------------------------
# 2. Cancellation
# ---------------------------------------------------------------------------
def test_cancel_task_cancels_downstream_and_is_idempotent(tmp_path: Path) -> None:
    with kernel(tmp_path, CALC_FILES) as harness:
        _freeze_calc(harness)
        task = _calc_task(harness)
        downstream = harness.create_task(
            title="Implement sub",
            spec_prompt="Implement sub after add is cancelled.",
            target_symbols=["src/calc.py::sub"],
            write_set_symbols=["src/calc.py::sub"],
            depends_on_task_ids=[task["task_id"]],
        )
        assert downstream["status"] == "BLOCKED"

        cancelled = harness.admin(
            "POST",
            f"/v1/admin/tasks/{task['task_id']}/cancel",
            {"reason": "duplicate of another task"},
        )
        assert cancelled["status"] == "CANCELLED", cancelled
        assert harness.task(task["task_id"])["status"] == "CANCELLED"
        assert harness.task(downstream["task_id"])["status"] == "CANCELLED"

        again = harness.admin(
            "POST",
            f"/v1/admin/tasks/{task['task_id']}/cancel",
            {"reason": "twice"},
        )
        assert again["status"] == "CANCELLED"
        assert again["idempotent"] is True


# ---------------------------------------------------------------------------
# 3. Workspace binding and debug-surface auth
# ---------------------------------------------------------------------------
def test_runner_token_workspace_binding_and_debug_auth(tmp_path: Path) -> None:
    with kernel(tmp_path, CALC_FILES) as harness:
        _freeze_calc(harness)
        task = _calc_task(harness)

        assert harness.client.get("/v1/dag").status_code == 401
        assert harness.client.get("/dashboard").status_code == 404

        other = harness.admin(
            "POST",
            "/v1/admin/workspaces",
            {
                "name": "other",
                "repo_path": str(harness.bare),
                "main_branch": "main",
            },
        )
        token_a = harness.admin(
            "POST",
            "/v1/admin/tokens",
            {"label": "workspace-a", "workspace_id": harness.workspace_id},
        )["token"]
        token_b = harness.admin(
            "POST",
            "/v1/admin/tokens",
            {"label": "workspace-b", "workspace_id": other["workspace_id"]},
        )["token"]
        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}

        ping = harness.client.get("/v1/runner/ping", headers=headers_a)
        assert ping.status_code == 200, ping.text
        assert ping.json()["workspace_id"] == harness.workspace_id

        own = harness.client.get(
            f"/v1/runner/tasks/{task['task_id']}", headers=headers_a
        )
        assert own.status_code == 200, own.text
        foreign = harness.client.get(
            f"/v1/runner/tasks/{task['task_id']}", headers=headers_b
        )
        assert foreign.status_code == 404, foreign.text

        cross_stream = harness.client.get(
            f"/v1/events/stream?workspace_id={other['workspace_id']}",
            headers=headers_a,
        )
        assert cross_stream.status_code == 403, cross_stream.text

        cross_pull = harness.client.post(
            "/v1/syscalls/pull_task_bundle",
            headers=headers_b,
            json={
                "worker_hub_id": "foreign",
                "supported_task_types": ["IMPLEMENT_BODY"],
                "max_context_tokens": 6000,
            },
        )
        assert cross_pull.status_code == 403, cross_pull.text


# ---------------------------------------------------------------------------
# 4. Token lifecycle
# ---------------------------------------------------------------------------
def test_token_revoke_rotate_and_lease_release(tmp_path: Path) -> None:
    with kernel(tmp_path, CALC_FILES) as harness:
        _freeze_calc(harness)
        task = _calc_task(harness)

        issued = harness.admin(
            "POST",
            "/v1/admin/tokens",
            {"label": "lifecycle", "workspace_id": harness.workspace_id},
        )
        token = issued["token"]
        token_id = issued["token_id"]

        listed = harness.admin("GET", "/v1/admin/tokens")
        assert any(
            item["token_id"] == token_id and item["active"] for item in listed
        ), listed

        bundle = harness.pull(token=token)
        assert bundle["leased"] is True
        assert bundle["task_id"] == task["task_id"]

        revoked = harness.admin(
            "POST", f"/v1/admin/tokens/{token_id}/revoke"
        )
        assert revoked["active"] is False, revoked
        assert str(task["task_id"]) in revoked["released_tasks"], revoked
        assert harness.task(task["task_id"])["status"] in {"READY", "FAILED"}
        audit = harness.client.get(
            f"/v1/dag/events?workspace_id={harness.workspace_id}",
            headers={"X-Admin-Key": ADMIN_KEY},
        ).json()
        assert "TOKEN_REVOKED" in {
            event["event_type"] for event in audit["events"]
        }, audit

        old_ping = harness.client.get(
            "/v1/runner/ping",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert old_ping.status_code == 401, old_ping.text

        issued2 = harness.admin(
            "POST",
            "/v1/admin/tokens",
            {"label": "rotate-me", "workspace_id": harness.workspace_id},
        )
        rotated = harness.admin(
            "POST", f"/v1/admin/tokens/{issued2['token_id']}/rotate"
        )
        assert rotated["token"] != issued2["token"], rotated
        old2 = harness.client.get(
            "/v1/runner/ping",
            headers={"Authorization": f"Bearer {issued2['token']}"},
        )
        assert old2.status_code == 401, old2.text
        new2 = harness.client.get(
            "/v1/runner/ping",
            headers={"Authorization": f"Bearer {rotated['token']}"},
        )
        assert new2.status_code == 200, new2.text


def test_require_bound_tokens_rejects_legacy_token(
    tmp_path: Path, monkeypatch
) -> None:
    with kernel(tmp_path, CALC_FILES) as harness:
        monkeypatch.setattr(settings, "default_workspace_id", None)
        monkeypatch.setattr(settings, "require_bound_tokens", True)

        legacy = harness.issue_token("legacy-unbound")
        legacy_ping = harness.client.get(
            "/v1/runner/ping",
            headers={"Authorization": f"Bearer {legacy}"},
        )
        assert legacy_ping.status_code == 401, legacy_ping.text

        bound = harness.admin(
            "POST",
            "/v1/admin/tokens",
            {"label": "bound", "workspace_id": harness.workspace_id},
        )["token"]
        bound_ping = harness.client.get(
            "/v1/runner/ping",
            headers={"Authorization": f"Bearer {bound}"},
        )
        assert bound_ping.status_code == 200, bound_ping.text


# ---------------------------------------------------------------------------
# 4. Disjoint write-set scheduler
# ---------------------------------------------------------------------------
SCHEDULER_FILES = {
    "src/__init__.py": "",
    "src/a.py": (
        "def f() -> int:\n    ...\n\n\ndef g() -> int:\n    ...\n"
    ),
    "src/b.py": "def h() -> int:\n    ...\n",
}


def test_scheduler_leases_only_orthogonal_write_sets(tmp_path: Path) -> None:
    with kernel(tmp_path, SCHEDULER_FILES) as harness:
        for symbol in ["src/a.py::f", "src/a.py::g", "src/b.py::h", "src/a.py"]:
            harness.artifact(symbol, "stub")

        first = harness.create_task(
            title="Implement f",
            spec_prompt="Implement f deterministically.",
            target_symbols=["src/a.py::f"],
            write_set_symbols=["src/a.py::f"],
        )
        second = harness.create_task(
            title="Implement g",
            spec_prompt="Implement g deterministically.",
            target_symbols=["src/a.py::g"],
            write_set_symbols=["src/a.py::g"],
        )
        conflicting = harness.create_task(
            title="Rewrite a.py",
            spec_prompt="Rewrite the whole module carefully.",
            target_symbols=["src/a.py"],
            write_set_symbols=["src/a.py"],
            priority=50,
        )
        independent = harness.create_task(
            title="Implement h",
            spec_prompt="Implement h deterministically.",
            target_symbols=["src/b.py::h"],
            write_set_symbols=["src/b.py::h"],
        )

        token2 = harness.issue_token("runner-2")
        assert harness.pull()["task_id"] == first["task_id"]
        assert harness.pull(token2)["task_id"] == second["task_id"]
        third = harness.pull()
        assert third["task_id"] == independent["task_id"], third
        assert harness.pull(token2)["leased"] is False
        assert harness.task(conflicting["task_id"])["status"] == "READY"


# ---------------------------------------------------------------------------
# 3. Frozen contract conservation
# ---------------------------------------------------------------------------
def test_signature_mutation_is_rejected(tmp_path: Path) -> None:
    with kernel(tmp_path, CALC_FILES) as harness:
        _freeze_calc(harness)
        task = _calc_task(harness)
        bundle = harness.pull()
        diff = _patch(
            harness.base,
            harness.bare,
            harness.head,
            "src/calc.py",
            "def add(a: int, b: int) -> int:",
            "def add(a: int, b: int, c: int = 0) -> int:",
            replacements=[
                (
                    "def add(a: int, b: int) -> int:",
                    "def add(a: int, b: int, c: int = 0) -> int:",
                ),
                ("    ...", "    return a + b + c"),
            ],
        )
        result = harness.submit(
            task["task_id"], bundle["lease_token"], diff, ["src/calc.py::add"]
        )
        assert result["new_status"] == "LEASED", result
        failed = [gate for gate in result["gate_results"] if not gate["passed"]]
        assert failed and failed[0]["stage"] in {
            "AST_SCOPE_DIFF",
            "CONTRACT_HASH_GUARD",
        }, result
        assert (
            "AST Scope Diff" in failed[0]["diagnostics"]
            or "Contract Conservation Violation" in failed[0]["diagnostics"]
        )
        assert harness.task(task["task_id"])["retry_count"] == 1
        head = _run(["git", "rev-parse", "main"], harness.bare).strip()
        assert head == harness.head


# ---------------------------------------------------------------------------
# 4. Cold-start stub scanner
# ---------------------------------------------------------------------------
SCAN_FILES = {
    "src/__init__.py": "",
    "src/lib.py": (
        '"""Library."""\n\n\n'
        "def alpha(x: int) -> int:\n"
        '    """Alpha."""\n'
        "    ...\n\n\n"
        "def beta(x: int) -> int:\n"
        '    """Beta."""\n'
        "    pass\n\n\n"
        "def gamma(x: int) -> int:\n"
        '    """Gamma."""\n'
        "    return x + 1\n"
    ),
}


def test_scan_stubs_creates_idempotent_write_test_and_implement_pairs(
    tmp_path: Path,
) -> None:
    with kernel(tmp_path, SCAN_FILES) as harness:
        dry = harness.admin(
            "POST",
            f"/v1/admin/workspaces/{harness.workspace_id}/scan-stubs",
            {"dry_run": True},
        )
        assert dry["stubs_found"] == 2, dry
        assert dry["would_create_implementations"] == 2

        created = harness.admin(
            "POST",
            f"/v1/admin/workspaces/{harness.workspace_id}/scan-stubs",
            {"dry_run": False},
        )
        assert created["created_write_tests"] == 2
        assert created["created_implementations"] == 2

        tasks = harness.workspace_tasks()
        by_type = {}
        for task in tasks:
            by_type.setdefault(task["type"], []).append(task)
        assert len(by_type["WRITE_TEST"]) == 2
        assert len(by_type["IMPLEMENT_BODY"]) == 2
        assert all(task["status"] == "READY" for task in by_type["WRITE_TEST"])
        assert all(task["status"] == "BLOCKED" for task in by_type["IMPLEMENT_BODY"])
        assert all(task["in_degree"] == 1 for task in by_type["IMPLEMENT_BODY"])

        again = harness.admin(
            "POST",
            f"/v1/admin/workspaces/{harness.workspace_id}/scan-stubs",
            {"dry_run": False},
        )
        assert again["created_implementations"] == 0
        assert again["skipped"] == 2


# ---------------------------------------------------------------------------
# 5. SPLIT
# ---------------------------------------------------------------------------
def test_request_task_split_expands_and_archives_parent(tmp_path: Path) -> None:
    with kernel(tmp_path, SCHEDULER_FILES) as harness:
        harness.artifact("src/a.py::f", "stub")
        harness.artifact("src/a.py::g", "stub")
        parent = harness.create_task(
            title="Implement f and g",
            spec_prompt="Implement both functions; this task is intentionally too large.",
            target_symbols=["src/a.py::f", "src/a.py::g"],
            write_set_symbols=["src/a.py::f", "src/a.py::g"],
        )
        bundle = harness.pull()
        assert bundle["task_id"] == parent["task_id"]

        response = harness.syscall(
            "/v1/syscalls/request_task_split",
            {
                "task_id": parent["task_id"],
                "lease_token": bundle["lease_token"],
                "split_reason": "Two independent functions exceed the 250 LOC rule.",
                "sub_tasks": [
                    {
                        "local_key": "f_impl",
                        "task_type": "IMPLEMENT_BODY",
                        "specification": "Implement f precisely.",
                        "write_set_symbols": ["src/a.py::f"],
                    },
                    {
                        "local_key": "g_impl",
                        "task_type": "IMPLEMENT_BODY",
                        "specification": "Implement g precisely.",
                        "write_set_symbols": ["src/a.py::g"],
                    },
                ],
            },
        )
        assert response["parent_new_status"] == "SPLIT_ARCHIVED"
        assert len(response["spawned_task_map"]) == 2
        assert harness.task(parent["task_id"])["status"] == "SPLIT_ARCHIVED"
        for child_id in response["spawned_task_map"].values():
            assert harness.task(child_id)["status"] == "READY"


# ---------------------------------------------------------------------------
# 6. INJECT
# ---------------------------------------------------------------------------
def test_inject_prerequisite_blocks_current_and_creates_ready_node(
    tmp_path: Path,
) -> None:
    with kernel(tmp_path, SCHEDULER_FILES) as harness:
        harness.artifact("src/b.py::h", "stub")
        task = harness.create_task(
            title="Implement h",
            spec_prompt="Implement h, which is blocked on a missing helper.",
            target_symbols=["src/b.py::h"],
            write_set_symbols=["src/b.py::h"],
        )
        bundle = harness.pull()
        response = harness.syscall(
            "/v1/syscalls/inject_dependency_task",
            {
                "task_id": task["task_id"],
                "lease_token": bundle["lease_token"],
                "prerequisite_symbol_path": "src/b.py::helper",
                "prerequisite_specification": "Provide the missing helper utility.",
                "prerequisite_write_set": ["src/b.py::helper"],
            },
        )
        assert response["current_task_new_status"] == "BLOCKED"
        current = harness.task(task["task_id"])
        injected = harness.task(response["injected_prerequisite_task_id"])
        assert current["status"] == "BLOCKED"
        assert current["in_degree"] == 1
        assert injected["status"] == "READY"
        assert injected["type"] == "DESIGN_CONTRACT"
        assert injected["write_locks"] == ["src/b.py::helper"]


# ---------------------------------------------------------------------------
# 7. ESCALATE
# ---------------------------------------------------------------------------
def test_escalate_contract_marks_stale_and_blocks_dependents(tmp_path: Path) -> None:
    with kernel(tmp_path, SCHEDULER_FILES) as harness:
        harness.artifact("src/a.py::f", "stub")
        harness.artifact("src/a.py::g", "stub")
        consumer = harness.create_task(
            title="Implement g using f",
            spec_prompt="Implement g using the frozen f contract.",
            read_symbols=["src/a.py::f"],
            target_symbols=["src/a.py::g"],
            write_set_symbols=["src/a.py::g"],
        )
        bundle = harness.pull()
        assert bundle["task_id"] == consumer["task_id"]
        assert bundle["read_only_signatures"], bundle

        defective_id = bundle["read_only_signatures"][0]["artifact_id"]
        response = harness.syscall(
            "/v1/syscalls/escalate_contract_defect",
            {
                "task_id": consumer["task_id"],
                "lease_token": bundle["lease_token"],
                "defective_artifact_id": defective_id,
                "defect_reason": (
                    "The frozen contract lacks a required precision field for rounding."
                ),
                "proposed_ast_signature": (
                    "def f(precision: int | None = None) -> int: ..."
                ),
                "breaking_symbol_paths": ["src/a.py::f"],
            },
        )
        assert response["suspended_task_id"] == consumer["task_id"]
        assert harness.task(consumer["task_id"])["status"] == "SUSPENDED_ESCALATED"
        revision = harness.task(response["contract_revision_task_id"])
        assert revision["status"] == "READY"
        assert revision["type"] == "REVISE_CONTRACT"
        assert revision["priority"] == 999
        assert response["stale_marked_artifact_ids"]

        artifacts = harness.admin(
            "GET", f"/v1/admin/artifacts?workspace_id={harness.workspace_id}"
        )
        f_artifact = next(
            artifact for artifact in artifacts if artifact["symbol_path"] == "src/a.py::f"
        )
        assert f_artifact["status"] == "STALE"



# ---------------------------------------------------------------------------
# 8. Durable asynchronous submissions (v2 protocol)
# ---------------------------------------------------------------------------
def _submission_world(tmp_path: Path, workers: int):
    """Prepare an isolated DB/repo; return (base, bare, head)."""
    _assert_test_database()
    _reset_schema()
    settings.database_url = TEST_DB_URL
    settings.auth_mode = "prod"
    settings.admin_api_key = ADMIN_KEY
    settings.skip_typecheck = True
    settings.skip_lint = True
    settings.test_cmd = "pytest -q --tb=short"
    settings.collect_cmd = "pytest --collect-only -q"
    settings.reaper_interval_seconds = 3600
    settings.max_merge_attempts = 3
    settings.gate_timeout_seconds = 300
    settings.submission_worker_concurrency = workers
    settings.submission_poll_seconds = 0.05
    settings.legacy_submit_mode = "reject"
    asyncio.run(configure_database(TEST_DB_URL))
    base = Path(tempfile.mkdtemp(prefix="veynod-submit-it-", dir=str(tmp_path)))
    bare, head = _make_repo(base, CALC_FILES)
    return base, bare, head


def _expire_task_lease(task_id: str) -> None:
    async def expire() -> None:
        conn = await asyncpg.connect(_asyncpg_dsn())
        try:
            await conn.execute(
                "UPDATE dag_tasks SET lease_expires_at = NOW() - INTERVAL '1 minute' "
                "WHERE task_id = $1",
                UUID(task_id),
            )
        finally:
            await conn.close()

    asyncio.run(expire())


def test_async_submission_survives_restart_and_is_idempotent(tmp_path: Path) -> None:
    base, bare, head = _submission_world(tmp_path, workers=0)
    key = "restart-lost-response-key"
    diff = ""
    diff_other = ""
    submission_id = ""
    token = ""
    task_id = ""
    lease_token = ""
    try:
        with TestClient(app) as client:
            response = client.post(
                "/v1/admin/workspaces",
                headers={"X-Admin-Key": ADMIN_KEY},
                json={"name": "submit-it", "repo_path": str(bare), "main_branch": "main"},
            )
            assert response.status_code == 201, response.text
            workspace_id = response.json()["workspace_id"]
            token = client.post(
                "/v1/admin/tokens",
                headers={"X-Admin-Key": ADMIN_KEY},
                json={"label": "submit-runner"},
            ).json()["token"]
            harness = Harness(
                client=client,
                workspace_id=workspace_id,
                token=token,
                bare=bare,
                head=head,
                base=base,
            )
            _freeze_calc(harness)
            task = _calc_task(harness)
            task_id = task["task_id"]
            bundle = harness.pull()
            lease_token = bundle["lease_token"]
            diff = _patch(
                base, bare, head, "src/calc.py", "    ...", "    return a + b"
            )
            diff_other = _patch(
                base,
                bare,
                head,
                "src/calc.py",
                "    ...",
                "    return a + b + 1",
            )
            headers = {
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": key,
            }
            body = {
                "task_id": task_id,
                "lease_token": lease_token,
                "git_diff": diff,
                "touched_symbols": ["src/calc.py::add"],
                "local_test_passed": True,
            }
            started = time.monotonic()
            accepted = client.post(
                "/v2/syscalls/submit_patch", headers=headers, json=body
            )
            accepted_elapsed = time.monotonic() - started
            assert accepted.status_code == 202, accepted.text
            submission_id = accepted.json()["submission_id"]
            assert accepted_elapsed < 10, accepted_elapsed

            # Workers are disabled, so the gate cannot have run in the API.
            assert harness.task(task_id)["status"] == "LEASED"
            queued = client.get(
                f"/v2/submissions/{submission_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert queued.status_code == 200, queued.text
            assert queued.json()["final"] is False
            assert queued.json()["status"] == "QUEUED"

            # Lost-response retry -> same visible result.
            duplicate = client.post(
                "/v2/syscalls/submit_patch", headers=headers, json=body
            )
            assert duplicate.status_code == 202, duplicate.text
            assert duplicate.json()["submission_id"] == submission_id
            assert duplicate.json()["deduplicated"] is True
            found = client.get(
                "/v2/submissions/find",
                params={"idempotency_key": key},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert found.status_code == 200, found.text
            assert found.json()["submission_id"] == submission_id

            # A different opaque key for the same task/lease/patch is still
            # the same submission (binding dedupe).
            other_key = client.post(
                "/v2/syscalls/submit_patch",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": "another-key-same-binding",
                },
                json=body,
            )
            assert other_key.status_code == 202, other_key.text
            assert other_key.json()["submission_id"] == submission_id
            assert other_key.json()["deduplicated"] is True

            # Same key with a different patch -> hard conflict.
            conflict = client.post(
                "/v2/syscalls/submit_patch",
                headers=headers,
                json={**body, "git_diff": diff_other},
            )
            assert conflict.status_code == 409, conflict.text
            assert conflict.json()["detail"]["error"] == "idempotency_key_conflict"

            # Start the independent gate worker.  This is a fresh process,
            # equivalent to the durable boundary after a kernel restart.
            with gate_worker(TEST_DB_URL, workers=1):
                deadline = time.monotonic() + 120
                final = None
                while time.monotonic() < deadline:
                    result = client.get(
                        f"/v2/submissions/{submission_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert result.status_code == 200, result.text
                    final = result.json()
                    if final["final"]:
                        break
                    time.sleep(0.05)
                assert final is not None and final["final"], final
                assert final["status"] == "COMMITTED", final
                assert final["task_status"] == "COMMITTED", final
                assert final["result_commit_sha"], final
                assert all(gate["passed"] for gate in final["gate_results"]), final

                replay = client.post(
                    "/v2/syscalls/submit_patch",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Idempotency-Key": key,
                    },
                    json={
                        "task_id": task_id,
                        "lease_token": lease_token,
                        "git_diff": diff,
                        "touched_symbols": ["src/calc.py::add"],
                        "local_test_passed": True,
                    },
                )
                assert replay.status_code == 202, replay.text
                assert replay.json()["submission_id"] == submission_id
                assert replay.json()["status"] == "COMMITTED"
                assert harness.task(task_id)["status"] == "COMMITTED"

        async def inspect() -> tuple[int, bool, int]:
            conn = await asyncpg.connect(_asyncpg_dsn())
            try:
                count = await conn.fetchval(
                    "SELECT count(*) FROM patch_submissions WHERE task_id = $1",
                    UUID(task_id),
                )
                merge_count = await conn.fetchval(
                    "SELECT count(*) FROM merge_intents WHERE submission_id = $1",
                    UUID(submission_id),
                )
                blocked = False
                try:
                    await conn.execute(
                        "UPDATE patch_submissions SET git_diff = 'tampered' "
                        "WHERE submission_id = $1",
                        UUID(submission_id),
                    )
                except Exception:
                    blocked = True
                return int(count), blocked, int(merge_count)
            finally:
                await conn.close()

        count, immutable, merge_count = asyncio.run(inspect())
        assert count == 1, count
        assert merge_count == 1, merge_count
        assert immutable is True
        assert _run(["git", "rev-list", "--count", "main"], bare).strip() == "2"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_async_submission_fails_durably_when_lease_expires_before_processing(
    tmp_path: Path,
) -> None:
    base, bare, head = _submission_world(tmp_path, workers=0)
    task_id = ""
    submission_id = ""
    token = ""
    try:
        with TestClient(app) as client:
            workspace = client.post(
                "/v1/admin/workspaces",
                headers={"X-Admin-Key": ADMIN_KEY},
                json={
                    "name": "lease-expiry",
                    "repo_path": str(bare),
                    "main_branch": "main",
                },
            )
            assert workspace.status_code == 201, workspace.text
            workspace_id = workspace.json()["workspace_id"]
            token = client.post(
                "/v1/admin/tokens",
                headers={"X-Admin-Key": ADMIN_KEY},
                json={"label": "lease-expiry-runner"},
            ).json()["token"]
            harness = Harness(
                client=client,
                workspace_id=workspace_id,
                token=token,
                bare=bare,
                head=head,
                base=base,
            )
            _freeze_calc(harness)
            task = _calc_task(harness)
            task_id = task["task_id"]
            bundle = harness.pull()
            diff = _patch(
                base, bare, head, "src/calc.py", "    ...", "    return a + b"
            )
            accepted = client.post(
                "/v2/syscalls/submit_patch",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": "lease-expiry-key",
                },
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

            _expire_task_lease(task_id)
            with gate_worker(TEST_DB_URL, workers=1):
                deadline = time.monotonic() + 60
                final = None
                while time.monotonic() < deadline:
                    result = client.get(
                        f"/v2/submissions/{submission_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert result.status_code == 200, result.text
                    final = result.json()
                    if final["final"]:
                        break
                    time.sleep(0.05)
                assert final is not None and final["final"], final
                assert final["status"] == "FAILED", final
                assert final["error_code"] == "lease_expired", final
                current = client.get(
                    f"/v1/runner/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {token}"},
                ).json()
                assert current["status"] == "READY", current
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_legacy_submit_endpoint_returns_migration_hint(tmp_path: Path) -> None:
    with kernel(tmp_path, CALC_FILES) as harness:
        _freeze_calc(harness)
        task = _calc_task(harness)
        bundle = harness.pull()
        diff = _patch(
            harness.base,
            harness.bare,
            harness.head,
            "src/calc.py",
            "    ...",
            "    return a + b",
        )
        response = harness.client.post(
            "/v1/syscalls/submit_patch",
            headers={"Authorization": f"Bearer {harness.token}"},
            json={
                "task_id": task["task_id"],
                "lease_token": bundle["lease_token"],
                "git_diff": diff,
                "touched_symbols": ["src/calc.py::add"],
                "local_test_passed": True,
            },
        )
        assert response.status_code == 410, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "legacy_submit_endpoint_retired"
        assert detail["migration"]["endpoint"] == "/v2/syscalls/submit_patch"
        assert harness.task(task["task_id"])["status"] == "LEASED"


def test_legacy_sync_escape_hatch_still_commits(tmp_path: Path) -> None:
    with kernel(tmp_path, CALC_FILES) as harness:
        settings.legacy_submit_mode = "sync"
        _freeze_calc(harness)
        task = _calc_task(harness)
        bundle = harness.pull()
        diff = _patch(
            harness.base,
            harness.bare,
            harness.head,
            "src/calc.py",
            "    ...",
            "    return a + b",
        )
        result = harness.syscall(
            "/v1/syscalls/submit_patch",
            {
                "task_id": task["task_id"],
                "lease_token": bundle["lease_token"],
                "git_diff": diff,
                "touched_symbols": ["src/calc.py::add"],
                "local_test_passed": True,
            },
        )
        assert result["new_status"] == "COMMITTED", result


# ---------------------------------------------------------------------------
# 9. Server-derived AST scope (do not trust Runner touched_symbols)
# ---------------------------------------------------------------------------
def test_ast_scope_rejects_changes_to_an_unleased_symbol(tmp_path: Path) -> None:
    with kernel(tmp_path, SCHEDULER_FILES) as harness:
        harness.artifact("src/a.py::f", "stub")
        task = harness.create_task(
            title="Implement f only",
            spec_prompt="Implement f; g is outside this task's write set.",
            target_symbols=["src/a.py::f"],
            write_set_symbols=["src/a.py::f"],
        )
        bundle = harness.pull()
        diff = _patch(
            harness.base,
            harness.bare,
            harness.head,
            "src/a.py",
            "def g() -> int:\n    ...",
            "def g() -> int:\n    return 1",
        )
        result = harness.submit(
            task["task_id"],
            bundle["lease_token"],
            diff,
            ["src/a.py::f"],  # Runner lies: the actual patch changes g.
        )
        assert result["new_status"] == "LEASED", result
        failed = [gate for gate in result["gate_results"] if not gate["passed"]]
        assert failed and failed[0]["stage"] == "AST_SCOPE_DIFF", result
        assert "src/a.py::g" in failed[0]["diagnostics"], result
        assert _run(["git", "rev-parse", "main"], harness.bare).strip() == harness.head


def test_ast_scope_requires_file_lock_for_module_level_change(tmp_path: Path) -> None:
    with kernel(tmp_path, SCHEDULER_FILES) as harness:
        harness.artifact("src/a.py::f", "stub")
        task = harness.create_task(
            title="Implement f at symbol scope",
            spec_prompt="Implement f; module-level imports are not allowed here.",
            target_symbols=["src/a.py::f"],
            write_set_symbols=["src/a.py::f"],
        )
        bundle = harness.pull()
        diff = _patch(
            harness.base,
            harness.bare,
            harness.head,
            "src/a.py",
            "def f() -> int:",
            "import os\n\n\ndef f() -> int:",
        )
        result = harness.submit(
            task["task_id"],
            bundle["lease_token"],
            diff,
            ["src/a.py::f"],
        )
        assert result["new_status"] == "LEASED", result
        failed = [gate for gate in result["gate_results"] if not gate["passed"]]
        assert failed and failed[0]["stage"] == "AST_SCOPE_DIFF", result
        assert "module-level" in failed[0]["diagnostics"].lower(), result
        assert _run(["git", "rev-parse", "main"], harness.bare).strip() == harness.head


# ---------------------------------------------------------------------------
# 10. Sandbox and timeout behavior
# ---------------------------------------------------------------------------
ENV_SANDBOX_FILES = {
    "src/__init__.py": "",
    "src/calc.py": "def add(a: int, b: int) -> int:\n    ...\n",
    "tests/__init__.py": "",
    "tests/test_env_sandbox.py": (
        "import os\n\n\n"
        "def test_sandbox_has_no_database_or_admin_credentials() -> None:\n"
        "    for key in (\n"
        "        'DATABASE_URL',\n"
        "        'VN_ADMIN_API_KEY',\n"
        "        'VN_BOOTSTRAP_RUNNER_TOKEN',\n"
        "        'TEST_DATABASE_URL',\n"
        "    ):\n"
        "        assert key not in os.environ, key\n"
        "    assert os.environ.get('VEYNOD_SANDBOX') == '1'\n"
    ),
}


def test_gate_sandbox_hides_database_and_admin_credentials(tmp_path: Path) -> None:
    with kernel(tmp_path, ENV_SANDBOX_FILES) as harness:
        harness.artifact("src/calc.py::add", "stub")
        harness.artifact("tests/test_env_sandbox.py::test_sandbox_has_no_database_or_admin_credentials", "test")
        task = harness.create_task(
            title="Implement add under sandbox",
            spec_prompt="Implement add; the frozen sandbox test must pass.",
            target_symbols=["src/calc.py::add"],
            test_symbols=[
                "tests/test_env_sandbox.py::test_sandbox_has_no_database_or_admin_credentials"
            ],
            write_set_symbols=["src/calc.py::add"],
        )
        bundle = harness.pull()
        diff = _patch(
            harness.base,
            harness.bare,
            harness.head,
            "src/calc.py",
            "    ...",
            "    return a + b",
        )
        result = harness.submit(
            task["task_id"], bundle["lease_token"], diff, ["src/calc.py::add"]
        )
        assert result["new_status"] == "COMMITTED", result
        stages = {gate["stage"] for gate in result["gate_results"]}
        assert {
            "WRITE_SET_BOUNDARY",
            "AST_SCOPE_DIFF",
            "CONTRACT_HASH_GUARD",
            "STATIC_TYPECHECK",
            "UNIT_TEST_SUITE",
        } <= stages, stages


SLOW_TEST_FILES = {
    "src/__init__.py": "",
    "src/calc.py": "def add(a: int, b: int) -> int:\n    ...\n",
    "tests/__init__.py": "",
    "tests/test_slow.py": (
        "import time\n\n\n"
        "def test_slow_gate() -> None:\n"
        "    time.sleep(5)\n"
        "    assert True\n"
    ),
}


def test_gate_timeout_produces_a_failed_unit_test_stage(tmp_path: Path) -> None:
    with kernel(
        tmp_path,
        SLOW_TEST_FILES,
        worker_env={"VN_GATE_TIMEOUT_SECONDS": "1"},
    ) as harness:
        harness.artifact("src/calc.py::add", "stub")
        harness.artifact("tests/test_slow.py::test_slow_gate", "test")
        task = harness.create_task(
            title="Implement add with slow test",
            spec_prompt="Implement add; the frozen test simulates a timeout.",
            target_symbols=["src/calc.py::add"],
            test_symbols=["tests/test_slow.py::test_slow_gate"],
            write_set_symbols=["src/calc.py::add"],
        )
        bundle = harness.pull()
        diff = _patch(
            harness.base,
            harness.bare,
            harness.head,
            "src/calc.py",
            "    ...",
            "    return a + b",
        )
        result = harness.submit(
            task["task_id"], bundle["lease_token"], diff, ["src/calc.py::add"]
        )
        assert result["new_status"] == "LEASED", result
        failed = [gate for gate in result["gate_results"] if not gate["passed"]]
        assert failed and failed[0]["stage"] == "UNIT_TEST_SUITE", result
        assert failed[0]["exit_code"] == 124, failed[0]


# ---------------------------------------------------------------------------
# 11. Merge coordinator: external commits, dependency revalidation, conflicts
# ---------------------------------------------------------------------------
def _external_commit(
    bare: Path,
    base: str,
    file_path: str,
    old: str,
    new: str,
) -> str:
    worktree = Path(tempfile.mkdtemp(prefix="veynod-external-", dir=str(bare.parent)))
    _run(["git", "worktree", "add", "--detach", str(worktree), base], bare)
    try:
        target = worktree / file_path
        content = target.read_text(encoding="utf-8")
        assert old in content, content
        target.write_text(content.replace(old, new, 1), encoding="utf-8")
        _run(["git", "add", "-A"], worktree)
        _run(
            [
                "git",
                "-c",
                "user.name=External",
                "-c",
                "user.email=external@example.com",
                "commit",
                "-m",
                "external main advance",
            ],
            worktree,
        )
        sha = _run(["git", "rev-parse", "HEAD"], worktree).strip()
        _run(["git", "update-ref", "refs/heads/main", sha, base], bare)
        return sha
    finally:
        _run(["git", "worktree", "remove", "--force", str(worktree)], bare)
        shutil.rmtree(worktree, ignore_errors=True)


def _wait_task_status(harness: Harness, task_id: str, wanted: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if harness.task(task_id)["status"] == wanted:
            return
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach {wanted}")


def _wait_submission(client, status_url: str, token: str, timeout: float = 60):
    deadline = time.monotonic() + timeout
    payload = None
    while time.monotonic() < deadline:
        response = client.get(
            status_url, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["final"]:
            return payload
        time.sleep(0.05)
    raise AssertionError(payload)


REVALIDATION_FILES = {
    "src/__init__.py": "",
    "src/dep.py": "def dep() -> int:\n    return 1\n",
    "src/target.py": "def f() -> int:\n    ...\n",
    "tests/__init__.py": "",
    "tests/test_revalidate.py": (
        "import time\n"
        "from src.dep import dep\n\n\n"
        "def test_f() -> None:\n"
        "    time.sleep(2)\n"
        "    assert dep() == 1\n"
    ),
}


def test_external_commit_affecting_read_dependency_forces_revalidation(
    tmp_path: Path,
) -> None:
    with kernel(tmp_path, REVALIDATION_FILES) as harness:
        for symbol, kind in (
            ("src/target.py::f", "stub"),
            ("src/dep.py::dep", "contract"),
            ("tests/test_revalidate.py::test_f", "test"),
        ):
            harness.artifact(symbol, kind)
        task = harness.create_task(
            title="Implement f against dep",
            spec_prompt="Implement f; the frozen test reads dep().",
            read_symbols=["src/dep.py::dep"],
            target_symbols=["src/target.py::f"],
            test_symbols=["tests/test_revalidate.py::test_f"],
            write_set_symbols=["src/target.py::f"],
        )
        bundle = harness.pull()
        diff = _patch(
            harness.base,
            harness.bare,
            harness.head,
            "src/target.py",
            "    ...",
            "    return 5",
        )
        key = "revalidate-external-commit"
        accepted = harness.client.post(
            "/v2/syscalls/submit_patch",
            headers={
                "Authorization": f"Bearer {harness.token}",
                "Idempotency-Key": key,
            },
            json={
                "task_id": task["task_id"],
                "lease_token": bundle["lease_token"],
                "git_diff": diff,
                "touched_symbols": ["src/target.py::f"],
                "local_test_passed": True,
            },
        )
        assert accepted.status_code == 202, accepted.text
        _wait_task_status(harness, task["task_id"], "VERIFYING")
        _external_commit(
            harness.bare,
            harness.head,
            "src/dep.py",
            "    return 1",
            "    return 2",
        )
        final = _wait_submission(
            harness.client,
            accepted.json()["status_url"],
            harness.token,
            timeout=120,
        )
        assert final["status"] == "FAILED", final
        assert final["task_status"] in {"LEASED", "FAILED"}, final
        failed = [gate for gate in final["gate_results"] if not gate["passed"]]
        assert failed, final
        # The dependency change invalidated the previous test run; the gate
        # reran and observed dep()==2.
        assert any(
            "UNIT_TEST_SUITE" in gate["stage"] for gate in failed
        ), failed
        assert harness.task(task["task_id"])["status"] != "COMMITTED"
        assert "return 2" in _run(["git", "show", "main:src/dep.py"], harness.bare)


def test_external_commit_causing_merge_conflict_is_never_committed(
    tmp_path: Path,
) -> None:
    files = {
        "src/__init__.py": "",
        "src/target.py": "def f() -> int:\n    ...\n",
        "tests/__init__.py": "",
        "tests/test_conflict.py": (
            "import time\n"
            "from src.target import f\n\n\n"
            "def test_f() -> None:\n"
            "    time.sleep(2)\n"
            "    assert f() == 5\n"
        ),
    }
    with kernel(tmp_path, files) as harness:
        harness.artifact("src/target.py::f", "stub")
        harness.artifact("tests/test_conflict.py::test_f", "test")
        task = harness.create_task(
            title="Implement f with merge conflict",
            spec_prompt="Implement f; an external commit will change the same line.",
            target_symbols=["src/target.py::f"],
            test_symbols=["tests/test_conflict.py::test_f"],
            write_set_symbols=["src/target.py::f"],
        )
        bundle = harness.pull()
        diff = _patch(
            harness.base,
            harness.bare,
            harness.head,
            "src/target.py",
            "    ...",
            "    return 5",
        )
        accepted = harness.client.post(
            "/v2/syscalls/submit_patch",
            headers={
                "Authorization": f"Bearer {harness.token}",
                "Idempotency-Key": "conflict-external-commit",
            },
            json={
                "task_id": task["task_id"],
                "lease_token": bundle["lease_token"],
                "git_diff": diff,
                "touched_symbols": ["src/target.py::f"],
                "local_test_passed": True,
            },
        )
        assert accepted.status_code == 202, accepted.text
        _wait_task_status(harness, task["task_id"], "VERIFYING")
        _external_commit(
            harness.bare,
            harness.head,
            "src/target.py",
            "    ...",
            "    return 42",
        )
        final = _wait_submission(
            harness.client,
            accepted.json()["status_url"],
            harness.token,
            timeout=120,
        )
        assert final["status"] == "FAILED", final
        assert final["task_status"] in {"LEASED", "FAILED"}, final
        failed = [gate for gate in final["gate_results"] if not gate["passed"]]
        assert failed, final
        assert any(
            gate["stage"] in {"WRITE_SET_BOUNDARY", "AST_SCOPE_DIFF"}
            for gate in failed
        ), failed
        assert harness.task(task["task_id"])["status"] != "COMMITTED"
        assert "return 42" in _run(
            ["git", "show", "main:src/target.py"], harness.bare
        )


# ---------------------------------------------------------------------------
# 21. Cell / repository placement
# ---------------------------------------------------------------------------
def test_cells_and_repository_placement(tmp_path: Path) -> None:
    with kernel(tmp_path, CALC_FILES) as harness:
        cells = harness.admin("GET", "/v1/admin/cells")
        assert any(cell["name"] == "default" for cell in cells), cells
        created = harness.client.post(
            "/v1/admin/cells",
            headers={"X-Admin-Key": ADMIN_KEY},
            json={"name": "cell-east", "capacity": {"cpu": 8}},
        )
        assert created.status_code == 201, created.text
        cell_id = created.json()["cell_id"]
        repositories = harness.admin("GET", "/v1/admin/repositories")
        repo = next(
            row for row in repositories if row["workspace_id"] == harness.workspace_id
        )
        epoch = repo["placement_epoch"]
        moved = harness.client.post(
            f"/v1/admin/repositories/{harness.workspace_id}/move",
            headers={"X-Admin-Key": ADMIN_KEY},
            json={"cell_id": cell_id, "expected_epoch": epoch},
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["placement_epoch"] == epoch + 1
        stale = harness.client.post(
            f"/v1/admin/repositories/{harness.workspace_id}/move",
            headers={"X-Admin-Key": ADMIN_KEY},
            json={"cell_id": cell_id, "expected_epoch": epoch},
        )
        assert stale.status_code == 409, stale.text
