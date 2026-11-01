"""Capacity acceptance harness for the Veynod Kernel swarm.

This replaces the earlier benchmark whose exit code only reflected "the client
workers stopped", not whether the database reached a terminal state.  The
harness now:

* creates a uniquely named, isolated PostgreSQL database (never drops an
  existing schema and never reuses an existing database by default);
* gives every Runner its own issued token / worker identity and keeps lease
  heartbeats alive while the simulated agent is "thinking";
* separates client observations (HTTP 200, timeout, transport error) from the
  authoritative database status (`COMMITTED`, `FAILED`, non-terminal);
* records per-operation retries, HTTP errors, timeouts and latencies;
* samples queue depth, server-side DB-pool wait, gate wait and merge wait;
* waits for a bounded drain window after stopping claims, then evaluates the
  final database state and exits non-zero when the run is not acceptable;
* writes a machine-readable JSON result to disk and prints a JSON summary.

Examples (see docs/BENCH_ACCEPTANCE.md for the full runbook):

    python scripts/bench_swarm.py --scenario cross-file --profile dev \
        --files 1000 --functions 1 --runners 1000 --sse-streams 1000

    python scripts/bench_swarm.py --evaluate-json old-legacy-result.json

Exit codes:
    0  acceptance passed for the selected profile
    1  acceptance failed (task completion / error budget / data disagreement)
    2  setup or harness error (database, server, seeding, invalid arguments)
    130 interrupted
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import collections
import contextlib
import dataclasses
import difflib
import json
import math
import multiprocessing as mp
import os
import random
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import asyncpg
import httpx

try:
    import psutil
except ImportError:  # pragma: no cover - resource sampling is optional
    psutil = None

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCHEMA_VERSION = "bench-swarm/2"
TERMINAL_STATUSES = frozenset(
    {"COMMITTED", "FAILED", "SUSPENDED_ESCALATED", "SPLIT_ARCHIVED"}
)
NON_TERMINAL_STATUSES = frozenset(
    {"BLOCKED", "READY", "LEASED", "VERIFYING", "SUSPENDED"}
)
DEFAULT_ADMIN_DB_URL = os.environ.get(
    "BENCH_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:55432/postgres",
)
DEFAULT_ADMIN_KEY = "bench-admin"


class SetupError(RuntimeError):
    """Bad harness configuration, database, server or seed topology."""

    def __init__(
        self, message: str, *, partial_result: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.partial_result = partial_result


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)


class ResourceSampler:
    """CPU/RSS for the API, gate worker and harness process trees."""

    def __init__(self, roots: dict[str, int]) -> None:
        self.roots = roots
        self._previous: dict[str, tuple[float, float]] = {}
        self.samples: list[dict[str, Any]] = []
        self._cores = max(1, psutil.cpu_count(logical=True) or 1) if psutil else 1

    def sample(self) -> dict[str, Any]:
        if psutil is None:
            return {}
        now = time.monotonic()
        snapshot: dict[str, Any] = {}
        for name, pid in self.roots.items():
            try:
                root = psutil.Process(pid)
                processes = [root, *root.children(recursive=True)]
                cpu_seconds = sum(
                    sum(p.cpu_times()[:2]) for p in processes
                )
                rss_bytes = sum(p.memory_info().rss for p in processes)
            except (psutil.Error, OSError):
                continue
            previous = self._previous.get(name)
            cpu_pct = 0.0
            if previous is not None and now > previous[1]:
                cpu_pct = (
                    (cpu_seconds - previous[0]) / (now - previous[1]) * 100.0
                    / self._cores
                )
            self._previous[name] = (cpu_seconds, now)
            snapshot[name] = {
                "cpu_pct_machine": round(max(0.0, cpu_pct), 2),
                "rss_mb": round(rss_bytes / (1024 * 1024), 1),
                "processes": len(processes),
            }
        if snapshot:
            self.samples.append(snapshot)
        return snapshot

    def summary(self) -> dict[str, Any]:
        if not self.samples:
            return {"available": psutil is not None, "components": {}}
        components: dict[str, Any] = {}
        for name in self.roots:
            rows = [sample[name] for sample in self.samples if name in sample]
            if not rows:
                continue
            components[name] = {
                "cpu_pct_machine_avg": round(
                    sum(row["cpu_pct_machine"] for row in rows) / len(rows), 2
                ),
                "cpu_pct_machine_max": max(
                    row["cpu_pct_machine"] for row in rows
                ),
                "rss_mb_avg": round(
                    sum(row["rss_mb"] for row in rows) / len(rows), 1
                ),
                "rss_mb_max": max(row["rss_mb"] for row in rows),
                "processes_max": max(row["processes"] for row in rows),
                "samples": len(rows),
            }
        return {"available": True, "cpu_cores": self._cores, "components": components}


class Latencies:
    def __init__(self) -> None:
        self.values: list[float] = []

    def add(self, milliseconds: float) -> None:
        self.values.append(float(milliseconds))

    def summary(self) -> dict[str, Any]:
        return {
            "count": len(self.values),
            "p50_ms": round(percentile(self.values, 0.50), 2),
            "p90_ms": round(percentile(self.values, 0.90), 2),
            "p95_ms": round(percentile(self.values, 0.95), 2),
            "p99_ms": round(percentile(self.values, 0.99), 2),
            "max_ms": round(max(self.values), 2) if self.values else 0.0,
        }

    def merge_summary(self) -> dict[str, Any]:
        return self.summary()


@dataclass
class OpCounters:
    attempts: int = 0
    success: int = 0
    empty: int = 0
    http_4xx: int = 0
    http_5xx: int = 0
    timeouts: int = 0
    transport_errors: int = 0
    retries: int = 0
    other_errors: int = 0
    not_leased: int = 0
    gate_rejected: int = 0
    admission_denied: int = 0

    @property
    def http_errors(self) -> int:
        return self.http_4xx + self.http_5xx

    @property
    def total_errors(self) -> int:
        return self.http_errors + self.timeouts + self.transport_errors + self.other_errors

    def add(self, other: "OpCounters") -> None:
        for field_name in dataclasses.fields(self):
            setattr(
                self,
                field_name.name,
                getattr(self, field_name.name) + getattr(other, field_name.name),
            )

    def as_dict(self) -> dict[str, Any]:
        attempts = self.attempts
        return {
            "attempts": attempts,
            "success": self.success,
            "empty": self.empty,
            "http_4xx": self.http_4xx,
            "http_5xx": self.http_5xx,
            "http_errors": self.http_errors,
            "timeouts": self.timeouts,
            "transport_errors": self.transport_errors,
            "other_errors": self.other_errors,
            "not_leased": self.not_leased,
            "gate_rejected": self.gate_rejected,
            "admission_denied": self.admission_denied,
            "retries": self.retries,
            "http_error_rate": round(self.http_errors / attempts, 6) if attempts else 0.0,
            "transport_error_rate": round(
                (self.timeouts + self.transport_errors) / attempts, 6
            )
            if attempts
            else 0.0,
            "retry_rate": round(self.retries / attempts, 6) if attempts else 0.0,
        }


@dataclass
class SseCounters(OpCounters):
    opens: int = 0
    streams_opened_once: int = 0
    frames: int = 0
    bytes: int = 0
    reconnects: int = 0

    def as_dict(self) -> dict[str, Any]:
        data = super().as_dict()
        data.update(
            {
                "opens": self.opens,
                "streams_opened_once": self.streams_opened_once,
                "frames": self.frames,
                "bytes": self.bytes,
                "reconnects": self.reconnects,
            }
        )
        return data


@dataclass
class TaskSpec:
    symbol: str
    test_symbol: str
    diff: str
    file_path: str
    test_path: str


@dataclass(frozen=True)
class RunnerIdentity:
    index: int
    label: str
    worker_hub_id: str
    token: str


@dataclass
class QueueSample:
    elapsed_s: float
    statuses: dict[str, int]
    task_count: int
    committed: int
    non_terminal: int
    in_flight: int
    accepted: int
    queues: dict[str, int] = dataclasses.field(default_factory=dict)


@dataclass
class RunConfig:
    scenario: str
    files: int
    functions: int
    runners: int
    sse_streams: int
    expected_tasks: int
    profile: str
    skip_typecheck: bool
    skip_lint: bool
    label: str
    host: str
    port: int
    output_dir: Path
    admin_db_url: str
    database_name: str | None
    reuse_database: bool
    db_policy: str
    keep_work_dir: bool
    server_start_timeout_s: float
    mode: str
    api_instances: int
    submit_instances: int
    client_shards: int
    db_pool_size: int
    db_max_overflow: int
    max_duration_s: float
    drain_timeout_s: float
    claim_timeout_s: float
    empty_claim_backoff_s: float
    lease_seconds: int
    submit_accept_timeout_s: float
    submit_timeout_s: float
    sse_timeout_s: float
    heartbeat_interval_s: float
    think_min_s: float
    think_max_s: float
    sample_interval_s: float
    metrics_interval_s: float
    reconcile_interval_s: float
    sse_warmup_s: float
    fast_forward_merge: bool
    gate_pool_size: int
    gate_workers: int
    gate_worker_port: int
    gate_sandbox: str
    gate_sandbox_image: str | None
    work_lease_s: int
    reaper_interval_s: int
    setup_concurrency: int
    max_http_error_rate: float
    max_transport_error_rate: float
    max_submit_error_rate: float
    max_sse_error_rate: float
    max_retry_rate: float
    max_error_events: int | None
    max_pool_timeouts: int
    max_failed: int
    require_all_committed: bool
    min_sse_open_ratio: float
    max_accept_p99_ms: float
    log_level: str
    evaluate_json: Path | None


# ---------------------------------------------------------------------------
# Acceptance evaluator (pure; shared by live runs and legacy replay)
# ---------------------------------------------------------------------------
@dataclass
class AcceptanceInput:
    expected_tasks: int
    db_task_count: int
    missing_tasks: int
    extra_tasks: int
    final_statuses: dict[str, int]
    client_statuses: dict[str, int]
    accepted_tasks: int
    accepted_non_terminal: int
    client_committed_without_db: int
    ops: dict[str, dict[str, int]]
    sse: dict[str, Any]
    pool_timeouts: int
    production_grade: bool
    profile: str
    fatal_errors: list[str]
    metrics_available: bool
    detail_complete: bool
    max_failed: int
    require_all_committed: bool
    max_http_error_rate: float
    max_transport_error_rate: float
    max_submit_error_rate: float
    max_sse_error_rate: float
    max_retry_rate: float
    max_error_events: int | None
    max_pool_timeouts: int
    min_sse_open_ratio: float
    submit_accept_p99_ms: float | None = None
    max_accept_p99_ms: float = 1000.0
    server_submit_incomplete: int = 0
    legacy_http_errors: int = 0


def _check(
    checks: dict[str, bool],
    reasons: list[dict[str, Any]],
    code: str,
    passed: bool,
    detail: str,
    observed: Any = None,
    limit: Any = None,
) -> None:
    checks[code] = passed
    if not passed:
        reason = {"code": code, "detail": detail}
        if observed is not None:
            reason["observed"] = observed
        if limit is not None:
            reason["limit"] = limit
        reasons.append(reason)


def evaluate_acceptance(inp: AcceptanceInput) -> dict[str, Any]:
    statuses = {str(k): int(v) for k, v in inp.final_statuses.items()}
    committed = statuses.get("COMMITTED", 0)
    failed = statuses.get("FAILED", 0)
    terminal = sum(v for k, v in statuses.items() if k in TERMINAL_STATUSES)
    non_terminal = sum(v for k, v in statuses.items() if k in NON_TERMINAL_STATUSES)
    unknown = sum(v for k, v in statuses.items() if k not in TERMINAL_STATUSES and k not in NON_TERMINAL_STATUSES)
    if unknown:
        non_terminal += unknown

    checks: dict[str, bool] = {}
    reasons: list[dict[str, Any]] = []
    warnings: list[str] = []

    _check(
        checks,
        reasons,
        "task_count_matches",
        inp.db_task_count == inp.expected_tasks
        and inp.missing_tasks == 0
        and inp.extra_tasks == 0,
        "database task count, missing tasks or extra tasks differ from the expected topology",
        observed={
            "db_task_count": inp.db_task_count,
            "expected_tasks": inp.expected_tasks,
            "missing_tasks": inp.missing_tasks,
            "extra_tasks": inp.extra_tasks,
        },
        limit={"missing_tasks": 0, "extra_tasks": 0},
    )
    _check(
        checks,
        reasons,
        "all_tasks_terminal",
        non_terminal == 0,
        "some tasks never reached a terminal status",
        observed={
            "non_terminal": non_terminal,
            "terminal": terminal,
            "statuses": statuses,
        },
        limit={"non_terminal": 0},
    )
    committed_ok = (
        committed == inp.expected_tasks
        if inp.require_all_committed
        else committed >= inp.expected_tasks - inp.max_failed
        and failed <= inp.max_failed
    )
    _check(
        checks,
        reasons,
        "committed_tasks",
        committed_ok,
        "not enough tasks reached COMMITTED",
        observed={
            "committed": committed,
            "failed": failed,
            "expected_tasks": inp.expected_tasks,
        },
        limit={
            "require_all_committed": inp.require_all_committed,
            "max_failed": inp.max_failed,
        },
    )
    _check(
        checks,
        reasons,
        "accepted_submissions_terminal",
        inp.accepted_non_terminal == 0,
        "some submit_patch requests returned HTTP 200 but the task is still non-terminal",
        observed={
            "accepted_tasks": inp.accepted_tasks,
            "accepted_non_terminal": inp.accepted_non_terminal,
        },
        limit={"accepted_non_terminal": 0},
    )
    _check(
        checks,
        reasons,
        "client_database_commit_agreement",
        inp.client_committed_without_db == 0,
        "a client received new_status=COMMITTED but the database does not show COMMITTED",
        observed={"client_committed_without_db": inp.client_committed_without_db},
        limit={"client_committed_without_db": 0},
    )

    # Error budget per operation.  Empty claims are not errors.
    for op, raw in sorted(inp.ops.items()):
        attempts = int(raw.get("attempts", 0))
        if attempts <= 0:
            continue
        http_errors = int(raw.get("http_4xx", 0)) + int(raw.get("http_5xx", 0))
        transport_errors = int(raw.get("timeouts", 0)) + int(
            raw.get("transport_errors", 0)
        )
        retries = int(raw.get("retries", 0))
        http_rate = http_errors / attempts
        transport_rate = transport_errors / attempts
        retry_rate = retries / attempts
        _check(
            checks,
            reasons,
            f"{op}_http_error_budget",
            http_rate <= inp.max_http_error_rate,
            f"{op} HTTP error rate exceeds the budget",
            observed={"rate": round(http_rate, 6), "errors": http_errors, "attempts": attempts},
            limit=inp.max_http_error_rate,
        )
        _check(
            checks,
            reasons,
            f"{op}_transport_error_budget",
            transport_rate <= inp.max_transport_error_rate,
            f"{op} timeout/transport error rate exceeds the budget",
            observed={
                "rate": round(transport_rate, 6),
                "errors": transport_errors,
                "attempts": attempts,
            },
            limit=inp.max_transport_error_rate,
        )
        _check(
            checks,
            reasons,
            f"{op}_retry_budget",
            retry_rate <= inp.max_retry_rate,
            f"{op} retry rate exceeds the budget",
            observed={"rate": round(retry_rate, 6), "retries": retries, "attempts": attempts},
            limit=inp.max_retry_rate,
        )
        if op == "submit":
            total_errors = http_errors + transport_errors + int(raw.get("other_errors", 0))
            _check(
                checks,
                reasons,
                "submit_error_budget",
                total_errors / attempts <= inp.max_submit_error_rate,
                "submit_patch failure rate exceeds the budget",
                observed={
                    "rate": round(total_errors / attempts, 6),
                    "errors": total_errors,
                    "attempts": attempts,
                },
                limit=inp.max_submit_error_rate,
            )

    if inp.submit_accept_p99_ms is not None:
        _check(
            checks,
            reasons,
            "submit_accept_p99_budget",
            inp.submit_accept_p99_ms <= inp.max_accept_p99_ms,
            "submit_patch accept P99 exceeds the budget",
            observed={"p99_ms": round(inp.submit_accept_p99_ms, 2)},
            limit={"p99_ms": inp.max_accept_p99_ms},
        )

    if inp.max_error_events is not None:
        total_errors = sum(
            int(raw.get("http_4xx", 0))
            + int(raw.get("http_5xx", 0))
            + int(raw.get("timeouts", 0))
            + int(raw.get("transport_errors", 0))
            + int(raw.get("other_errors", 0))
            for raw in inp.ops.values()
        )
        total_errors += inp.legacy_http_errors
        _check(
            checks,
            reasons,
            "absolute_error_budget",
            total_errors <= inp.max_error_events,
            "absolute HTTP/transport error count exceeds the budget",
            observed=total_errors,
            limit=inp.max_error_events,
        )
    else:
        total_errors = sum(
            int(raw.get("http_4xx", 0))
            + int(raw.get("http_5xx", 0))
            + int(raw.get("timeouts", 0))
            + int(raw.get("transport_errors", 0))
            + int(raw.get("other_errors", 0))
            for raw in inp.ops.values()
        ) + inp.legacy_http_errors

    streams = int(inp.sse.get("streams", 0))
    if streams > 0 and inp.detail_complete:
        opened_once = int(inp.sse.get("streams_opened_once", inp.sse.get("opens", 0)))
        open_ratio = opened_once / streams if streams else 0.0
        attempts = int(inp.sse.get("attempts", 0))
        sse_errors = (
            int(inp.sse.get("http_errors", 0))
            + int(inp.sse.get("timeouts", 0))
            + int(inp.sse.get("transport_errors", 0))
        )
        sse_rate = sse_errors / attempts if attempts else 0.0
        _check(
            checks,
            reasons,
            "sse_open_ratio",
            open_ratio >= inp.min_sse_open_ratio,
            "not enough SSE streams received an HTTP 200 connection",
            observed={"opened_once": opened_once, "streams": streams, "ratio": round(open_ratio, 6)},
            limit=inp.min_sse_open_ratio,
        )
        _check(
            checks,
            reasons,
            "sse_error_budget",
            sse_rate <= inp.max_sse_error_rate,
            "SSE connection error rate exceeds the budget",
            observed={"rate": round(sse_rate, 6), "errors": sse_errors, "attempts": attempts},
            limit=inp.max_sse_error_rate,
        )
        _check(
            checks,
            reasons,
            "sse_frames_received",
            int(inp.sse.get("frames", 0)) > 0,
            "SSE streams opened but no event frames were received",
            observed={"frames": int(inp.sse.get("frames", 0))},
            limit={">": 0},
        )
    elif streams == 0:
        checks["sse_open_ratio"] = True
        checks["sse_error_budget"] = True
        checks["sse_frames_received"] = True

    _check(
        checks,
        reasons,
        "db_pool_timeout_budget",
        inp.pool_timeouts <= inp.max_pool_timeouts,
        "database connection-pool timeouts exceed the budget",
        observed=inp.pool_timeouts,
        limit=inp.max_pool_timeouts,
    )
    _check(
        checks,
        reasons,
        "server_submit_incomplete",
        inp.server_submit_incomplete == 0,
        "submit_patch requests entered the server gate but never finished",
        observed=inp.server_submit_incomplete,
        limit=0,
    )
    _check(
        checks,
        reasons,
        "no_fatal_harness_errors",
        not inp.fatal_errors,
        "the harness recorded fatal errors",
        observed=inp.fatal_errors[:20],
        limit=[],
    )
    if inp.profile == "production":
        _check(
            checks,
            reasons,
            "production_gate_profile",
            inp.production_grade,
            "production profile requires typecheck+lint plus server metrics",
            observed={"production_grade": inp.production_grade},
            limit=True,
        )
        if not inp.metrics_available:
            warnings.append(
                "server metrics are unavailable; DB-pool/gate/merge waits are incomplete"
            )
    else:
        checks["production_gate_profile"] = True
        warnings.append(
            "profile=dev: typecheck/lint and/or server metrics may be skipped; "
            "this run is NOT production capacity evidence"
        )

    if not inp.detail_complete:
        warnings.append(
            "detail_complete=false: this is a legacy replay; per-operation "
            "HTTP/timeout/retry breakdown and SSE opens are unavailable"
        )

    passed = not reasons
    return {
        "passed": passed,
        "capacity_accepted": passed,
        "exit_code": 0 if passed else 1,
        "production_grade": inp.production_grade,
        "checks": checks,
        "reasons": reasons,
        "warnings": warnings,
        "summary": {
            "expected_tasks": inp.expected_tasks,
            "db_task_count": inp.db_task_count,
            "committed": committed,
            "failed": failed,
            "terminal": terminal,
            "non_terminal": non_terminal,
            "total_errors": total_errors,
            "pool_timeouts": inp.pool_timeouts,
            "server_submit_incomplete": inp.server_submit_incomplete,
        },
    }


# ---------------------------------------------------------------------------
# Source / repository generation
# ---------------------------------------------------------------------------
def _run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise SetupError(
            f"{cmd} failed ({result.returncode})\n{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def source_text(functions: int, implemented: int | None = None) -> str:
    chunks = []
    for index in range(functions):
        body = "    return a + b\n" if index == implemented else "    ...\n"
        chunks.append(
            f"def f{index}(a: int, b: int) -> int:\n"
            f'    """Function {index}."""\n'
            f"{body}\n"
        )
    return "\n".join(chunks)


def test_text(module: str, function_index: int) -> str:
    nl = chr(10)
    return (
        f"from {module} import f{function_index}{nl}{nl}{nl}"
        f"def test_f{function_index}() -> None:{nl}"
        f"    assert f{function_index}(2, 3) == 5{nl}"
    )


def make_repo(
    base: Path, files: int, functions: int, scenario: str
) -> tuple[Path, list[TaskSpec], str]:
    work = base / "work"
    work.mkdir(parents=True)
    _run(["git", "init", "-b", "main"], work)
    _run(["git", "config", "user.name", "Swarm"], work)
    _run(["git", "config", "user.email", "swarm@example.com"], work)
    (work / "src").mkdir()
    (work / "src" / "__init__.py").write_text("", encoding="utf-8")
    (work / "tests").mkdir()
    (work / "tests" / "__init__.py").write_text("", encoding="utf-8")

    specs: list[TaskSpec] = []
    for file_index in range(files):
        path = "src/target.py" if files == 1 else f"src/f{file_index:03d}.py"
        base_source = source_text(functions)
        (work / path).write_text(base_source, encoding="utf-8")
        module = "src.target" if files == 1 else f"src.f{file_index:03d}"
        for function_index in range(functions):
            modified = source_text(functions, function_index)
            diff = "".join(
                difflib.unified_diff(
                    base_source.splitlines(keepends=True),
                    modified.splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
            symbol = f"{path}::f{function_index}"
            test_path = f"tests/test_f{file_index:03d}_{function_index}.py"
            (work / test_path).write_text(
                test_text(module, function_index), encoding="utf-8"
            )
            specs.append(
                TaskSpec(
                    symbol=symbol,
                    test_symbol=f"{test_path}::test_f{function_index}",
                    diff=diff,
                    file_path=path,
                    test_path=test_path,
                )
            )
    _run(["git", "add", "-A"], work)
    _run(["git", "commit", "-m", "seed"], work)
    head = _run(["git", "rev-parse", "HEAD"], work).strip()
    bare = base / "bare.git"
    _run(["git", "clone", "--bare", str(work), str(bare)], base)
    _run(["git", "config", "gc.auto", "0"], bare)
    return bare, specs, head


# ---------------------------------------------------------------------------
# Isolated database lifecycle
# ---------------------------------------------------------------------------
def _replace_database(url: str, name: str) -> str:
    prefix, rest = url.split("://", 1)
    netloc, _, path_query = rest.partition("/")
    _path, sep, query = path_query.partition("?")
    suffix = f"?{query}" if sep else ""
    return f"{prefix}://{netloc}/{name}{suffix}"


def _database_name_from_url(url: str) -> str:
    _, rest = url.split("://", 1)
    _, _, path_query = rest.partition("/")
    return path_query.split("?", 1)[0]


def _sqlalchemy_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def _asyncpg_dsn(url: str) -> str:
    return (
        url.replace("postgresql+asyncpg://", "postgresql://", 1)
        .replace("postgres://", "postgresql://", 1)
    )


def _assert_port_free(port: int, label: str) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise SetupError(
            f"{label} port {port} is already in use; stop the old process or choose "
            f"another --port/--gate-worker-port ({exc})"
        ) from exc
    finally:
        probe.close()


def _safe_db_name(scenario: str, label: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", f"veynod_bench_{scenario}_{label}".lower())
    slug = slug.strip("_")[:36] or "veynod_bench"
    return f"{slug}_{utc_stamp().lower()}_{secrets.token_hex(3)}"


async def _create_database(admin_url: str, name: str) -> None:
    conn = await asyncpg.connect(_asyncpg_dsn(admin_url))
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", name
        )
        if exists:
            raise SetupError(
                f"Database {name!r} already exists; refusing to reuse it. "
                "Use --reuse-database only when you deliberately want that."
            )
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop_database(admin_url: str, name: str) -> None:
    conn = await asyncpg.connect(_asyncpg_dsn(admin_url))
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            name,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


async def _database_has_workspaces(admin_url: str, name: str) -> bool:
    """True when a reused database already contains kernel data.

    The syscalls resolve the *oldest* workspace when no workspace id is pinned.
    Reusing a database that already has another workspace would therefore make
    the new workers claim the wrong workspace, so reuse is only allowed for an
    empty (schema-free) isolated database.
    """
    conn = await asyncpg.connect(
        _asyncpg_dsn(_replace_database(admin_url, name))
    )
    try:
        table = await conn.fetchval("SELECT to_regclass('public.workspaces')")
        if table is None:
            return False
        count = await conn.fetchval("SELECT count(*) FROM workspaces")
        return bool(count)
    finally:
        await conn.close()


async def _database_exists(admin_url: str, name: str) -> bool:
    conn = await asyncpg.connect(_asyncpg_dsn(admin_url))
    try:
        return bool(
            await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", name
            )
        )
    finally:
        await conn.close()


class DbProbe:
    """Authoritative task state, on a harness-owned asyncpg pool."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None
        self.errors = 0

    async def start(self) -> None:
        for attempt in range(60):
            try:
                self.pool = await asyncpg.create_pool(
                    self.dsn, min_size=1, max_size=4, command_timeout=15
                )
                return
            except Exception:
                if attempt == 59:
                    raise
                await asyncio.sleep(0.25)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def snapshot(
        self, workspace_id: str, expected_ids: set[str]
    ) -> dict[str, Any]:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT task_id::text AS task_id,
                       status::text AS status,
                       result_commit_sha,
                       retry_count
                FROM dag_tasks
                WHERE workspace_id = $1::uuid
                """,
                workspace_id,
            )
            queues = await conn.fetchrow(
                """
                SELECT
                    (SELECT count(*) FROM patch_submissions
                     WHERE workspace_id = $1::uuid AND status = 'QUEUED') AS gate_queued,
                    (SELECT count(*) FROM patch_submissions
                     WHERE workspace_id = $1::uuid AND status = 'RUNNING') AS gate_running,
                    (SELECT count(*) FROM merge_intents
                     WHERE workspace_id = $1::uuid AND status IN ('PENDING', 'RUNNING')) AS merge_queue,
                    (SELECT count(*) FROM edge_rebuild_jobs
                     WHERE workspace_id = $1::uuid AND status IN ('PENDING', 'RUNNING')) AS edge_queue,
                    (SELECT count(*) FROM admission_permits
                     WHERE workspace_id = $1::uuid
                       AND expires_at > NOW()) AS admission_permits
                """,
                workspace_id,
            )
        by_id = {row["task_id"]: dict(row) for row in rows}
        statuses: collections.Counter[str] = collections.Counter(
            row["status"] for row in rows
        )
        actual_ids = set(by_id)
        terminal = sum(
            count for status, count in statuses.items() if status in TERMINAL_STATUSES
        )
        non_terminal = sum(
            count for status, count in statuses.items() if status not in TERMINAL_STATUSES
        )
        return {
            "task_count": len(rows),
            "statuses": dict(sorted(statuses.items())),
            "queues": dict(queues) if queues else {},
            "terminal": terminal,
            "non_terminal": non_terminal,
            "committed": statuses.get("COMMITTED", 0),
            "failed": statuses.get("FAILED", 0),
            "result_commit_tasks": sum(
                1 for row in rows if row["result_commit_sha"] is not None
            ),
            "missing_tasks": len(expected_ids - actual_ids),
            "extra_tasks": len(actual_ids - expected_ids),
            "by_id": by_id,
            "committed_ids": {
                task_id
                for task_id, row in by_id.items()
                if row["status"] == "COMMITTED"
            },
        }

    async def task_status(self, task_id: str) -> dict[str, Any] | None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT task_id::text AS task_id, status::text AS status,
                       retry_count, lease_expires_at, result_commit_sha
                FROM dag_tasks WHERE task_id = $1::uuid
                """,
                task_id,
            )
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
class RunStats:
    def __init__(self, config: RunConfig) -> None:
        self.config = config
        self.claim = OpCounters()
        self.submit = OpCounters()
        self.release = OpCounters()
        self.heartbeat = OpCounters()
        self.setup = OpCounters()
        self.sse = SseCounters()
        self.latencies: dict[str, Latencies] = collections.defaultdict(Latencies)
        self.client_statuses: collections.Counter[str] = collections.Counter()
        self.merge_modes: collections.Counter[str] = collections.Counter()
        self.client_gate_stage_ms: dict[str, Latencies] = collections.defaultdict(Latencies)
        self.client_merge_timing_ms: dict[str, Latencies] = collections.defaultdict(Latencies)
        self.client_outcomes: dict[str, str] = {}
        self.accepted_task_ids: set[str] = set()
        self.client_committed_ids: set[str] = set()
        self.reconciled: collections.Counter[str] = collections.Counter()
        self.fatal_errors: list[str] = []
        self.error_samples: list[dict[str, Any]] = []
        self.in_flight: dict[str, dict[str, Any]] = {}
        self.reconciling: dict[str, asyncio.Task] = {}
        self.claim_backoff: dict[int, int] = collections.defaultdict(int)
        self.queue_samples: list[QueueSample] = []
        self.metrics_samples: list[dict[str, Any]] = []
        self.initial_queue: dict[str, Any] | None = None
        self.final_queue: dict[str, Any] | None = None
        self.monitor_errors = 0
        self.submission_wait_retries = 0
        self.submission_wait_timeouts = 0
        self.submission_wait_errors = 0
        self.peak: dict[str, int] = collections.defaultdict(int)
        self.peak_total = 0
        self.peak_checkedout = 0
        self.last_queue: dict[str, Any] | None = None

    def fatal(self, message: str) -> None:
        self.fatal_errors.append(message)
        if len(self.fatal_errors) > 50:
            del self.fatal_errors[:-50]

    def sample_error(self, kind: str, status: int | None, body: str) -> None:
        if len(self.error_samples) >= 25:
            return
        self.error_samples.append(
            {
                "kind": kind,
                "status": status,
                "body": body[:500],
            }
        )

    def record_queue(self, snapshot: Mapping[str, Any], elapsed_s: float) -> None:
        statuses = dict(snapshot["statuses"])
        for status, count in statuses.items():
            if count > self.peak.get(status, 0):
                self.peak[status] = count
        total = int(snapshot["non_terminal"])
        self.peak_total = max(self.peak_total, total)
        sample = QueueSample(
            elapsed_s=round(elapsed_s, 3),
            statuses=statuses,
            task_count=int(snapshot["task_count"]),
            committed=int(snapshot.get("committed", 0)),
            non_terminal=total,
            in_flight=len(self.in_flight),
            accepted=len(self.accepted_task_ids),
            queues={
                str(key): int(value)
                for key, value in (snapshot.get("queues") or {}).items()
            },
        )
        if len(self.queue_samples) < 10_000:
            self.queue_samples.append(sample)
        self.last_queue = snapshot

    def as_client_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim.as_dict(),
            "submit": self.submit.as_dict(),
            "release": self.release.as_dict(),
            "heartbeat": self.heartbeat.as_dict(),
            "sse": self.sse.as_dict(),
            "setup": self.setup.as_dict(),
            "latency_ms": {
                key: recorder.summary() for key, recorder in sorted(self.latencies.items())
            },
            "client_submit_statuses": dict(sorted(self.client_statuses.items())),
            "client_merge_modes": dict(sorted(self.merge_modes.items())),
            "client_gate_stage_ms": {
                key: recorder.summary()
                for key, recorder in sorted(self.client_gate_stage_ms.items())
            },
            "client_merge_timing_ms": {
                key: recorder.summary()
                for key, recorder in sorted(self.client_merge_timing_ms.items())
            },
            "task_outcomes": dict(sorted(self.client_outcomes.items())),
            "accepted_task_ids_count": len(self.accepted_task_ids),
            "client_committed_ids_count": len(self.client_committed_ids),
            "reconciled": dict(sorted(self.reconciled.items())),
            "submission_wait": {
                "retries": self.submission_wait_retries,
                "timeouts": self.submission_wait_timeouts,
                "errors": self.submission_wait_errors,
            },
            "error_samples": self.error_samples,
            "fatal_errors": self.fatal_errors,
        }


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------
class ServerHandle:
    def __init__(
        self,
        api: str,
        port: int,
        db_url: str,
        admin_key: str,
        log_path: Path,
        config: RunConfig,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.api = api
        self.port = port
        self.db_url = db_url
        self.admin_key = admin_key
        self.log_path = log_path
        self.config = config
        self.extra_env = extra_env or {}
        self.proc: subprocess.Popen | None = None
        self.log_handle = None
        self.exit_code: int | None = None

    def start(self) -> None:
        env = {
            **os.environ,
            "DATABASE_URL": self.db_url,
            "VN_AUTH_MODE": "prod",
            "VN_ADMIN_API_KEY": self.admin_key,
            "VN_SKIP_TYPECHECK": "true" if self.config.skip_typecheck else "false",
            "VN_SKIP_LINT": "true" if self.config.skip_lint else "false",
            "VN_FAST_FORWARD_MERGE": "true" if self.config.fast_forward_merge else "false",
            "VN_GATE_WORKTREE_POOL_SIZE": str(self.config.gate_pool_size),
            "VN_REAPER_INTERVAL_SECONDS": str(self.config.reaper_interval_s),
            "VN_TEST_CMD": os.environ.get(
                "VN_TEST_CMD",
                "pytest -q --tb=short --no-header --no-summary "
                "-p no:cov -p no:warnings -p no:doctest -p no:pastebin "
                "-p no:junitxml -p no:anyio",
            ),
            "VN_SSE_POLL_SECONDS": "0.25",
            "VN_LEASE_SECONDS": str(self.config.lease_seconds),
            "VN_VERIFY_LEASE_SECONDS": "1800",
            "VN_DB_POOL_SIZE": str(self.config.db_pool_size),
            "VN_DB_MAX_OVERFLOW": str(self.config.db_max_overflow),
            "VN_AUTO_INIT_DB": "true",
            "PYTHONUNBUFFERED": "1",
            **self.extra_env,
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = self.log_path.open("w", encoding="utf-8")
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--log-level",
            self.config.log_level,
            "--backlog",
            "4096",
            "--timeout-keep-alive",
            "10",
        ]
        self.proc = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=env,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            **kwargs,
        )

    def wait_health(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        last_error = "no response"
        with httpx.Client(timeout=5.0) as client:
            while time.monotonic() < deadline:
                if self.proc is not None and self.proc.poll() is not None:
                    raise SetupError(
                        f"server exited during startup with code {self.proc.returncode}; "
                        f"see {self.log_path}"
                    )
                try:
                    response = client.get(f"{self.api}/healthz")
                    if response.status_code == 200:
                        return
                    last_error = f"HTTP {response.status_code}"
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(0.10)
        raise SetupError(
            f"server did not become healthy within {timeout_s:.0f}s ({last_error}); "
            f"see {self.log_path}"
        )

    def stop(self, timeout_s: float = 15.0) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            if os.name == "nt":
                # Kill the whole process tree: pytest and git children of the
                # server must not survive the benchmark and distort the next
                # run.  Metrics/logs are already collected before stop().
                with contextlib.suppress(Exception):
                    subprocess.run(
                        ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                        capture_output=True,
                        text=True,
                    )
                try:
                    self.exit_code = self.proc.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(Exception):
                        self.proc.kill()
                    self.exit_code = self.proc.wait(timeout=5)
            else:
                with contextlib.suppress(Exception):
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                try:
                    self.exit_code = self.proc.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(Exception):
                        os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                    self.exit_code = self.proc.wait(timeout=5)
        else:
            self.exit_code = self.proc.returncode
        if self.log_handle is not None:
            self.log_handle.flush()
            self.log_handle.close()
            self.log_handle = None

    def log_text(self) -> str:
        if not self.log_path.exists():
            return ""
        return self.log_path.read_text(encoding="utf-8", errors="replace")


class GateWorkerHandle:
    """Independent deterministic-gate worker process for the benchmark."""

    def __init__(
        self,
        db_url: str,
        metrics_port: int,
        log_path: Path,
        config: RunConfig,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.db_url = db_url
        self.metrics_port = metrics_port
        self.metrics_url = f"http://127.0.0.1:{metrics_port}"
        self.log_path = log_path
        self.config = config
        self.extra_env = extra_env or {}
        self.proc: subprocess.Popen | None = None
        self.log_handle = None
        self.exit_code: int | None = None

    def start(self) -> None:
        env = {
            **os.environ,
            "DATABASE_URL": self.db_url,
            "VN_SKIP_TYPECHECK": "true" if self.config.skip_typecheck else "false",
            "VN_SKIP_LINT": "true" if self.config.skip_lint else "false",
            "VN_FAST_FORWARD_MERGE": "true" if self.config.fast_forward_merge else "false",
            "VN_GATE_WORKTREE_POOL_SIZE": str(self.config.gate_pool_size),
            "VN_TEST_CMD": os.environ.get(
                "VN_TEST_CMD",
                "pytest -q --tb=short --no-header --no-summary "
                "-p no:cov -p no:warnings -p no:doctest -p no:pastebin "
                "-p no:junitxml -p no:anyio",
            ),
            "VN_WORK_LEASE_SECONDS": str(self.config.work_lease_s),
            "VN_WORK_HEARTBEAT_SECONDS": "10",
            "VN_WORKER_POLL_SECONDS": "0.05",
            "VN_GATE_SANDBOX": self.config.gate_sandbox,
            "VN_DB_POOL_SIZE": str(self.config.db_pool_size),
            "VN_DB_MAX_OVERFLOW": str(self.config.db_max_overflow),
            "VN_AUTO_INIT_DB": "true",
            "PYTHONUNBUFFERED": "1",
        }
        if self.config.gate_sandbox_image:
            env["VN_GATE_SANDBOX_IMAGE"] = self.config.gate_sandbox_image
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = self.log_path.open("w", encoding="utf-8")
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        command = [
            sys.executable,
            "-m",
            "app.worker",
            "--workers",
            str(max(1, self.config.gate_workers)),
            "--database-url",
            self.db_url,
            "--metrics-port",
            str(self.metrics_port),
            "--work-lease-seconds",
            str(self.config.work_lease_s),
            "--poll-seconds",
            "0.05",
            "--sandbox",
            self.config.gate_sandbox,
        ]
        if self.config.gate_sandbox_image:
            command.extend(["--sandbox-image", self.config.gate_sandbox_image])
        self.proc = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=env,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            **kwargs,
        )

    def wait_health(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        with httpx.Client(timeout=3.0) as client:
            while time.monotonic() < deadline:
                if self.proc is not None and self.proc.poll() is not None:
                    raise SetupError(
                        f"gate worker exited with {self.proc.returncode}; "
                        f"{self.log_path}"
                    )
                try:
                    if client.get(f"{self.metrics_url}/metrics").status_code == 200:
                        return
                except Exception:
                    pass
                time.sleep(0.1)
        raise SetupError(f"gate worker metrics did not become healthy: {self.log_path}")

    def stop(self, timeout_s: float = 15.0) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            if os.name == "nt":
                with contextlib.suppress(Exception):
                    subprocess.run(
                        ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                        capture_output=True,
                        text=True,
                    )
            else:
                with contextlib.suppress(Exception):
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            try:
                self.exit_code = self.proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(Exception):
                    self.proc.kill()
                self.exit_code = self.proc.wait(timeout=5)
        else:
            self.exit_code = self.proc.returncode
        if self.log_handle is not None:
            self.log_handle.flush()
            self.log_handle.close()
            self.log_handle = None

    def log_text(self) -> str:
        if not self.log_path.exists():
            return ""
        return self.log_path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------
async def _request(
    client: httpx.AsyncClient,
    stats: RunStats,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    stats.setup.attempts += 1
    started = time.perf_counter()
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        stats.setup.timeouts += 1
        raise SetupError(f"{method} {url} timed out") from exc
    except httpx.TransportError as exc:
        stats.setup.transport_errors += 1
        raise SetupError(f"{method} {url} transport error: {exc}") from exc
    stats.latencies["setup"].add((time.perf_counter() - started) * 1000)
    if response.status_code >= 400:
        stats.setup.http_4xx += 400 <= response.status_code < 500
        stats.setup.http_5xx += response.status_code >= 500
        body = response.text[:1000]
        raise SetupError(
            f"{method} {url} -> HTTP {response.status_code}: {body}"
        )
    stats.setup.success += 1
    return response


async def _create_workspace(
    client: httpx.AsyncClient,
    api: str,
    admin_key: str,
    repo_path: Path,
    label: str,
    stats: RunStats,
) -> str:
    response = await _request(
        client,
        stats,
        "POST",
        f"{api}/v1/admin/workspaces",
        headers={"X-Admin-Key": admin_key},
        json={
            "name": f"bench-{label}",
            "repo_path": str(repo_path),
            "main_branch": "main",
        },
    )
    return response.json()["workspace_id"]


async def _create_tokens(
    client: httpx.AsyncClient,
    api: str,
    admin_key: str,
    count: int,
    label: str,
    stats: RunStats,
    concurrency: int,
) -> list[RunnerIdentity]:
    identities: list[RunnerIdentity] = []
    semaphore = asyncio.Semaphore(max(1, concurrency))
    lock = asyncio.Lock()

    async def create(index: int) -> None:
        async with semaphore:
            response = await _request(
                client,
                stats,
                "POST",
                f"{api}/v1/admin/tokens",
                headers={"X-Admin-Key": admin_key},
                json={"label": f"{label}-r{index:04d}"},
            )
            payload = response.json()
            identity = RunnerIdentity(
                index=index,
                label=f"{label}-r{index:04d}",
                worker_hub_id=payload["worker_hub_id"],
                token=payload["token"],
            )
            async with lock:
                identities.append(identity)

    await asyncio.gather(*(create(index) for index in range(count)))
    identities.sort(key=lambda item: item.index)
    if len(identities) != count:
        raise SetupError(f"expected {count} tokens, got {len(identities)}")
    return identities


async def _seed_artifacts_and_tasks(
    client: httpx.AsyncClient,
    api: str,
    admin_key: str,
    workspace_id: str,
    specs: list[TaskSpec],
    stats: RunStats,
    concurrency: int,
    task_data: dict[str, TaskSpec],
) -> None:
    headers = {"X-Admin-Key": admin_key}
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def create_artifact(symbol: str, kind: str) -> None:
        async with semaphore:
            await _request(
                client,
                stats,
                "POST",
                f"{api}/v1/admin/artifacts",
                headers=headers,
                json={
                    "workspace_id": workspace_id,
                    "symbol_path": symbol,
                    "kind": kind,
                    "status": "FROZEN",
                },
            )

    async def create_task(spec: TaskSpec) -> tuple[str, TaskSpec]:
        async with semaphore:
            response = await _request(
                client,
                stats,
                "POST",
                f"{api}/v1/admin/tasks",
                headers=headers,
                json={
                    "workspace_id": workspace_id,
                    "title": f"Implement {spec.symbol}",
                    "spec_prompt": f"Implement {spec.symbol}; the frozen test must pass.",
                    "type": "IMPLEMENT_BODY",
                    "target_symbols": [spec.symbol],
                    "test_symbols": [spec.test_symbol],
                    "write_set_symbols": [spec.symbol],
                    "max_retries": 3,
                },
            )
            return response.json()["task_id"], spec

    # Artifacts first: the FROZEN status must exist before tasks are created.
    await asyncio.gather(
        *(
            create_artifact(spec.symbol, "stub") for spec in specs
        ),
        *(create_artifact(spec.test_symbol, "test") for spec in specs),
    )
    created = await asyncio.gather(*(create_task(spec) for spec in specs))
    for task_id, spec in created:
        task_data[str(task_id)] = spec
    if len(task_data) != len(specs):
        raise SetupError(
            f"seeded {len(task_data)} tasks but expected {len(specs)}"
        )


# ---------------------------------------------------------------------------
# Load generation
# ---------------------------------------------------------------------------
def _retry_after_seconds(response: httpx.Response) -> float:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return max(0.1, float(header))
        except ValueError:
            pass
    try:
        detail = response.json().get("detail") or {}
        if isinstance(detail, dict) and detail.get("retry_after_seconds"):
            return max(0.1, float(detail["retry_after_seconds"]))
    except Exception:  # noqa: BLE001
        pass
    return 5.0


async def _stamp_wire_sent_at(request: httpx.Request) -> None:
    """Stamp just before httpx writes the request (not in the worker coroutine)."""
    request.headers["X-Bench-Sent-At"] = f"{time.time() * 1000.0:.3f}"


async def _claim_once(
    ctx: "Harness", identity: RunnerIdentity
) -> tuple[dict[str, Any] | None, str]:
    stats = ctx.stats
    stats.claim.attempts += 1
    started = time.perf_counter()
    api = ctx.claim_apis[identity.index % len(ctx.claim_apis)]
    try:
        response = await ctx.worker_client.post(
            f"{api}/v1/syscalls/pull_task_bundle",
            headers={
                "Authorization": f"Bearer {identity.token}",
            },
            json={
                "worker_hub_id": identity.worker_hub_id,
                "supported_task_types": ["IMPLEMENT_BODY"],
                "max_context_tokens": 6000,
                "wait_seconds": 12.0,
            },
            timeout=httpx.Timeout(
                connect=10.0, read=ctx.config.claim_timeout_s, write=10.0, pool=10.0
            ),
        )
    except httpx.TimeoutException as exc:
        stats.claim.timeouts += 1
        stats.sample_error("claim_timeout", None, type(exc).__name__)
        return None, "timeout"
    except httpx.TransportError as exc:
        stats.claim.transport_errors += 1
        stats.sample_error("claim_transport", None, str(exc))
        return None, "transport"
    except Exception as exc:  # noqa: BLE001
        stats.claim.other_errors += 1
        stats.sample_error("claim_exception", None, repr(exc))
        return None, "error"

    stats.latencies["claim"].add((time.perf_counter() - started) * 1000)
    server_ms = response.headers.get("X-Response-Time-Ms")
    if server_ms:
        with contextlib.suppress(ValueError):
            stats.latencies["claim_server"].add(float(server_ms))
    if response.status_code == 429:
        # Backpressure is an expected protocol outcome, not a claim error.
        stats.claim.admission_denied += 1
        ctx.admission_retry_s = _retry_after_seconds(response)
        return None, "admission"
    if response.status_code == 200:
        try:
            bundle = response.json()
        except Exception as exc:  # noqa: BLE001
            stats.claim.other_errors += 1
            stats.sample_error("claim_bad_json", response.status_code, str(exc))
            return None, "error"
        if bundle.get("admission_denied"):
            stats.claim.admission_denied += 1
            ctx.admission_retry_s = max(
                0.1, float(bundle.get("retry_after_seconds") or 5)
            )
            return None, "admission"
        if not bundle.get("leased"):
            stats.claim.empty += 1
            return None, "empty"
        stats.claim.success += 1
        if not bundle.get("task_id") or not bundle.get("lease_token"):
            stats.fatal("claim response says leased but lacks task_id/lease_token")
            return None, "fatal"
        return bundle, "leased"

    stats.sample_error("claim_http", response.status_code, response.text[:500])
    if 400 <= response.status_code < 500:
        stats.claim.http_4xx += 1
        if response.status_code in (401, 403, 409):
            stats.fatal(
                f"claim authentication/topology error HTTP {response.status_code}"
            )
            return None, "fatal"
    else:
        stats.claim.http_5xx += 1
    return None, "http"


async def _heartbeat_loop(
    ctx: "Harness",
    identity: RunnerIdentity,
    task_id: str,
    lease_token: str,
    stop_event: asyncio.Event,
) -> None:
    stats = ctx.stats
    api = ctx.claim_apis[identity.index % len(ctx.claim_apis)]
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=ctx.config.heartbeat_interval_s
            )
            return
        except asyncio.TimeoutError:
            pass
        stats.heartbeat.attempts += 1
        started = time.perf_counter()
        try:
            response = await ctx.worker_client.post(
                f"{api}/v1/runner/heartbeat",
                headers={"Authorization": f"Bearer {identity.token}"},
                json={
                    "task_id": task_id,
                    "lease_token": lease_token,
                    "extend_seconds": 300,
                },
                timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
            )
        except httpx.TimeoutException:
            stats.heartbeat.timeouts += 1
            continue
        except httpx.TransportError:
            stats.heartbeat.transport_errors += 1
            continue
        except Exception:
            stats.heartbeat.other_errors += 1
            continue
        stats.latencies["heartbeat"].add((time.perf_counter() - started) * 1000)
        if response.status_code == 200:
            stats.heartbeat.success += 1
        elif response.status_code == 409:
            # The task moved to VERIFYING/terminal; benign and expected.
            stats.heartbeat.not_leased += 1
        else:
            stats.sample_error("heartbeat_http", response.status_code, response.text[:300])
            if 400 <= response.status_code < 500:
                stats.heartbeat.http_4xx += 1
            else:
                stats.heartbeat.http_5xx += 1


async def _release(
    ctx: "Harness",
    identity: RunnerIdentity,
    task_id: str,
    lease_token: str,
    reason: str,
    retryable: bool,
) -> bool:
    stats = ctx.stats
    stats.release.attempts += 1
    api = ctx.claim_apis[identity.index % len(ctx.claim_apis)]
    try:
        response = await ctx.worker_client.post(
            f"{api}/v1/runner/release",
            headers={"Authorization": f"Bearer {identity.token}"},
            json={
                "task_id": task_id,
                "lease_token": lease_token,
                "reason": reason[:4000],
                "retryable": retryable,
            },
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        )
    except httpx.TimeoutException:
        stats.release.timeouts += 1
        return False
    except httpx.TransportError:
        stats.release.transport_errors += 1
        return False
    except Exception:
        stats.release.other_errors += 1
        return False
    if response.status_code == 200:
        stats.release.success += 1
        try:
            payload = response.json()
        except Exception:
            payload = {}
        released = payload.get("released") is not False
        if not released:
            stats.release.not_leased += 1
        return released
    stats.sample_error("release_http", response.status_code, response.text[:300])
    if response.status_code in (403, 409):
        stats.release.not_leased += 1
    elif 400 <= response.status_code < 500:
        stats.release.http_4xx += 1
    else:
        stats.release.http_5xx += 1
    return False


def _record_merge_response(stats: RunStats, payload: Mapping[str, Any]) -> None:
    status = str(payload.get("new_status", "UNKNOWN"))
    stats.client_statuses[status] += 1
    mode = payload.get("merge_mode")
    if mode:
        stats.merge_modes[str(mode)] += 1
    for key, value in (payload.get("merge_timing") or {}).items():
        with contextlib.suppress(TypeError, ValueError):
            stats.client_merge_timing_ms[str(key)].add(float(value))
    for gate in payload.get("gate_results") or []:
        stage = str(gate.get("stage", "UNKNOWN"))
        with contextlib.suppress(TypeError, ValueError):
            stats.client_gate_stage_ms[stage].add(float(gate.get("duration_ms", 0)))


def _schedule_reconcile(
    ctx: "Harness",
    identity: RunnerIdentity,
    task_id: str,
    lease_token: str,
    reason: str,
) -> None:
    if task_id in ctx.stats.reconciling:
        return

    async def reconcile() -> None:
        stats = ctx.stats
        attempted_release = False
        while not ctx.stop_all_event.is_set():
            if not ctx.drain_deadline or time.monotonic() > ctx.drain_deadline:
                break
            try:
                row = await ctx.probe.task_status(task_id)
            except Exception as exc:  # noqa: BLE001
                stats.reconciled["probe_error"] += 1
                stats.sample_error("reconcile_probe", None, str(exc))
                await asyncio.sleep(ctx.config.reconcile_interval_s)
                continue
            if row is None:
                stats.reconciled["missing"] += 1
                return
            status = row["status"]
            if status in TERMINAL_STATUSES:
                stats.reconciled[status] += 1
                if status == "COMMITTED":
                    stats.reconciled["late_commit"] += 1
                return
            if status == "READY":
                stats.reconciled["ready"] += 1
                return
            if status == "LEASED" and not attempted_release:
                # A gate rejection can leave the task LEASED even though the
                # client timed out.  Release it so another worker can retry.
                attempted_release = True
                if await _release(
                    ctx,
                    identity,
                    task_id,
                    lease_token,
                    f"{reason}; harness reconcile release",
                    retryable=False,
                ):
                    stats.reconciled["released"] += 1
                    continue
            await asyncio.sleep(ctx.config.reconcile_interval_s)
        stats.reconciled["unresolved"] += 1

    task = asyncio.create_task(reconcile(), name=f"reconcile-{task_id}")
    ctx.stats.reconciling[task_id] = task

    def done(_: asyncio.Task) -> None:
        ctx.stats.reconciling.pop(task_id, None)

    task.add_done_callback(done)


async def _submit_task(
    ctx: "Harness",
    identity: RunnerIdentity,
    bundle: Mapping[str, Any],
    spec: TaskSpec,
) -> None:
    stats = ctx.stats
    task_id = str(bundle["task_id"])
    lease_token = str(bundle["lease_token"])
    stats.in_flight[task_id] = {"identity": identity, "lease_token": lease_token}
    try:
        await _submit_attempt(ctx, identity, task_id, lease_token, spec)
    finally:
        stats.in_flight.pop(task_id, None)


def _submission_key(task_id: str, lease_token: str, diff: str) -> str:
    return hashlib.sha256(
        f"{task_id}:{lease_token}:{diff}".encode("utf-8")
    ).hexdigest()


def _apply_submission_final(
    stats: RunStats, task_id: str, payload: Mapping[str, Any]
) -> str:
    final_status = str(payload.get("status", "FAILED"))
    task_status = str(
        payload.get("task_status")
        or ("COMMITTED" if final_status == "COMMITTED" else "FAILED")
    )
    stats.client_outcomes[task_id] = final_status
    _record_merge_response(
        stats,
        {
            "new_status": task_status,
            "merge_mode": payload.get("merge_mode"),
            "gate_results": payload.get("gate_results") or [],
            "merge_timing": payload.get("merge_timing") or {},
        },
    )
    if task_status == "COMMITTED":
        stats.client_committed_ids.add(task_id)
    if task_status == "LEASED":
        stats.submit.gate_rejected += 1
    if final_status != "COMMITTED":
        stats.sample_error(
            "submission_failed",
            None,
            f"{payload.get('error_code') or 'unknown'}: "
            f"{(payload.get('error_detail') or '')[:400]}",
        )
    return task_status


async def _poll_submission(
    ctx: "Harness",
    identity: RunnerIdentity,
    task_id: str,
    lease_token: str,
    accepted: Mapping[str, Any],
    started: float,
) -> None:
    stats = ctx.stats
    if accepted.get("final"):
        task_status = _apply_submission_final(stats, task_id, accepted)
        stats.latencies["submission_final"].add(
            (time.perf_counter() - started) * 1000
        )
        if task_status == "LEASED":
            await _release(
                ctx,
                identity,
                task_id,
                lease_token,
                "async submission gate rejected the patch",
                retryable=False,
            )
        return

    status_url = str(accepted.get("status_url") or "")
    if not status_url:
        stats.fatal(f"submission {accepted.get('submission_id')} has no status_url")
        return
    url = (
        status_url
        if status_url.startswith("http")
        else f"{ctx.submit_apis[identity.index % len(ctx.submit_apis)]}{status_url}"
    )
    deadline = started + ctx.config.submit_timeout_s
    delay = 0.25
    while time.monotonic() < deadline and not ctx.stop_claim_event.is_set():
        try:
            response = await ctx.worker_client.get(
                url,
                headers={"Authorization": f"Bearer {identity.token}"},
                timeout=httpx.Timeout(
                    connect=10.0, read=30.0, write=10.0, pool=10.0
                ),
            )
        except httpx.TimeoutException:
            stats.submission_wait_errors += 1
            await asyncio.sleep(delay)
            delay = min(2.0, delay * 1.5)
            continue
        except httpx.TransportError:
            stats.submission_wait_errors += 1
            await asyncio.sleep(delay)
            delay = min(2.0, delay * 1.5)
            continue
        if response.status_code == 200:
            try:
                payload = response.json()
            except Exception:
                stats.submission_wait_errors += 1
                await asyncio.sleep(delay)
                continue
            if payload.get("final"):
                stats.latencies["submission_final"].add(
                    (time.perf_counter() - started) * 1000
                )
                task_status = _apply_submission_final(stats, task_id, payload)
                if task_status == "LEASED":
                    await _release(
                        ctx,
                        identity,
                        task_id,
                        lease_token,
                        "async submission gate rejected the patch",
                        retryable=False,
                    )
                return
            await asyncio.sleep(delay)
            delay = min(2.0, delay * 1.5)
            continue
        if response.status_code == 404:
            stats.submission_wait_errors += 1
            await asyncio.sleep(delay)
            continue
        stats.submission_wait_errors += 1
        stats.sample_error(
            "submission_status_http", response.status_code, response.text[:300]
        )
        await asyncio.sleep(delay)
        delay = min(2.0, delay * 1.5)

    if ctx.stop_claim_event.is_set():
        # One last non-blocking read: the DB may have reached a terminal task
        # state (which also stops the monitor) while this poller was sleeping.
        try:
            response = await ctx.worker_client.get(
                url,
                headers={"Authorization": f"Bearer {identity.token}"},
                timeout=httpx.Timeout(
                    connect=5.0, read=5.0, write=5.0, pool=5.0
                ),
            )
            if response.status_code == 200:
                payload = response.json()
                if payload.get("final"):
                    stats.latencies["submission_final"].add(
                        (time.perf_counter() - started) * 1000
                    )
                    task_status = _apply_submission_final(stats, task_id, payload)
                    if task_status == "LEASED":
                        await _release(
                            ctx,
                            identity,
                            task_id,
                            lease_token,
                            "async submission gate rejected the patch",
                            retryable=False,
                        )
                    return
        except Exception:
            pass
        stats.client_outcomes[task_id] = "ACCEPTED_NOT_FINAL_AT_CUTOFF"
        stats.sample_error(
            "submission_cutoff",
            None,
            f"submission {accepted.get('submission_id')} was still non-final at cutoff",
        )
    else:
        stats.submission_wait_timeouts += 1
        stats.client_outcomes[task_id] = "FINAL_TIMEOUT"
        stats.sample_error(
            "submission_wait_timeout",
            None,
            f"submission {accepted.get('submission_id')} did not finalize in time",
        )
    # The durable submission worker owns the lease from 202 onward.  The
    # benchmark must not release it here: doing so races the worker and turns
    # a queued submission into lease_lost.


async def _submit_attempt(
    ctx: "Harness",
    identity: RunnerIdentity,
    task_id: str,
    lease_token: str,
    spec: TaskSpec,
) -> None:
    stats = ctx.stats
    idempotency_key = _submission_key(task_id, lease_token, spec.diff)
    body = {
        "task_id": task_id,
        "lease_token": lease_token,
        "git_diff": spec.diff,
        "touched_symbols": [spec.symbol],
        "local_test_passed": True,
    }
    headers = {
        "Authorization": f"Bearer {identity.token}",
        "Idempotency-Key": idempotency_key,
    }
    accept_timeout = httpx.Timeout(
        connect=10.0,
        read=ctx.config.submit_accept_timeout_s,
        write=30.0,
        pool=10.0,
    )
    api = ctx.submit_apis[identity.index % len(ctx.submit_apis)]
    attempts = 0
    while attempts < 6 and not ctx.stop_claim_event.is_set():
        attempts += 1
        stats.submit.attempts += 1
        started = time.perf_counter()
        try:
            response = await ctx.submit_client.post(
                f"{api}/v2/syscalls/submit_patch",
                headers=headers,
                json=body,
                timeout=accept_timeout,
            )
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout) as exc:
            if isinstance(exc, httpx.ConnectError):
                stats.submit.transport_errors += 1
            else:
                stats.submit.timeouts += 1
            # The request may have been persisted; look up the key first.
            try:
                lookup = await ctx.submit_client.get(
                    f"{api}/v2/submissions/find",
                    params={"idempotency_key": idempotency_key},
                    headers={"Authorization": f"Bearer {identity.token}"},
                    timeout=httpx.Timeout(
                        connect=10.0, read=30.0, write=10.0, pool=10.0
                    ),
                )
            except httpx.HTTPError:
                lookup = None
            if lookup is not None and lookup.status_code == 200:
                stats.submit.success += 1
                stats.accepted_task_ids.add(task_id)
                await _poll_submission(
                    ctx, identity, task_id, lease_token, lookup.json(), started
                )
                return
            if attempts < 6:
                stats.submit.retries += 1
                await asyncio.sleep(min(2.0, 0.1 * (2**attempts)))
                continue
            stats.client_outcomes[task_id] = "CONNECT_ERROR"
            stats.sample_error("submit_connect", None, str(exc))
            return
        except httpx.TimeoutException as exc:
            stats.submit.timeouts += 1
            if attempts < 6:
                stats.submit.retries += 1
                await asyncio.sleep(min(2.0, 0.1 * (2**attempts)))
                continue
            stats.client_outcomes[task_id] = "TIMEOUT"
            stats.sample_error("submit_timeout", None, type(exc).__name__)
            return
        except httpx.TransportError as exc:
            stats.submit.transport_errors += 1
            if attempts < 6:
                stats.submit.retries += 1
                await asyncio.sleep(min(2.0, 0.1 * (2**attempts)))
                continue
            stats.client_outcomes[task_id] = "TRANSPORT_ERROR"
            stats.sample_error("submit_transport", None, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            stats.submit.other_errors += 1
            stats.client_outcomes[task_id] = "ERROR"
            stats.sample_error("submit_exception", None, repr(exc))
            return

        stats.latencies["submit_accept"].add((time.perf_counter() - started) * 1000)
        server_ms = response.headers.get("X-Response-Time-Ms")
        if server_ms:
            with contextlib.suppress(ValueError):
                stats.latencies["submit_accept_server"].add(float(server_ms))
        if response.status_code in (200, 202):
            stats.submit.success += 1
            stats.accepted_task_ids.add(task_id)
            try:
                accepted = response.json()
            except Exception as exc:  # noqa: BLE001
                stats.submit.other_errors += 1
                stats.client_outcomes[task_id] = "BAD_JSON"
                stats.sample_error("submit_bad_json", response.status_code, str(exc))
                return
            await _poll_submission(
                ctx, identity, task_id, lease_token, accepted, started
            )
            return

        body_text = response.text[:500]
        stats.sample_error("submit_http", response.status_code, body_text)
        if 400 <= response.status_code < 500:
            stats.submit.http_4xx += 1
        else:
            stats.submit.http_5xx += 1
        stats.client_outcomes[task_id] = f"HTTP_{response.status_code}"
        if response.status_code in {401, 403, 404, 409, 410, 422}:
            # 409 may refer to an already persisted submission; never release
            # a lease owned by that durable row.
            return
        if attempts < 6:
            stats.submit.retries += 1
            await asyncio.sleep(min(2.0, 0.1 * (2**attempts)))
            continue
        return

    stats.submit.timeouts += 1
    stats.client_outcomes[task_id] = "ACCEPT_RETRY_EXHAUSTED"


async def _worker_loop(ctx: "Harness", identity: RunnerIdentity) -> None:
    try:
        await _worker_loop_inner(ctx, identity)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        ctx.stats.fatal(f"worker {identity.label} crashed: {exc!r}")
        ctx.stats.sample_error("worker_exception", None, repr(exc))


async def _worker_loop_inner(ctx: "Harness", identity: RunnerIdentity) -> None:
    stats = ctx.stats
    empty_streak = 0
    while not ctx.stop_claim_event.is_set():
        if (
            ctx.config.mode != "claim"
            and len(stats.accepted_task_ids) >= ctx.config.expected_tasks
        ):
            # Every task in this finite batch has been accepted; further
            # pulls can only return empty and would compete with status polls.
            break
        bundle, reason = await _claim_once(ctx, identity)
        if bundle is None:
            if reason == "fatal":
                return
            if reason == "admission":
                empty_streak = 0
                await asyncio.sleep(ctx.admission_retry_s * random.uniform(0.8, 1.2))
            elif reason == "empty":
                empty_streak += 1
                await asyncio.sleep(
                    min(
                        ctx.config.empty_claim_backoff_s,
                        0.1 * empty_streak,
                    )
                    * random.uniform(0.8, 1.2)
                )
            else:
                stats.claim.retries += 1
                empty_streak = 0
                await asyncio.sleep(min(2.0, 0.1 * (2 ** min(4, stats.claim.retries % 5))) * random.uniform(0.7, 1.3))
            continue
        empty_streak = 0
        task_id = str(bundle["task_id"])
        spec = ctx.task_specs.get(task_id)
        if spec is None:
            stats.fatal(f"server leased unknown task {task_id}")
            return
        lease_token = str(bundle["lease_token"])
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(
                ctx, identity, task_id, lease_token, heartbeat_stop
            ),
            name=f"heartbeat-{task_id}",
        )
        try:
            if ctx.config.mode == "claim":
                # Claim-only probe: hold the lease (and heartbeat it) until the
                # load window ends, then release.  This measures unique claim
                # issuance without gate/submit work.
                await ctx.stop_claim_event.wait()
                released = await _release(
                    ctx,
                    identity,
                    task_id,
                    lease_token,
                    "claim-only benchmark cutoff",
                    retryable=False,
                )
                if not released:
                    _schedule_reconcile(
                        ctx,
                        identity,
                        task_id,
                        lease_token,
                        "claim-only cutoff release did not complete",
                    )
                break
            think = random.uniform(ctx.config.think_min_s, ctx.config.think_max_s)
            await asyncio.sleep(think)
            # submit_patch transitions the task to VERIFYING before the gate
            # wait, so heartbeats after this point can only return 409.  Keep
            # heartbeats on the LEASED "thinking" phase only.
            heartbeat_stop.set()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            if ctx.stop_claim_event.is_set():
                released = await _release(
                    ctx,
                    identity,
                    task_id,
                    lease_token,
                    "benchmark claim cutoff before submit",
                    retryable=False,
                )
                if not released:
                    _schedule_reconcile(
                        ctx,
                        identity,
                        task_id,
                        lease_token,
                        "claim cutoff release did not complete",
                    )
                break
            await _submit_task(ctx, identity, bundle, spec)
        finally:
            heartbeat_stop.set()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task


async def _sse_stream(
    ctx: "Harness", identity: RunnerIdentity, stream_index: int
) -> None:
    stats = ctx.stats
    api = ctx.claim_apis[stream_index % len(ctx.claim_apis)]
    headers = {"Authorization": f"Bearer {identity.token}"}
    opened_once = False
    attempt = 0
    while not ctx.stop_all_event.is_set():
        attempt += 1
        stats.sse.attempts += 1
        if attempt > 1:
            stats.sse.reconnects += 1
        started = time.perf_counter()
        try:
            timeout = httpx.Timeout(
                connect=10.0, read=ctx.config.sse_timeout_s, write=10.0, pool=10.0
            )
            async with ctx.sse_client.stream(
                "GET",
                f"{api}/v1/events/stream",
                headers=headers,
                timeout=timeout,
            ) as response:
                if response.status_code != 200:
                    stats.sse.http_errors += 1
                    stats.sample_error(
                        "sse_http", response.status_code, response.text[:200]
                    )
                    await asyncio.sleep(0.5)
                    continue
                stats.sse.success += 1
                stats.sse.opens += 1
                if not opened_once:
                    opened_once = True
                    stats.sse.streams_opened_once += 1
                first = True
                async for line in response.aiter_lines():
                    if first:
                        stats.latencies["sse_ttfb"].add(
                            (time.perf_counter() - started) * 1000
                        )
                        first = False
                    stats.sse.bytes += len(line.encode("utf-8"))
                    if line.startswith("event:"):
                        stats.sse.frames += 1
                stats.sse.empty += 1
        except httpx.TimeoutException:
            stats.sse.timeouts += 1
            stats.sample_error("sse_timeout", None, "read timeout")
        except httpx.TransportError as exc:
            stats.sse.transport_errors += 1
            stats.sample_error("sse_transport", None, str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            stats.sse.other_errors += 1
            stats.sample_error("sse_exception", None, repr(exc))
        if not ctx.stop_all_event.is_set():
            await asyncio.sleep(min(2.0, 0.2 * attempt))


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
class Harness:
    def __init__(
        self,
        config: RunConfig,
        api: str,
        apis: list[str] | None,
        claim_apis: list[str] | None,
        submit_apis: list[str] | None,
        workspace_id: str,
        task_specs: dict[str, TaskSpec],
        identities: list[RunnerIdentity],
        stats: RunStats,
        probe: DbProbe,
        worker_client: httpx.AsyncClient,
        submit_client: httpx.AsyncClient | None,
        sse_client: httpx.AsyncClient,
        admin_client: httpx.AsyncClient,
        admin_key: str,
        worker_metrics_url: str,
        resources: ResourceSampler | None = None,
    ) -> None:
        self.config = config
        self.api = api
        self.apis = apis or [api]
        self.claim_apis = claim_apis or self.apis
        self.submit_apis = submit_apis or self.apis
        self.workspace_id = workspace_id
        self.task_specs = task_specs
        self.identities = identities
        self.stats = stats
        self.probe = probe
        self.worker_client = worker_client
        self.submit_client = submit_client or worker_client
        self.sse_client = sse_client
        self.admin_client = admin_client
        self.admin_key = admin_key
        self.worker_metrics_url = worker_metrics_url
        self.resources = resources
        self.load_started = 0.0
        self.cutoff_at = 0.0
        self.drained_at = 0.0
        self.drain_deadline: float | None = None
        self.stop_claim_event = asyncio.Event()
        self.drain_event = asyncio.Event()
        self.stop_all_event = asyncio.Event()
        self.cutoff_reason = "unknown"
        self.admission_retry_s = 5.0
        self.server_metrics_available = False
        self.server_metrics_final: dict[str, Any] | None = None
        self.worker_metrics_final: dict[str, Any] | None = None
        self.worker_metrics_available = False
        self.metrics_reset_at: str | None = None
        self.metrics_collector_stop = asyncio.Event()

    # ----------------------------------------------------------- server metrics
    async def reset_server_metrics(self) -> None:
        reset_at = None
        for api in self.apis:
            try:
                response = await self.admin_client.post(
                    f"{api}/v1/admin/metrics/reset",
                    headers={"X-Admin-Key": self.admin_key},
                    timeout=10.0,
                )
            except Exception:
                continue
            if response.status_code == 200:
                self.server_metrics_available = True
                with contextlib.suppress(Exception):
                    reset_at = response.json().get("reset_at")
        if reset_at is not None:
            self.metrics_reset_at = reset_at
        with contextlib.suppress(Exception):
            await self.admin_client.post(
                f"{self.worker_metrics_url}/metrics/reset", timeout=10.0
            )

    async def fetch_worker_metrics(self) -> dict[str, Any] | None:
        try:
            response = await self.admin_client.get(
                f"{self.worker_metrics_url}/metrics", timeout=10.0
            )
        except Exception:
            return None
        if response.status_code != 200:
            return None
        self.worker_metrics_available = True
        return response.json()

    async def fetch_server_metrics(self) -> dict[str, Any] | None:
        snapshots: list[dict[str, Any]] = []
        for api in self.apis:
            try:
                response = await self.admin_client.get(
                    f"{api}/v1/admin/metrics",
                    headers={"X-Admin-Key": self.admin_key},
                    timeout=10.0,
                )
            except Exception:
                continue
            if response.status_code == 200:
                snapshots.append(response.json())
        if not snapshots:
            return None
        self.server_metrics_available = True
        primary = snapshots[0]
        if len(snapshots) == 1:
            return primary
        counters: dict[str, int] = {}
        for snapshot in snapshots:
            for name, value in (snapshot.get("counters") or {}).items():
                if isinstance(value, int):
                    counters[name] = counters.get(name, 0) + value
        histograms: dict[str, Any] = {}
        for snapshot in snapshots:
            for name, value in (snapshot.get("histograms_ms") or {}).items():
                if name not in histograms:
                    histograms[name] = value
        return {
            **primary,
            "counters": counters,
            "histograms_ms": histograms,
            "per_instance": snapshots,
            "api_instances": len(snapshots),
        }

    async def collect_metrics(self) -> None:
        while not self.metrics_collector_stop.is_set():
            snapshot = await self.fetch_server_metrics()
            if snapshot is not None:
                pool = snapshot.get("db_pool") or {}
                samples = self.stats.metrics_samples
                if len(samples) < 5_000:
                    samples.append(
                        {
                            "elapsed_s": round(
                                time.monotonic() - self.load_started, 3
                            ),
                            "checkedout": pool.get("checkedout"),
                            "overflow": pool.get("overflow"),
                            "wait_count": (pool.get("wait_ms") or {}).get("count"),
                            "wait_p95_ms": (pool.get("wait_ms") or {}).get("p95"),
                            "submit_finished": (
                                snapshot.get("counters") or {}
                            ).get("submit.finished"),
                            "sse_active": (snapshot.get("gauges") or {}).get(
                                "sse.active"
                            ),
                        }
                    )
                    self.stats.peak_checkedout = max(
                        self.stats.peak_checkedout,
                        int(pool.get("checkedout") or 0),
                    )
            try:
                await asyncio.wait_for(
                    self.metrics_collector_stop.wait(),
                    timeout=self.config.metrics_interval_s,
                )
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ monitor
    async def monitor(self) -> None:
        expected = set(self.task_specs)
        while not self.stop_all_event.is_set():
            try:
                snapshot = await self.probe.snapshot(self.workspace_id, expected)
            except Exception as exc:  # noqa: BLE001
                self.stats.monitor_errors += 1
                self.stats.sample_error("db_probe", None, str(exc))
                await asyncio.sleep(self.config.sample_interval_s)
                continue
            elapsed = time.monotonic() - self.load_started
            if self.stats.initial_queue is None:
                self.stats.initial_queue = snapshot
            if self.resources is not None:
                self.resources.sample()
            self.stats.record_queue(snapshot, elapsed)
            # Drain on the actual seeded topology, not the asserted expected
            # count.  An --expected-tasks mismatch must fail the verdict, not
            # make the harness wait for a task that was never created.
            if (
                snapshot["missing_tasks"] == 0
                and snapshot["extra_tasks"] == 0
                and snapshot["non_terminal"] == 0
            ):
                self.drain_event.set()
            try:
                await asyncio.wait_for(
                    self.stop_all_event.wait(), timeout=self.config.sample_interval_s
                )
            except asyncio.TimeoutError:
                pass

    async def cutoff(self) -> None:
        try:
            await asyncio.wait_for(
                self.drain_event.wait(), timeout=self.config.max_duration_s
            )
            self.cutoff_reason = "drained"
        except asyncio.TimeoutError:
            self.cutoff_reason = "max_duration"
        self.cutoff_at = time.monotonic()
        self.stop_claim_event.set()

    async def wait_for_drain(self) -> None:
        deadline = time.monotonic() + self.config.drain_timeout_s
        if not self.drain_deadline:
            self.drain_deadline = deadline
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.drain_event.wait(), timeout=self.config.drain_timeout_s)
        reconcile_tasks = list(self.stats.reconciling.values())
        if reconcile_tasks:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining > 0:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.gather(*reconcile_tasks, return_exceptions=True),
                        timeout=remaining,
                    )

    async def start_sse(self) -> list[asyncio.Task]:
        tasks: list[asyncio.Task] = []
        for index in range(self.config.sse_streams):
            identity = self.identities[
                self.config.runners + (index % max(1, self.config.sse_streams))
            ]
            tasks.append(
                asyncio.create_task(
                    _sse_stream(self, identity, index), name=f"sse-{index}"
                )
            )
        if tasks:
            deadline = time.monotonic() + self.config.sse_warmup_s
            while time.monotonic() < deadline:
                opened = self.stats.sse.streams_opened_once
                if opened >= self.config.sse_streams:
                    break
                await asyncio.sleep(0.05)
        return tasks

    async def _run_load_sharded(self) -> None:
        shards = max(1, min(self.config.client_shards, self.config.runners))
        stop_flag = mp.Event()
        manager = mp.Manager()
        result_queue = manager.Queue()
        processes: list[mp.Process] = []
        for shard in range(shards):
            start = shard * self.config.runners // shards
            end = (shard + 1) * self.config.runners // shards
            sse_start = shard * self.config.sse_streams // shards
            sse_end = (shard + 1) * self.config.sse_streams // shards
            process = mp.Process(
                target=_shard_entry,
                args=(
                    self.config,
                    self.identities[start:end],
                    self.identities[self.config.runners + sse_start : self.config.runners + sse_end],
                    list(range(sse_start, sse_end)),
                    self.task_specs,
                    self.claim_apis,
                    self.submit_apis,
                    stop_flag,
                    result_queue,
                ),
                name=f"bench-shard-{shard}",
            )
            process.start()
            processes.append(process)

        self.load_started = time.monotonic()
        self.drain_deadline = (
            self.load_started
            + self.config.max_duration_s
            + self.config.drain_timeout_s
        )
        monitor_task = asyncio.create_task(self.monitor(), name="monitor")
        metrics_task = asyncio.create_task(self.collect_metrics(), name="metrics")
        cutoff_task = asyncio.create_task(self.cutoff(), name="cutoff")
        try:
            await cutoff_task
            stop_flag.set()
            deadline = time.monotonic() + min(
                90.0, self.config.submit_timeout_s + 10.0
            )
            for process in processes:
                remaining = max(0.1, deadline - time.monotonic())
                await asyncio.to_thread(process.join, remaining)
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    await asyncio.to_thread(process.join, 5.0)
            for _ in processes:
                try:
                    result = result_queue.get(timeout=30.0)
                except Exception:
                    break
                _merge_shard_result(self.stats, result)
            await self.wait_for_drain()
        finally:
            stop_flag.set()
            self.stop_all_event.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
            self.metrics_collector_stop.set()
            with contextlib.suppress(asyncio.CancelledError):
                await metrics_task
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
            self.drained_at = time.monotonic()
            self.stats.metrics_samples = self.stats.metrics_samples[:5_000]
            manager.shutdown()

    async def run_load(self) -> None:
        if self.config.client_shards > 1:
            await self._run_load_sharded()
            return
        sse_tasks = await self.start_sse()
        # Warmup must not inflate the acceptance throughput denominator.
        self.load_started = time.monotonic()
        self.drain_deadline = (
            self.load_started
            + self.config.max_duration_s
            + self.config.drain_timeout_s
        )
        monitor_task = asyncio.create_task(self.monitor(), name="monitor")
        metrics_task = asyncio.create_task(self.collect_metrics(), name="metrics")
        cutoff_task = asyncio.create_task(self.cutoff(), name="cutoff")
        workers = [
            asyncio.create_task(
                _worker_loop(self, self.identities[index]),
                name=f"worker-{index}",
            )
            for index in range(self.config.runners)
        ]
        try:
            await cutoff_task
            # Let in-flight submits finish; they already hold the server gate.
            wait_budget = self.config.submit_timeout_s + 60.0
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*workers, return_exceptions=True),
                    timeout=wait_budget,
                )
            # Anything still in flight is unexpected; cancel it and reconcile.
            stubborn = [task for task in workers if not task.done()]
            inflight_snapshot = list(self.stats.in_flight.items())
            for task in stubborn:
                task.cancel()
            if stubborn:
                await asyncio.gather(*stubborn, return_exceptions=True)
            # Do not release an in-flight v2 submission: the durable
            # submission worker owns the lease from 202 onward.
            _ = inflight_snapshot
            await self.wait_for_drain()
        finally:
            self.stop_all_event.set()
            for task in sse_tasks:
                task.cancel()
            if sse_tasks:
                await asyncio.gather(*sse_tasks, return_exceptions=True)
            self.metrics_collector_stop.set()
            with contextlib.suppress(asyncio.CancelledError):
                await metrics_task
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
            self.drained_at = time.monotonic()
            for task in list(self.stats.reconciling.values()):
                task.cancel()
            if self.stats.reconciling:
                await asyncio.gather(
                    *list(self.stats.reconciling.values()), return_exceptions=True
                )
            self.stats.metrics_samples = self.stats.metrics_samples[:5_000]


