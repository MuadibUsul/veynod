"""End-to-end proof that the new harness exits non-zero on an incomplete run.

Two real runs are executed against a fresh isolated database, a real bare Git
repository, the real deterministic gate and a real uvicorn server:

1. two tasks / two Runners / two SSE streams -> exit 0, DB dropped;
2. the same topology with `--expected-tasks 3` -> exit 1 with
   `task_count_matches`, proving the CLI gate is effective.

The test never touches the existing `veynod_test` database: it creates its own
uniquely named database through the admin connection.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bench_swarm.py"
TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/veynod_test",
)


def _admin_url() -> str:
    """Derive a maintenance connection, never the benchmarked test database."""
    if os.environ.get("BENCH_ADMIN_DATABASE_URL"):
        return os.environ["BENCH_ADMIN_DATABASE_URL"]
    raw = TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
    parts = urlsplit(raw)
    return urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))


def _available() -> bool:
    async def probe() -> bool:
        try:
            conn = await asyncpg.connect(_admin_url(), timeout=3)
            await conn.close()
            return True
        except Exception:
            return False

    try:
        return asyncio.run(probe())
    except Exception:
        return False


pytestmark = pytest.mark.integration


@pytest.mark.skipif(not _available(), reason="PostgreSQL admin database unavailable")
def test_new_harness_passes_complete_run_and_fails_on_missing_tasks(tmp_path):
    try:
        import veynod_core  # noqa: F401
    except Exception:
        pytest.skip("veynod_core extension is not built")

    common = [
        sys.executable,
        str(SCRIPT),
        "--scenario",
        "cross-file",
        "--files",
        "2",
        "--functions",
        "1",
        "--runners",
        "2",
        "--sse-streams",
        "2",
        "--profile",
        "dev",
        "--port",
        "0",
        "--admin-db-url",
        _admin_url(),
        "--output-dir",
        str(tmp_path / "out"),
        "--db-policy",
        "always-drop",
        "--max-duration-s",
        "60",
        "--drain-timeout-s",
        "45",
        "--submit-timeout-s",
        "90",
        "--think-min-s",
        "1.0",
        "--think-max-s",
        "1.0",
        "--heartbeat-interval-s",
        "0.5",
        "--sse-warmup-s",
        "8",
    ]

    passed = subprocess.run(
        common,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    assert passed.returncode == 0, passed.stderr + passed.stdout
    payload = json.loads(passed.stdout)
    acceptance = payload["acceptance"]
    assert acceptance["passed"] is True
    assert acceptance["exit_code"] == 0
    assert payload["database"]["dropped"] is True
    assert payload["client"]["sse"]["streams_opened_once"] == 2
    assert payload["client"]["heartbeat"]["attempts"] > 0
    assert payload["client_vs_database"]["client_committed_without_db"] == 0
    assert payload["client_vs_database"]["db_committed_without_client_success"] == 0
    assert payload["database"]["committed"] == 2
    assert payload["server"]["metrics_available"] is True

    failed = subprocess.run(
        common + ["--expected-tasks", "3"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    assert failed.returncode == 1, failed.stderr + failed.stdout
    failed_payload = json.loads(failed.stdout)
    reasons = {reason["code"] for reason in failed_payload["acceptance"]["reasons"]}
    assert "task_count_matches" in reasons
    assert failed_payload["acceptance"]["passed"] is False
    assert failed_payload["database"]["task_count"] == 2
    assert failed_payload["database"]["expected_tasks"] == 3
    assert failed_payload["database"]["committed"] == 2
