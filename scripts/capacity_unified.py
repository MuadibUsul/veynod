"""Staged capacity run for the unified Runtime -> Kernel chain.

Measured, not assumed: real Kernel + Gate + Runtime + AgentGraph processes,
real PostgreSQL, a deterministic HTTP model, and a named concurrency level.

    python scripts/capacity_unified.py --levels 10,25,50,100 --profile fast
    python scripts/capacity_unified.py --levels 10,25 --profile real
    python scripts/capacity_unified.py --levels 250,500,1000 --profile fast

Profiles:
  real   the deterministic gate runs pyright + lint + pytest (production cost)
  fast   the same chain with the gate *commands* stubbed (VN_TEST_CMD=true),
         to measure orchestration capacity instead of CPU-bound type checking.
         Clearly marked in the report; never presented as a production number.

Every level creates a product task per concurrent slot, starts the Run, and
waits for the terminal product state.  Latencies are wall-clock from the test
client; the database facts (submissions, deliveries, wakeups) are read back
afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "AgentGraph", ROOT / "veynod_kernel", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import chaos_suite as cs  # noqa: E402


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((p / 100) * (len(ordered) - 1)))))
    return ordered[index]


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "p50": round(percentile(values, 50), 3),
        "p95": round(percentile(values, 95), 3),
        "p99": round(percentile(values, 99), 3),
        "max": round(max(values), 3) if values else 0.0,
        "mean": round(statistics.mean(values), 3) if values else 0.0,
    }


async def run_level(
    stack: cs.Stack, level: int, timeout: float, start_index: int = 0
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=60) as client:
        create_latency: list[float] = []
        start_latency: list[float] = []
        keys: list[str] = []
        started: dict[str, float] = {}
        headers = stack.auth

        for offset in range(level):
            index = start_index + offset
            key = f"cap-{level}-{index}-{int(time.time() * 1000) % 100000}"
            body = {
                "task_key": key,
                "title": f"Implement compute in src/f{index}.py",
                "project": stack.project,
                "repo": str(stack.repo),
                "description": (
                    f"Implement compute in src/f{index}.py so the frozen test "
                    f"tests/test_f{index}.py passes. Symbol src/f{index}.py::compute."
                ),
                "status": "READY",
            }
            begin = time.monotonic()
            response = await client.post(stack.api + "/v1/tasks", headers=headers, json=body)
            create_latency.append(time.monotonic() - begin)
            if response.status_code >= 300:
                raise RuntimeError(f"task create failed: {response.status_code} {response.text[:300]}")
            keys.append(key)

        # Start every Run first, then wait: the level is concurrent execution,
        # not a queue of one.
        for key in keys:
            begin = time.monotonic()
            response = await client.post(
                stack.api + f"/v1/tasks/{key}/run", headers=headers, json={}
            )
            start_latency.append(time.monotonic() - begin)
            if response.status_code >= 300:
                raise RuntimeError(f"run start failed: {response.status_code} {response.text[:300]}")
            started[key] = begin

        deadline = time.monotonic() + timeout
        statuses: dict[str, str] = {key: "PENDING" for key in keys}
        pending = set(keys)
        while pending and time.monotonic() < deadline:
            for key in list(pending):
                response = await client.get(
                    stack.api + f"/v1/tasks/{key}", headers=headers
                )
                if response.status_code < 300:
                    status = response.json()["status"]
                    statuses[key] = status
                    if status in {"REVIEWING", "FAILED", "CANCELLED"}:
                        pending.discard(key)
            if pending:
                await asyncio.sleep(0.5)

        e2e: list[float] = []
        for key, begin in started.items():
            response = await client.get(stack.api + f"/v1/tasks/{key}", headers=headers)
            if response.status_code < 300 and response.json()["status"] in {
                "REVIEWING", "FAILED", "CANCELLED"
            }:
                e2e.append(time.monotonic() - begin)

    delivered = sum(1 for key in keys if stack.commits_for(key) == 1)
    ok = [key for key in keys if statuses.get(key) == "REVIEWING"]
    return {
        "level": level,
        "completed": len(ok),
        "failed": sum(1 for s in statuses.values() if s == "FAILED"),
        "stuck": len(keys) - len(ok) - sum(1 for s in statuses.values() if s == "FAILED"),
        "delivered": delivered,
        "error_rate": round(1 - len(ok) / level, 4),
        "throughput_per_s": round(len(ok) / max(0.001, max(e2e) if e2e else 1), 3),
        "create_ms": summarize([v * 1000 for v in create_latency]),
        "run_start_ms": summarize([v * 1000 for v in start_latency]),
        "e2e_s": summarize(e2e),
    }


async def db_facts(stack: cs.Stack) -> dict[str, int]:
    import asyncpg

    facts: dict[str, int] = {}
    kernel = await asyncpg.connect(
        stack.kernel_dsn.replace("postgresql+asyncpg://", "postgresql://")
    )
    runtime = await asyncpg.connect(
        stack.runtime_dsn.replace("postgresql+asyncpg://", "postgresql://")
    )
    try:
        facts["submissions"] = int(
            await kernel.fetchval("SELECT count(*) FROM patch_submissions") or 0
        )
        facts["deliveries"] = int(
            await kernel.fetchval("SELECT count(*) FROM delivery_records") or 0
        )
        facts["dag_tasks"] = int(await kernel.fetchval("SELECT count(*) FROM dag_tasks") or 0)
        facts["runs"] = int(await runtime.fetchval("SELECT count(*) FROM agent_runtime.agent_runs") or 0)
        facts["stuck_runs"] = int(
            await runtime.fetchval(
                "SELECT count(*) FROM agent_runtime.agent_runs "
                "WHERE status NOT IN ('COMPLETED','FAILED','CANCELLED','TIMED_OUT','BUDGET_EXCEEDED')"
            )
            or 0
        )
        facts["wakeups"] = int(
            await runtime.fetchval("SELECT count(*) FROM agent_runtime.runtime_wakeups") or 0
        )
        facts["steps"] = int(
            await runtime.fetchval("SELECT count(*) FROM agent_runtime.agent_steps") or 0
        )
    finally:
        await kernel.close()
        await runtime.close()
    return facts


def sample_resources(pids: list[int]) -> dict[str, float]:
    total_rss = 0
    total_cpu = 0.0
    for pid in pids:
        try:
            process = psutil.Process(pid)
            total_rss += process.memory_info().rss
            total_cpu += process.cpu_percent(interval=None)
        except psutil.Error:
            continue
    return {"rss_mb": round(total_rss / 1e6, 1), "cpu_percent": round(total_cpu, 1)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels", default="10,25,50,100")
    parser.add_argument("--profile", choices=("fast", "real"), default="fast")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--gate-workers", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    levels = [int(item) for item in args.levels.split(",") if item.strip()]
    # Every level gets its own symbols: reusing an implemented function would
    # make the model produce an empty diff and measure nothing.
    cs.FUNCTIONS = sum(levels) + 2

    temp = Path(tempfile.mkdtemp(prefix="veynod-capacity-"))
    logs = temp / "logs"
    logs.mkdir()
    stack = cs.Stack(temp, logs)
    stack.worker_count = args.workers
    stack.gate_workers = args.gate_workers
    if args.profile == "fast":
        stack.gate_env = {
            "VN_SKIP_TYPECHECK": "true",
            "VN_SKIP_LINT": "true",
            "VN_TEST_CMD": "true",
            "VN_COLLECT_CMD": "true",
        }
    results: list[dict[str, Any]] = []
    try:
        stack.boot(temp)
        pids = [process.pid for process in stack.processes.values()]
        baseline = sample_resources(pids)
        print(
            f"stack up profile={args.profile} workers={args.workers} "
            f"gate_workers={args.gate_workers} baseline={baseline}",
            flush=True,
        )
        next_index = 0
        for level in levels:
            started = time.monotonic()
            result = asyncio.run(
                run_level(stack, level, args.timeout, start_index=next_index)
            )
            next_index += level
            result["wall_s"] = round(time.monotonic() - started, 1)
            result["resources"] = sample_resources(
                [process.pid for process in stack.processes.values()]
            )
            result["db"] = asyncio.run(db_facts(stack))
            results.append(result)
            print(json.dumps(result, default=str), flush=True)
    finally:
        stack.stop()
        report = {
            "profile": args.profile,
            "workers": args.workers,
            "gate_workers": args.gate_workers,
            "gate_commands": "real" if args.profile == "real" else "stubbed (VN_TEST_CMD=true)",
            "levels": results,
        }
        out = Path(args.out) if args.out else ROOT / f"docs/capacity_{args.profile}.json"
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"wrote {out}", flush=True)
        if not args.keep:
            import shutil

            shutil.rmtree(temp, ignore_errors=True)
        else:
            print(f"logs kept at {logs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