# ---------------------------------------------------------------------------
# Multiprocess client shards
# ---------------------------------------------------------------------------
class _ShardContext:
    """The subset of Harness the worker/SSE functions actually use."""

    def __init__(
        self,
        *,
        config: RunConfig,
        task_specs: dict[str, TaskSpec],
        claim_apis: list[str],
        submit_apis: list[str],
        worker_client: httpx.AsyncClient,
        submit_client: httpx.AsyncClient,
        sse_client: httpx.AsyncClient,
    ) -> None:
        self.config = config
        self.task_specs = task_specs
        self.claim_apis = claim_apis
        self.submit_apis = submit_apis
        self.apis = sorted(set(claim_apis + submit_apis))
        self.api = claim_apis[0]
        self.worker_client = worker_client
        self.submit_client = submit_client
        self.sse_client = sse_client
        self.stats = RunStats(config)
        self.probe = None
        self.stop_claim_event = asyncio.Event()
        self.stop_all_event = asyncio.Event()
        self.drain_event = asyncio.Event()
        self.drain_deadline = 0.0
        self.admission_retry_s = 5.0


def _counter_from_dict(cls, value: Mapping[str, Any]):
    return cls(
        **{
            field.name: int(value.get(field.name, 0) or 0)
            for field in dataclasses.fields(cls)
        }
    )


