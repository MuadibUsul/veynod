"""Micro-benchmark: pure Python fallback vs the Rust core.

Usage:
    python scripts/bench_rust.py

This is illustrative, not a production benchmark: it measures the hot pure
functions (lock expansion, AST slicing, claim planning) on one machine.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import veynod_core  # noqa: E402

from app.engine import rust_bridge  # noqa: E402
from app.engine.lock_algebra import _plan_claimable_python, plan_claimable  # noqa: E402
from app.services.ast_slicer import ASTContextSlicer, RustContextSlicer  # noqa: E402


def timed(label: str, function: Callable[[], object], repeats: int = 1) -> float:
    best = float("inf")
    for _ in range(repeats):
        started = time.perf_counter()
        function()
        best = min(best, time.perf_counter() - started)
    print(f"{label:<46} {best * 1000:>10.2f} ms")
    return best


def bench_locks() -> None:
    paths = [
        f"src/mod{index % 100}/file{index % 50}.py::C{index % 10}::m{index % 5}"
        for index in range(20_000)
    ]

    from app.engine.lock_algebra import LockAlgebra

    previous = rust_bridge.RUST_AVAILABLE
    rust_bridge.RUST_AVAILABLE = False
    try:
        timed("lock expand: Python (20k paths)", lambda: LockAlgebra.expand_write_set(paths))
    finally:
        rust_bridge.RUST_AVAILABLE = previous
    timed("lock expand: Rust   (20k paths)", lambda: LockAlgebra.expand_write_set(paths))


def bench_ast() -> None:
    source_parts = ['"""bench"""', ""]
    for index in range(300):
        source_parts.append(
            f'def f{index}(a: int, b: str) -> int:\n'
            f'    """doc {index}"""\n'
            f"    return a + {index}\n"
        )
    source = "\n".join(source_parts)
    symbols = [f"src/bench.py::f{index}" for index in range(300)]

    python_slicer = ASTContextSlicer()
    rust_slicer = RustContextSlicer()

    def run(slicer) -> None:
        for symbol in symbols:
            slicer.extract_artifact_signature(source, symbol)

    timed("AST signatures: Python (300 symbols)", lambda: run(python_slicer))
    timed("AST signatures: Rust   (300 symbols)", lambda: run(rust_slicer))


def bench_scheduler_core(count: int = 100_000) -> None:
    core = veynod_core.SchedulerCore()
    tasks = [
        (
            f"t{index}",
            "ws",
            "IMPLEMENT_BODY",
            "READY",
            100,
            index,
            index + 1,
            [f"src/scale/f{index}.py::f"],
            ["src"],
            None,
            None,
        )
        for index in range(count)
    ]
    timed(f"scheduler core upsert ({count:,})", lambda: core.upsert_tasks(tasks))
    timed(
        "scheduler core claim (1,000)",
        lambda: [
            core.claim_next("ws", [], "bench", f"token{index}", 10_000 + index)
            for index in range(1_000)
        ],
    )


def bench_planner(python_tasks: int = 1_000, rust_tasks: int = 100_000) -> None:
    def make_tasks(count: int) -> list[dict]:
        tasks = []
        for index in range(count):
            path = f"src/scale/file{index}.py::C::m"
            tasks.append(
                {
                    "task_id": f"t{index}",
                    "write_locks": [path],
                    "ancestor_locks": ["src", "src/scale", f"src/scale/file{index}.py"],
                    "status": "READY",
                    "priority": 100,
                    "seq": index,
                }
            )
        tasks.append(
            {
                "task_id": "active",
                "write_locks": ["src/scale/locked.py"],
                "ancestor_locks": ["src", "src/scale"],
                "status": "LEASED",
                "priority": 1,
                "seq": -1,
            }
        )
        return tasks

    python_snapshots = make_tasks(python_tasks)
    rust_snapshots = make_tasks(rust_tasks)
    timed(
        f"claim plan: Python ({python_tasks:,} ready)",
        lambda: _plan_claimable_python(python_snapshots),
    )
    timed(
        f"claim plan: Rust   ({rust_tasks:,} ready)",
        lambda: plan_claimable(rust_snapshots),
    )


def main() -> None:
    print(f"veynod_core {veynod_core.__version__} | python fallback comparison")
    print("-" * 70)
    bench_locks()
    bench_ast()
    bench_scheduler_core()
    bench_planner()


if __name__ == "__main__":
    main()