def _merge_shard_result(stats: RunStats, result: Mapping[str, Any] | None) -> None:
    if not result:
        return
    stats.claim.add(_counter_from_dict(OpCounters, result.get("claim") or {}))
    stats.submit.add(_counter_from_dict(OpCounters, result.get("submit") or {}))
    stats.release.add(_counter_from_dict(OpCounters, result.get("release") or {}))
    stats.heartbeat.add(_counter_from_dict(OpCounters, result.get("heartbeat") or {}))
    stats.sse.add(_counter_from_dict(SseCounters, result.get("sse") or {}))
    for name, values in (result.get("latencies") or {}).items():
        for value in values:
            stats.latencies[name].add(float(value))
    stats.accepted_task_ids.update(result.get("accepted_task_ids") or [])
    stats.client_committed_ids.update(result.get("client_committed_ids") or [])
    stats.client_statuses.update(result.get("client_statuses") or {})
    stats.merge_modes.update(result.get("merge_modes") or {})
    stats.client_outcomes.update(result.get("client_outcomes") or {})
    for name, values in (result.get("client_gate_stage_ms") or {}).items():
        for value in values:
            stats.client_gate_stage_ms[name].add(float(value))
    for name, values in (result.get("client_merge_timing_ms") or {}).items():
        for value in values:
            stats.client_merge_timing_ms[name].add(float(value))
    stats.submission_wait_retries += int(result.get("submission_wait_retries") or 0)
    stats.submission_wait_timeouts += int(result.get("submission_wait_timeouts") or 0)
    stats.submission_wait_errors += int(result.get("submission_wait_errors") or 0)
    stats.fatal_errors.extend(result.get("fatal_errors") or [])
    for sample in result.get("error_samples") or []:
        if len(stats.error_samples) < 25:
            stats.error_samples.append(sample)


def _shard_snapshot(stats: RunStats) -> dict[str, Any]:
    return {
        "claim": dataclasses.asdict(stats.claim),
        "submit": dataclasses.asdict(stats.submit),
        "release": dataclasses.asdict(stats.release),
        "heartbeat": dataclasses.asdict(stats.heartbeat),
        "sse": dataclasses.asdict(stats.sse),
        "latencies": {name: list(values.values) for name, values in stats.latencies.items()},
        "accepted_task_ids": list(stats.accepted_task_ids),
        "client_committed_ids": list(stats.client_committed_ids),
        "client_statuses": dict(stats.client_statuses),
        "merge_modes": dict(stats.merge_modes),
        "client_outcomes": dict(stats.client_outcomes),
        "client_gate_stage_ms": {
            name: list(values.values)
            for name, values in stats.client_gate_stage_ms.items()
        },
        "client_merge_timing_ms": {
            name: list(values.values)
            for name, values in stats.client_merge_timing_ms.items()
        },
        "submission_wait_retries": stats.submission_wait_retries,
        "submission_wait_timeouts": stats.submission_wait_timeouts,
        "submission_wait_errors": stats.submission_wait_errors,
        "fatal_errors": list(stats.fatal_errors),
        "error_samples": list(stats.error_samples),
    }


def _shard_entry(
    config: RunConfig,
    identities: list[RunnerIdentity],
    sse_identities: list[RunnerIdentity],
    sse_indices: list[int],
    task_specs: dict[str, TaskSpec],
    claim_apis: list[str],
    submit_apis: list[str],
    stop_flag: Any,
    result_queue: Any,
) -> None:
    async def main() -> None:
        limits = httpx.Limits(
            max_connections=max(32, len(identities) * 2 + 32),
            max_keepalive_connections=max(16, len(identities) * 2 + 16),
        )
        timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        async with httpx.AsyncClient(
            limits=limits,
            timeout=timeout,
            event_hooks={"request": [_stamp_wire_sent_at]},
        ) as worker_client, httpx.AsyncClient(
            limits=limits,
            timeout=timeout,
            event_hooks={"request": [_stamp_wire_sent_at]},
        ) as submit_client, httpx.AsyncClient(
            limits=limits,
            timeout=None,
        ) as sse_client:
            shard = _ShardContext(
                config=config,
                task_specs=task_specs,
                claim_apis=claim_apis,
                submit_apis=submit_apis,
                worker_client=worker_client,
                submit_client=submit_client,
                sse_client=sse_client,
            )

            async def watch_stop() -> None:
                while not stop_flag.is_set():
                    await asyncio.sleep(0.2)
                shard.stop_claim_event.set()
                shard.stop_all_event.set()

            watcher = asyncio.create_task(watch_stop(), name="shard-stop-watch")
            sse_tasks = [
                asyncio.create_task(
                    _sse_stream(shard, identity, stream_index),
                    name=f"shard-sse-{stream_index}",
                )
                for identity, stream_index in zip(sse_identities, sse_indices)
            ]
            workers = [
                asyncio.create_task(
                    _worker_loop(shard, identity),
                    name=f"shard-worker-{identity.index}",
                )
                for identity in identities
            ]
            await shard.stop_claim_event.wait()
            if workers:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.gather(*workers, return_exceptions=True),
                        timeout=config.submit_timeout_s + 60.0,
                    )
            for task in sse_tasks:
                task.cancel()
            if sse_tasks:
                await asyncio.gather(*sse_tasks, return_exceptions=True)
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            result_queue.put(_shard_snapshot(shard.stats))

    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001 - parent reports the shard failure
        result_queue.put({"fatal_errors": [f"shard crashed: {exc!r}"]})


# ---------------------------------------------------------------------------
# Legacy replay
# ---------------------------------------------------------------------------
def _load_result_json(path: Path) -> Mapping[str, Any]:
    """Load a result JSON, tolerating the old script's concatenated stdout.

    The legacy benchmark printed its result object and then a second JSON
    object with the server log scan; both were written to stdout.  Prefer the
    document that actually contains final task statuses.
    """
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    documents: list[Mapping[str, Any]] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        document, index = decoder.raw_decode(text, index)
        if isinstance(document, Mapping):
            documents.append(document)
    for document in documents:
        if "final_statuses" in document:
            return document
    if documents:
        return documents[0]
    raise ValueError(f"{path} contains no JSON object")


def evaluate_legacy_payload(payload: Mapping[str, Any], config: RunConfig) -> dict[str, Any]:
    statuses = {
        str(key): int(value)
        for key, value in (payload.get("final_statuses") or {}).items()
    }
    total = sum(statuses.values())
    expected = int(payload.get("tasks") or total)
    committed = statuses.get("COMMITTED", 0)
    failed = statuses.get("FAILED", 0)
    non_terminal = sum(
        value for key, value in statuses.items() if key not in TERMINAL_STATUSES
    )
    http_errors = int(payload.get("http_errors") or 0)
    inp = AcceptanceInput(
        expected_tasks=expected,
        db_task_count=total,
        missing_tasks=max(0, expected - total),
        extra_tasks=max(0, total - expected),
        final_statuses=statuses,
        client_statuses={},
        accepted_tasks=committed,
        accepted_non_terminal=0,
        client_committed_without_db=0,
        ops={},
        sse={"streams": int(payload.get("sse_streams") or 0)},
        pool_timeouts=0,
        production_grade=False,
        profile=config.profile,
        fatal_errors=[],
        metrics_available=False,
        detail_complete=False,
        max_failed=config.max_failed,
        require_all_committed=config.require_all_committed,
        max_http_error_rate=config.max_http_error_rate,
        max_transport_error_rate=config.max_transport_error_rate,
        max_submit_error_rate=config.max_submit_error_rate,
        max_sse_error_rate=config.max_sse_error_rate,
        max_retry_rate=config.max_retry_rate,
        max_error_events=config.max_error_events,
        max_pool_timeouts=config.max_pool_timeouts,
        min_sse_open_ratio=config.min_sse_open_ratio,
        legacy_http_errors=http_errors,
    )
    verdict = evaluate_acceptance(inp)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "legacy-replay",
        "source": {
            "path": str(config.evaluate_json) if config.evaluate_json else None,
            "legacy_fields": {
                "tasks": payload.get("tasks"),
                "final_statuses": statuses,
                "http_errors": http_errors,
                "duration_s": payload.get("duration_s"),
                "sse_streams": payload.get("sse_streams"),
            },
        },
        "profile": config.profile,
        "production_grade": False,
        "database": {
            "task_count": total,
            "expected_tasks": expected,
            "final_statuses": statuses,
            "committed": committed,
            "failed": failed,
            "non_terminal": non_terminal,
        },
        "acceptance": verdict,
        "artifacts": {},
    }


# ---------------------------------------------------------------------------
# Live run
# ---------------------------------------------------------------------------
async def run_live(config: RunConfig) -> tuple[dict[str, Any], ServerHandle | None]:
    stats = RunStats(config)
    base = Path(tempfile.mkdtemp(prefix=f"veynod-bench-{config.scenario}-"))
    server: ServerHandle | None = None
    servers: list[ServerHandle] = []
    gate_worker: GateWorkerHandle | None = None
    probe: DbProbe | None = None
    created_database = False
    db_name = config.database_name
    # The production profile enforces a >=32-char admin key (app/security.py).
    admin_key = f"{DEFAULT_ADMIN_KEY}-{secrets.token_hex(20)}"
    config_dict = dataclasses.asdict(config)
    config_dict["admin_db_url"] = _mask_url(config_dict["admin_db_url"])
    config_dict["output_dir"] = str(config_dict["output_dir"])
    config_dict["evaluate_json"] = None
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "live",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "config": config_dict,
        "environment": {
            "python": sys.version.split()[0],
            "cpu_count": os.cpu_count(),
            "platform": sys.platform,
        },
        "database": {},
        "server": {},
        "client": {},
        "queue": {},
        "client_vs_database": {},
        "timing": {},
        "acceptance": {},
        "artifacts": {},
    }
    try:
        if db_name is None:
            db_name = _safe_db_name(config.scenario, config.label)
        if not config.reuse_database:
            await _create_database(config.admin_db_url, db_name)
            created_database = True
        elif not await _database_exists(config.admin_db_url, db_name):
            raise SetupError(f"--reuse-database {db_name!r} does not exist")
        elif await _database_has_workspaces(config.admin_db_url, db_name):
            raise SetupError(
                f"--reuse-database {db_name!r} already contains a workspace; "
                "reuse is only supported for an empty isolated database"
            )
        # Build from the admin URL while preserving credentials/host/port.
        server_db_url = _replace_database(_sqlalchemy_url(config.admin_db_url), db_name)
        probe_dsn = _asyncpg_dsn(_replace_database(config.admin_db_url, db_name))
        result["database"] = {
            "name": db_name,
            "isolated": created_database,
            "reused": config.reuse_database,
            "admin_url": _mask_url(config.admin_db_url),
            "server_url": _mask_url(server_db_url),
            "created": created_database,
            "dropped": False,
            "cleanup_sql": f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE);',
        }
        bare, specs, head = make_repo(
            base, config.files, config.functions, config.scenario
        )
        result["repository"] = {
            "path": str(bare),
            "head_sha": head,
            "files": config.files,
            "functions": config.functions,
            "tasks": len(specs),
        }
        for index in range(max(1, config.api_instances)):
            _assert_port_free(config.port + index, f"api-{index}")
        _assert_port_free(config.gate_worker_port, "gate-worker")
        submit_instances = max(0, min(config.submit_instances, max(1, config.api_instances) - 1))
        claim_count = max(1, max(1, config.api_instances) - submit_instances)
        servers = []
        for index in range(max(1, config.api_instances)):
            is_submit = index >= claim_count
            plane = "submit" if is_submit else ("claim_sse" if submit_instances else "all")
            servers.append(
                ServerHandle(
                    api=f"http://127.0.0.1:{config.port + index}",
                    port=config.port + index,
                    db_url=server_db_url,
                    admin_key=admin_key,
                    log_path=base / f"server-{index}.log",
                    config=config,
                    extra_env={
                        "VN_API_PLANE": plane,
                        "VN_ENABLE_BACKGROUND_LOOPS": (
                            "true" if index == 0 and not is_submit else "false"
                        ),
                        # Give the submit plane a larger pool; claim planes
                        # mostly park without a connection.
                        "VN_DB_POOL_SIZE": "40" if is_submit else "10",
                        "VN_DB_MAX_OVERFLOW": "20" if is_submit else "10",
                    },
                )
            )
        for instance in servers:
            instance.start()
        server = servers[0]
        for instance in servers:
            instance.wait_health(config.server_start_timeout_s)
        gate_worker = GateWorkerHandle(
            db_url=server_db_url,
            metrics_port=config.gate_worker_port,
            log_path=base / "gate_worker.log",
            config=config,
        )
        gate_worker.start()
        gate_worker.wait_health(config.server_start_timeout_s)
        setup_started = time.monotonic()
        limits = httpx.Limits(
            max_connections=max(64, config.runners + config.sse_streams + 64),
            max_keepalive_connections=max(64, config.runners + 32),
        )
        async with httpx.AsyncClient(timeout=60.0, limits=limits) as setup_client:
            workspace_id = await _create_workspace(
                setup_client,
                server.api,
                admin_key,
                bare,
                config.label,
                stats,
            )
            token_count = config.runners + config.sse_streams
            identities = await _create_tokens(
                setup_client,
                server.api,
                admin_key,
                token_count,
                config.label,
                stats,
                config.setup_concurrency,
            )
            task_data: dict[str, TaskSpec] = {}
            await _seed_artifacts_and_tasks(
                setup_client,
                server.api,
                admin_key,
                workspace_id,
                specs,
                stats,
                config.setup_concurrency,
                task_data,
            )
        probe = DbProbe(probe_dsn)
        await probe.start()
        initial = await probe.snapshot(workspace_id, set(task_data))
        result["database"].update(
            {
                "workspace_id": workspace_id,
                "initial_task_count": initial["task_count"],
                "initial_statuses": initial["statuses"],
                "expected_tasks": config.expected_tasks,
                "seeded_tasks": len(task_data),
            }
        )
        if initial["task_count"] != len(specs):
            raise SetupError(
                f"seeded {len(specs)} tasks but database has {initial['task_count']}"
            )
        stats.initial_queue = initial
        result["timing"]["setup_s"] = round(time.monotonic() - setup_started, 3)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
            limits=httpx.Limits(
                max_connections=config.runners * 2 + 64,
                max_keepalive_connections=config.runners * 2 + 64,
            ),
            event_hooks={"request": [_stamp_wire_sent_at]},
        ) as worker_client, httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
            limits=httpx.Limits(
                max_connections=config.runners + 64,
                max_keepalive_connections=config.runners + 64,
            ),
            event_hooks={"request": [_stamp_wire_sent_at]},
        ) as submit_client, httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0),
            limits=httpx.Limits(
                max_connections=max(8, config.sse_streams + 8),
                max_keepalive_connections=0,
            ),
        ) as sse_client, httpx.AsyncClient(
            timeout=10.0,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        ) as admin_client:
            harness = Harness(
                config=config,
                api=server.api,
                apis=[instance.api for instance in servers],
                claim_apis=[instance.api for instance in servers[:claim_count]],
                submit_apis=[
                    instance.api
                    for instance in servers[claim_count:]
                ]
                or [instance.api for instance in servers[:claim_count]],
                workspace_id=workspace_id,
                task_specs=task_data,
                identities=identities,
                stats=stats,
                probe=probe,
                worker_client=worker_client,
                submit_client=submit_client,
                sse_client=sse_client,
                admin_client=admin_client,
                admin_key=admin_key,
                worker_metrics_url=gate_worker.metrics_url,
                resources=ResourceSampler(
                    {
                        **{
                            f"api_{index}": (
                                instance.proc.pid if instance.proc else os.getpid()
                            )
                            for index, instance in enumerate(servers)
                        },
                        "gate_worker": (
                            gate_worker.proc.pid
                            if gate_worker.proc
                            else os.getpid()
                        ),
                        "bench": os.getpid(),
                    }
                ),
            )
            await harness.reset_server_metrics()
            load_started = time.monotonic()
            await harness.run_load()
            acceptance_elapsed = time.monotonic() - load_started
            load_elapsed = (
                harness.cutoff_at - load_started
                if harness.cutoff_at
                else acceptance_elapsed
            )
            drain_elapsed = (
                harness.drained_at - harness.cutoff_at
                if harness.cutoff_at and harness.drained_at
                else 0.0
            )
            final_snapshot = await probe.snapshot(workspace_id, set(task_data))
            final_metrics = await harness.fetch_server_metrics()
            harness.server_metrics_final = final_metrics
            worker_metrics = await harness.fetch_worker_metrics()
            harness.worker_metrics_final = worker_metrics
            # The final snapshot is what the verdict consumes.  A successful
            # reset alone is not enough if the metrics GET failed afterwards.
            harness.server_metrics_available = final_metrics is not None
            result["timing"]["load_s"] = round(load_elapsed, 3)
            result["timing"]["drain_s"] = round(drain_elapsed, 3)
            result["timing"]["acceptance_s"] = round(acceptance_elapsed, 3)
            result["timing"]["cutoff_reason"] = harness.cutoff_reason
            result["throughput"] = _throughput_block(
                stats, final_snapshot, load_elapsed, acceptance_elapsed
            )
            result["queue"] = _queue_block(stats, final_snapshot)
            result["server"] = _server_block(
                final_metrics, harness, server, stats, gate_worker
            )
            result["client"] = stats.as_client_dict()
            if harness.resources is not None:
                result["resources"] = harness.resources.summary()
            result["client_vs_database"] = _client_vs_database(
                stats, final_snapshot
            )
            result["database"].update(
                {
                    "task_count": final_snapshot["task_count"],
                    "final_statuses": final_snapshot["statuses"],
                    "terminal": final_snapshot["terminal"],
                    "non_terminal": final_snapshot["non_terminal"],
                    "committed": final_snapshot["committed"],
                    "failed": final_snapshot["failed"],
                    "result_commit_tasks": final_snapshot["result_commit_tasks"],
                    "missing_tasks": final_snapshot["missing_tasks"],
                    "extra_tasks": final_snapshot["extra_tasks"],
                    "queued_tasks": final_snapshot["non_terminal"],
                }
            )
            result["timing"]["total_s"] = round(
                acceptance_elapsed + result["timing"]["setup_s"], 3
            )
            result["acceptance"] = _verdict_for_run(
                config, stats, final_snapshot, harness
            )
            result["client_vs_database"]["server_submit_incomplete"] = (
                result["acceptance"].get("server_submit_incomplete", 0)
            )
    except SetupError as exc:
        exc.partial_result = result
        raise
    finally:
        if probe is not None:
            await probe.close()
        if gate_worker is not None:
            gate_worker.stop()
            result.setdefault("server", {})
            worker_log = gate_worker.log_text()
            result["server"]["worker_exit_code"] = gate_worker.exit_code
            result["server"]["worker_log_path"] = str(gate_worker.log_path)
            result["server"]["worker_log_analysis"] = _analyze_log(worker_log)
            result["server"]["worker_log_tail"] = worker_log[-4000:]
        if servers:
            log_text = ""
            instance_logs: list[dict[str, Any]] = []
            for instance in servers:
                instance.stop()
                instance_text = instance.log_text()
                instance_logs.append(
                    {
                        "api": instance.api,
                        "port": instance.port,
                        "exit_code": instance.exit_code,
                        "log_path": str(instance.log_path),
                        "log_analysis": _analyze_log(instance_text),
                        "log_tail": instance_text[-2000:],
                    }
                )
                if instance is server:
                    log_text = instance_text
            result.setdefault("server", {})
            result["server"]["api_instances"] = len(servers)
            result["server"]["instance_logs"] = instance_logs
            result["server"]["exit_code"] = server.exit_code if server else None
            result["server"]["log_path"] = str(server.log_path) if server else None
            # The pre-stop log is the load-phase log.  Keep a separate
            # shutdown snapshot: terminating uvicorn can interrupt in-flight
            # git subprocesses and produce shutdown-only tracebacks.
            if "log_analysis" not in result["server"]:
                result["server"]["log_analysis"] = _analyze_log(log_text)
            result["server"]["shutdown_log_analysis"] = _analyze_log(log_text)
            result["server"]["log_tail_shutdown"] = log_text[-4000:]
        policy = config.db_policy
        should_drop = (
            created_database
            and not config.reuse_database
            and (
                policy == "always-drop"
                or (policy == "keep-on-failure" and result.get("acceptance", {}).get("passed"))
            )
        )
        if should_drop and db_name:
            try:
                await _drop_database(config.admin_db_url, db_name)
                result["database"]["dropped"] = True
            except Exception as exc:  # noqa: BLE001
                result["database"]["drop_error"] = str(exc)
        if not config.keep_work_dir:
            shutil.rmtree(base, ignore_errors=True)
        else:
            result["artifacts"]["work_dir"] = str(base)
    return result, server


def _mask_url(url: str) -> str:
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", url)


def _throughput_block(
    stats: RunStats,
    final_snapshot: Mapping[str, Any],
    load_s: float,
    acceptance_s: float,
) -> dict[str, Any]:
    window = max(acceptance_s, 0.001)
    claim_window = max(load_s, 0.001)
    samples = stats.queue_samples
    peak_committed_per_s = 0.0
    for previous, current in zip(samples, samples[1:]):
        delta_s = current.elapsed_s - previous.elapsed_s
        if delta_s > 0:
            peak_committed_per_s = max(
                peak_committed_per_s,
                max(0, current.committed - previous.committed) / delta_s,
            )
    committed_at_cutoff = 0
    for sample in samples:
        if sample.elapsed_s <= load_s:
            committed_at_cutoff = sample.committed
    return {
        "window_s": round(window, 3),
        "claim_window_s": round(claim_window, 3),
        "committed_at_cutoff": committed_at_cutoff,
        "committed_per_s_at_cutoff": round(committed_at_cutoff / claim_window, 4),
        "committed_total": int(final_snapshot["committed"]),
        "terminal_total": int(final_snapshot["terminal"]),
        "committed_per_s": round(int(final_snapshot["committed"]) / window, 4),
        "terminal_per_s": round(int(final_snapshot["terminal"]) / window, 4),
        "submit_accept_per_s": round(stats.submit.success / window, 4),
        "submit_200_per_s": round(stats.submit.success / window, 4),
        "claim_leased_per_s": round(stats.claim.success / window, 4),
        "peak_committed_per_s": round(peak_committed_per_s, 4),
    }


def _queue_block(stats: RunStats, final_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "initial": {
            "statuses": (stats.initial_queue or {}).get("statuses", {}),
            "non_terminal": (stats.initial_queue or {}).get("non_terminal"),
        },
        "final": {
            "statuses": dict(final_snapshot["statuses"]),
            "non_terminal": final_snapshot["non_terminal"],
        },
        "peak": dict(sorted(stats.peak.items())),
        "peak_non_terminal": stats.peak_total,
        "peak_checkedout": stats.peak_checkedout,
        "samples": [
            dataclasses.asdict(sample) for sample in stats.queue_samples
        ],
        "monitor_errors": stats.monitor_errors,
    }


def _server_block(
    metrics: dict[str, Any] | None,
    harness: Harness,
    server: ServerHandle,
    stats: RunStats,
    gate_worker: GateWorkerHandle,
) -> dict[str, Any]:
    worker_metrics = harness.worker_metrics_final or {}
    counters: dict[str, int] = {}
    for name, value in (metrics or {}).get("counters", {}).items():
        if isinstance(value, int):
            counters[name] = counters.get(name, 0) + value
    for name, value in worker_metrics.get("counters", {}).items():
        if isinstance(value, int):
            counters[name] = counters.get(name, 0) + value
    histograms = dict((metrics or {}).get("histograms_ms", {}))
    histograms.update(worker_metrics.get("histograms_ms", {}))
    return {
        "metrics_available": (
            harness.server_metrics_available and harness.worker_metrics_available
        ),
        "api_metrics_available": harness.server_metrics_available,
        "worker_metrics_available": harness.worker_metrics_available,
        "metrics_reset_at": harness.metrics_reset_at,
        "started_at": (metrics or {}).get("started_at"),
        "counters": counters,
        "histograms_ms": histograms,
        "db_pool": (metrics or {}).get("db_pool", {}),
        "worker_db_pool": worker_metrics.get("db_pool", {}),
        "gauges": {
            **(metrics or {}).get("gauges", {}),
            **worker_metrics.get("gauges", {}),
        },
        "samples": stats.metrics_samples,
        "log_analysis": _analyze_log(server.log_text()),
        "log_tail": server.log_text()[-4000:],
        "worker_exit_code": gate_worker.exit_code,
        "worker_log_analysis": _analyze_log(gate_worker.log_text()),
        "worker_log_tail": gate_worker.log_text()[-4000:],
    }


def _client_vs_database(
    stats: RunStats, final_snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    db_committed = set(final_snapshot.get("committed_ids", set()))
    client_committed = set(stats.client_committed_ids)
    final_by_id = final_snapshot.get("by_id", {})
    accepted = set(stats.accepted_task_ids)
    accepted_non_terminal = sum(
        1
        for task_id in accepted
        if final_by_id.get(task_id, {}).get("status") not in TERMINAL_STATUSES
    )
    return {
        "client_committed_responses": len(client_committed),
        "db_committed_tasks": len(db_committed),
        "db_committed_without_client_success": len(db_committed - client_committed),
        "client_committed_without_db": len(client_committed - db_committed),
        "late_commits_reconciled": stats.reconciled.get("late_commit", 0),
        "accepted_submissions": len(accepted),
        "accepted_terminal": len(accepted) - accepted_non_terminal,
        "accepted_non_terminal": accepted_non_terminal,
        "client_timeouts": stats.submit.timeouts,
        "client_transport_errors": stats.submit.transport_errors,
        "reconciled": dict(sorted(stats.reconciled.items())),
    }


def _verdict_for_run(
    config: RunConfig,
    stats: RunStats,
    final_snapshot: Mapping[str, Any],
    harness: Harness,
) -> dict[str, Any]:
    comparison = _client_vs_database(stats, final_snapshot)
    ops = {
        "claim": stats.claim.as_dict(),
        "submit": stats.submit.as_dict(),
        "release": stats.release.as_dict(),
        "heartbeat": stats.heartbeat.as_dict(),
    }
    sse = {
        "streams": config.sse_streams,
        "attempts": stats.sse.attempts,
        "opens": stats.sse.opens,
        "streams_opened_once": stats.sse.streams_opened_once,
        "frames": stats.sse.frames,
        "http_errors": stats.sse.http_errors,
        "timeouts": stats.sse.timeouts,
        "transport_errors": stats.sse.transport_errors,
        "reconnects": stats.sse.reconnects,
        "bytes": stats.sse.bytes,
    }
    pool_timeouts = int(
        ((harness.server_metrics_final or {}).get("db_pool") or {}).get("timeouts", 0)
    ) + int(
        ((harness.worker_metrics_final or {}).get("db_pool") or {}).get("timeouts", 0)
    )
    server_counters: dict[str, int] = {}
    for snapshot in (
        harness.server_metrics_final or {},
        harness.worker_metrics_final or {},
    ):
        for name, value in (snapshot.get("counters") or {}).items():
            if isinstance(value, int):
                server_counters[name] = server_counters.get(name, 0) + value
    server_submit_incomplete = max(
        0,
        int(server_counters.get("submit.started", 0))
        - int(server_counters.get("submit.finished", 0)),
    )
    accept_summary = stats.latencies["submit_accept"].summary()
    submit_accept_p99 = (
        float(accept_summary["p99_ms"]) if accept_summary["count"] else None
    )
    inspection = AcceptanceInput(
        expected_tasks=config.expected_tasks,
        db_task_count=int(final_snapshot["task_count"]),
        missing_tasks=int(final_snapshot["missing_tasks"]),
        extra_tasks=int(final_snapshot["extra_tasks"]),
        final_statuses=dict(final_snapshot["statuses"]),
        client_statuses=dict(stats.client_statuses),
        accepted_tasks=comparison["accepted_submissions"],
        accepted_non_terminal=comparison["accepted_non_terminal"],
        client_committed_without_db=comparison["client_committed_without_db"],
        ops=ops,
        sse=sse,
        pool_timeouts=pool_timeouts,
        production_grade=(
            config.profile == "production"
            and not config.skip_typecheck
            and not config.skip_lint
            and harness.server_metrics_available
            and harness.worker_metrics_available
        ),
        profile=config.profile,
        fatal_errors=stats.fatal_errors,
        metrics_available=(
            harness.server_metrics_available and harness.worker_metrics_available
        ),
        detail_complete=True,
        max_failed=config.max_failed,
        require_all_committed=config.require_all_committed,
        max_http_error_rate=config.max_http_error_rate,
        max_transport_error_rate=config.max_transport_error_rate,
        max_submit_error_rate=config.max_submit_error_rate,
        max_sse_error_rate=config.max_sse_error_rate,
        max_retry_rate=config.max_retry_rate,
        max_error_events=config.max_error_events,
        max_pool_timeouts=config.max_pool_timeouts,
        min_sse_open_ratio=config.min_sse_open_ratio,
        submit_accept_p99_ms=submit_accept_p99,
        max_accept_p99_ms=config.max_accept_p99_ms,
        server_submit_incomplete=server_submit_incomplete,
    )
    verdict = evaluate_acceptance(inspection)
    verdict["server_submit_incomplete"] = server_submit_incomplete
    verdict["cutoff_reason"] = harness.cutoff_reason
    verdict["server_metrics_available"] = harness.server_metrics_available
    return verdict


def _analyze_log(text: str) -> dict[str, Any]:
    return {
        "length": len(text),
        "queuepool_limit": text.count("QueuePool limit"),
        "timeout_error": text.count("TimeoutError"),
        "config_lock": text.count("could not lock config file"),
        "traceback": text.count("Traceback (most recent call last)"),
        "error_lines": text.count("ERROR:"),
        "capacity_errors": text.count("pool_timeout")
        + text.count("QueuePool limit"),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Trustworthy Veynod Kernel swarm capacity acceptance harness."
    )
    parser.add_argument("--label", default=None)
    parser.add_argument(
        "--scenario", choices=("cross-file", "hot-file"), default="cross-file"
    )
    parser.add_argument("--files", type=int, default=None)
    parser.add_argument("--functions", type=int, default=None)
    parser.add_argument("--runners", type=int, default=1000)
    parser.add_argument("--sse-streams", type=int, default=0)
    parser.add_argument(
        "--expected-tasks",
        type=int,
        default=None,
        help="Override the expected task count (integrity tests); default files*functions.",
    )
    parser.add_argument("--profile", choices=("production", "dev"), default="production")
    parser.add_argument("--skip-typecheck", action="store_true")
    parser.add_argument("--skip-lint", action="store_true")
    parser.add_argument(
        "--mode",
        choices=("claim", "merge"),
        default="merge",
        help="claim: pull then release (no submit); merge: full submit/gate/merge pipeline",
    )
    parser.add_argument(
        "--api-instances",
        type=int,
        default=1,
        help="independent uvicorn processes on consecutive ports sharing one DB",
    )
    parser.add_argument(
        "--submit-instances",
        type=int,
        default=0,
        help="last N API instances serve only submit/status (0 = all-in-one)",
    )
    parser.add_argument(
        "--client-shards",
        type=int,
        default=1,
        help="independent OS client processes for runners/SSE (1 = old behaviour)",
    )
    parser.add_argument(
        "--db-pool-size",
        type=int,
        default=8,
        help="SQLAlchemy pool per API/gate-worker process",
    )
    parser.add_argument(
        "--db-max-overflow",
        type=int,
        default=12,
        help="SQLAlchemy overflow per API/gate-worker process",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--output-dir", default=str(ROOT / ".run" / "bench"))
    parser.add_argument(
        "--admin-db-url",
        default=DEFAULT_ADMIN_DB_URL,
        help="PostgreSQL admin/maintenance URL used only to CREATE/DROP the isolated database.",
    )
    parser.add_argument("--database-name", default=None)
    parser.add_argument("--reuse-database", action="store_true")
    parser.add_argument(
        "--db-policy",
        choices=("keep-on-failure", "always-drop", "always-keep"),
        default="keep-on-failure",
    )
    parser.add_argument("--keep-work-dir", action="store_true")
    parser.add_argument("--server-start-timeout-s", type=float, default=180.0)
    parser.add_argument("--max-duration-s", type=float, default=1800.0)
    parser.add_argument("--drain-timeout-s", type=float, default=120.0)
    parser.add_argument("--claim-timeout-s", type=float, default=60.0)
    parser.add_argument(
        "--empty-claim-backoff-s",
        type=float,
        default=5.0,
        help="idle wait before a Runner retries an empty claim queue",
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=600,
        help="task lease for the benchmark server (shorter recovers lost responses)",
    )
    parser.add_argument("--submit-accept-timeout-s", type=float, default=30.0)
    parser.add_argument("--submit-timeout-s", type=float, default=600.0)
    parser.add_argument("--sse-timeout-s", type=float, default=45.0)
    parser.add_argument("--heartbeat-interval-s", type=float, default=30.0)
    parser.add_argument("--think-min-s", type=float, default=0.05)
    parser.add_argument("--think-max-s", type=float, default=0.20)
    parser.add_argument("--sample-interval-s", type=float, default=1.0)
    parser.add_argument("--metrics-interval-s", type=float, default=2.0)
    parser.add_argument("--reconcile-interval-s", type=float, default=2.0)
    parser.add_argument("--sse-warmup-s", type=float, default=10.0)
    parser.add_argument("--gate-pool-size", type=int, default=8)
    parser.add_argument("--gate-workers", type=int, default=8)
    parser.add_argument("--gate-worker-port", type=int, default=8030)
    parser.add_argument("--gate-sandbox", choices=("local", "docker"), default="local")
    parser.add_argument("--gate-sandbox-image", default=None)
    parser.add_argument("--work-lease-seconds", type=int, default=300)
    parser.add_argument("--reaper-interval-s", type=int, default=3600)
    parser.add_argument("--setup-concurrency", type=int, default=32)
    parser.add_argument("--no-fast-forward-merge", action="store_true")
    parser.add_argument("--max-http-error-rate", type=float, default=0.01)
    parser.add_argument("--max-transport-error-rate", type=float, default=0.005)
    parser.add_argument("--max-submit-error-rate", type=float, default=0.002)
    parser.add_argument("--max-sse-error-rate", type=float, default=0.01)
    parser.add_argument("--max-retry-rate", type=float, default=0.05)
    parser.add_argument("--max-error-events", type=int, default=None)
    parser.add_argument("--max-pool-timeouts", type=int, default=0)
    parser.add_argument("--max-accept-p99-ms", type=float, default=1000.0)
    parser.add_argument("--max-failed", type=int, default=0)
    parser.add_argument(
        "--allow-failed",
        action="store_true",
        help="Accept up to --max-failed terminal failures instead of requiring every task COMMITTED.",
    )
    parser.add_argument("--min-sse-open-ratio", type=float, default=0.99)
    parser.add_argument("--log-level", default="warning")
    parser.add_argument(
        "--evaluate-json",
        type=Path,
        default=None,
        help="Re-evaluate a previous result (including legacy bench_swarm output) and exit.",
    )
    return parser


def _resolve_config(args: argparse.Namespace) -> RunConfig:
    if args.files is None:
        args.files = 1000 if args.scenario == "cross-file" else 10
    if args.functions is None:
        args.functions = 1 if args.scenario == "cross-file" else 100
    if args.runners < 0 or args.sse_streams < 0:
        raise SetupError("runners and sse-streams must be non-negative")
    if args.files < 1 or args.functions < 1:
        raise SetupError("files and functions must be positive")
    expected = (
        args.expected_tasks
        if args.expected_tasks is not None
        else args.files * args.functions
    )
    if expected < 1:
        raise SetupError("expected task count must be positive")
    if args.runners == 0 and args.sse_streams == 0:
        raise SetupError("at least one Runner or SSE stream is required")
    if args.scenario == "hot-file" and args.files > args.functions and args.files > 1:
        # Not an error, but warn in the result; the preset is 10 files x 100 functions.
        pass
    if args.profile == "production":
        skip_typecheck = bool(args.skip_typecheck)
        skip_lint = bool(args.skip_lint)
        if args.evaluate_json is None:
            if not skip_typecheck and shutil.which("pyright") is None:
                raise SetupError(
                    "production profile needs pyright on PATH; use --profile dev or --skip-typecheck"
                )
            if not skip_lint and shutil.which("ruff") is None:
                raise SetupError(
                    "production profile needs ruff on PATH; use --profile dev or --skip-lint"
                )
    else:
        skip_typecheck = True
        skip_lint = True
    label = args.label or args.scenario
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    port = args.port
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((args.host, 0))
            port = int(sock.getsockname()[1])
    return RunConfig(
        scenario=args.scenario,
        files=args.files,
        functions=args.functions,
        runners=args.runners,
        sse_streams=args.sse_streams,
        expected_tasks=expected,
        profile=args.profile,
        skip_typecheck=skip_typecheck,
        skip_lint=skip_lint,
        label=label,
        host=args.host,
        port=port,
        output_dir=output_dir,
        admin_db_url=args.admin_db_url,
        database_name=args.database_name,
        reuse_database=args.reuse_database,
        db_policy=args.db_policy,
        keep_work_dir=args.keep_work_dir,
        server_start_timeout_s=args.server_start_timeout_s,
        mode=args.mode,
        api_instances=args.api_instances,
        submit_instances=getattr(args, "submit_instances", 0),
        client_shards=max(1, getattr(args, "client_shards", 1)),
        db_pool_size=args.db_pool_size,
        db_max_overflow=args.db_max_overflow,
        max_duration_s=args.max_duration_s,
        drain_timeout_s=args.drain_timeout_s,
        claim_timeout_s=args.claim_timeout_s,
        empty_claim_backoff_s=args.empty_claim_backoff_s,
        lease_seconds=args.lease_seconds,
        submit_accept_timeout_s=args.submit_accept_timeout_s,
        submit_timeout_s=args.submit_timeout_s,
        sse_timeout_s=args.sse_timeout_s,
        heartbeat_interval_s=args.heartbeat_interval_s,
        think_min_s=args.think_min_s,
        think_max_s=args.think_max_s,
        sample_interval_s=args.sample_interval_s,
        metrics_interval_s=args.metrics_interval_s,
        reconcile_interval_s=args.reconcile_interval_s,
        sse_warmup_s=args.sse_warmup_s,
        fast_forward_merge=not args.no_fast_forward_merge,
        gate_pool_size=args.gate_pool_size,
        gate_workers=args.gate_workers,
        gate_worker_port=args.gate_worker_port,
        gate_sandbox=args.gate_sandbox,
        gate_sandbox_image=args.gate_sandbox_image,
        work_lease_s=args.work_lease_seconds,
        reaper_interval_s=args.reaper_interval_s,
        setup_concurrency=args.setup_concurrency,
        max_http_error_rate=args.max_http_error_rate,
        max_transport_error_rate=args.max_transport_error_rate,
        max_submit_error_rate=args.max_submit_error_rate,
        max_sse_error_rate=args.max_sse_error_rate,
        max_retry_rate=args.max_retry_rate,
        max_error_events=args.max_error_events,
        max_pool_timeouts=args.max_pool_timeouts,
        max_accept_p99_ms=args.max_accept_p99_ms,
        max_failed=args.max_failed,
        require_all_committed=not args.allow_failed,
        min_sse_open_ratio=args.min_sse_open_ratio,
        log_level=args.log_level,
        evaluate_json=args.evaluate_json.resolve() if args.evaluate_json else None,
    )


def _summary(result: Mapping[str, Any]) -> dict[str, Any]:
    acceptance = result.get("acceptance") or {}
    queue = result.get("queue") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": result.get("mode"),
        "source": result.get("source"),
        "profile": (result.get("config") or {}).get("profile"),
        "production_grade": acceptance.get("production_grade"),
        "acceptance": acceptance,
        "timing": result.get("timing", {}),
        "database": {
            key: value
            for key, value in (result.get("database") or {}).items()
            if key not in {"by_id"}
        },
        "client": {
            "claim": (result.get("client") or {}).get("claim"),
            "submit": (result.get("client") or {}).get("submit"),
            "release": (result.get("client") or {}).get("release"),
            "heartbeat": (result.get("client") or {}).get("heartbeat"),
            "sse": (result.get("client") or {}).get("sse"),
            "latency_ms": (result.get("client") or {}).get("latency_ms"),
            "client_submit_statuses": (result.get("client") or {}).get(
                "client_submit_statuses"
            ),
            "client_merge_modes": (result.get("client") or {}).get(
                "client_merge_modes"
            ),
            "submission_wait": (result.get("client") or {}).get(
                "submission_wait"
            ),
            "fatal_errors": (result.get("client") or {}).get("fatal_errors"),
        },
        "server": {
            "metrics_available": (result.get("server") or {}).get(
                "metrics_available"
            ),
            "db_pool": (result.get("server") or {}).get("db_pool"),
            "worker_db_pool": (result.get("server") or {}).get("worker_db_pool"),
            "counters": (result.get("server") or {}).get("counters"),
            "histograms_ms": (result.get("server") or {}).get("histograms_ms"),
            "log_analysis": (result.get("server") or {}).get("log_analysis"),
        },
        "throughput": result.get("throughput"),
        "queue": {
            "initial": queue.get("initial"),
            "final": queue.get("final"),
            "peak": queue.get("peak"),
            "peak_non_terminal": queue.get("peak_non_terminal"),
            "peak_checkedout": queue.get("peak_checkedout"),
            "sample_count": len(queue.get("samples") or []),
        },
        "client_vs_database": result.get("client_vs_database"),
        "artifacts": result.get("artifacts"),
    }


def _write_result(result: dict[str, Any], config: RunConfig) -> Path:
    stem = f"{utc_stamp()}-{re.sub(r'[^A-Za-z0-9_.-]+', '-', config.label)}"
    path = config.output_dir / f"{stem}.json"
    path.write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    latest = config.output_dir / "latest.json"
    with contextlib.suppress(OSError):
        shutil.copyfile(path, latest)
    return path


def _print_summary(result: Mapping[str, Any], result_path: Path | None) -> None:
    summary = _summary(result)
    if result_path is not None:
        summary["result_file"] = str(result_path)
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = _resolve_config(args)
    except SetupError as exc:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "mode": "setup",
                    "acceptance": {
                        "passed": False,
                        "exit_code": 2,
                        "reasons": [{"code": "setup_error", "detail": str(exc)}],
                    },
                    "error": str(exc),
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2

    if config.evaluate_json is not None:
        try:
            payload = _load_result_json(config.evaluate_json)
            result = evaluate_legacy_payload(payload, config)
        except Exception as exc:  # noqa: BLE001
            print(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "mode": "legacy-replay",
                        "acceptance": {
                            "passed": False,
                            "exit_code": 2,
                            "reasons": [{"code": "replay_error", "detail": str(exc)}],
                        },
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 2
        path = _write_result(result, config)
        _print_summary(result, path)
        return int(result["acceptance"]["exit_code"])

    result: dict[str, Any] = {}
    server: ServerHandle | None = None
    try:
        result, server = asyncio.run(run_live(config))
    except SetupError as exc:
        partial = getattr(exc, "partial_result", None)
        result = dict(partial) if partial else {
            "schema_version": SCHEMA_VERSION,
            "mode": "setup",
            "config": dataclasses.asdict(config)
            | {"output_dir": str(config.output_dir), "evaluate_json": None},
        }
        result["mode"] = "setup"
        result["acceptance"] = {
            "passed": False,
            "capacity_accepted": False,
            "exit_code": 2,
            "production_grade": False,
            "reasons": [{"code": "setup_error", "detail": str(exc)}],
            "checks": {},
            "warnings": [],
        }
        result["error"] = str(exc)
        path = _write_result(result, config)
        _print_summary(result, path)
        return 2
    except KeyboardInterrupt:
        result = {
            "schema_version": SCHEMA_VERSION,
            "mode": "interrupted",
            "acceptance": {
                "passed": False,
                "capacity_accepted": False,
                "exit_code": 130,
                "production_grade": False,
                "reasons": [{"code": "interrupted", "detail": "Ctrl+C"}],
                "checks": {},
                "warnings": [],
            },
        }
        path = _write_result(result, config)
        _print_summary(result, path)
        return 130
    except Exception as exc:  # noqa: BLE001
        result = {
            "schema_version": SCHEMA_VERSION,
            "mode": "harness-error",
            "acceptance": {
                "passed": False,
                "capacity_accepted": False,
                "exit_code": 2,
                "production_grade": False,
                "reasons": [{"code": "harness_error", "detail": repr(exc)}],
                "checks": {},
                "warnings": [],
            },
            "traceback": __import__("traceback").format_exc(),
        }
        path = _write_result(result, config)
        _print_summary(result, path)
        return 2

    path = _write_result(result, config)
    result.setdefault("artifacts", {})["result_file"] = str(path)
    path.write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    _print_summary(result, path)
    return int(result.get("acceptance", {}).get("exit_code", 1))


if __name__ == "__main__":
    raise SystemExit(main())
