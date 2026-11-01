"""Optional bridge to the Rust core extension (`veynod_core`).

The Python implementation remains the reference/fallback. The extension is
built with maturin; see `scripts/build_rust.py`. Set `VEYNOD_DISABLE_RUST=1` to
force the pure-Python path for debugging or portability.
"""

from __future__ import annotations

import os
from typing import Any

_DISABLED = os.environ.get("VEYNOD_DISABLE_RUST", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

_IMPORT_ERROR: str | None = None
try:
    import veynod_core as _core  # type: ignore[import-not-found]
except Exception as exc:  # pragma: no cover - environment dependent
    _core = None
    _IMPORT_ERROR = str(exc)

RUST_AVAILABLE = _core is not None and not _DISABLED


def available() -> bool:
    return RUST_AVAILABLE


def core() -> Any:
    if not RUST_AVAILABLE or _core is None:
        raise RuntimeError(
            "veynod_core is not available. Build it with "
            "`python scripts/build_rust.py` (or set VEYNOD_DISABLE_RUST=1 for "
            "the Python fallback)."
        )
    return _core


def version() -> str | None:
    if _core is None:
        return None
    return getattr(_core, "__version__", None)


def describe() -> dict[str, Any]:
    return {
        "available": RUST_AVAILABLE,
        "version": version(),
        "disabled": _DISABLED,
        "import_error": _IMPORT_ERROR,
    }


def engine_name() -> str:
    return "rust" if RUST_AVAILABLE else "python"


# ---------------------------------------------------------------------------
# Lock algebra
# ---------------------------------------------------------------------------
def normalize(raw: str) -> str:
    return core().normalize_symbol_path(raw)


def expand_write_set(paths: list[str]) -> tuple[list[str], list[str]]:
    return core().expand_write_set(paths)


def is_sub_write_set(parent: list[str], child: list[str]) -> bool:
    return core().is_sub_write_set(parent, child)


def is_covered_by_write_set(target: str, allowed: list[str]) -> bool:
    return core().is_covered_by_write_set(target, allowed)


def conflicts(left: str, right: str) -> bool:
    return core().conflicts(left, right)


def promote_new_files(paths: list[str], existing: set[str]) -> list[str]:
    return core().promote_new_files(paths, existing)


def file_paths_for_locks(locks: list[str]) -> set[str]:
    return set(core().file_paths_for_locks(locks))


def scan_workspace_repo(
    repo_path: str, include_tests: bool = False
) -> list[tuple]:
    """Whole-working-tree scan (ignore + rayon + GIL release)."""
    return core().scan_workspace_repo(repo_path, include_tests)


def plan_claimable(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    prepared = [
        (
            str(task["task_id"]),
            list(task["write_locks"]),
            list(task["ancestor_locks"]),
            str(task["status"]),
            int(task.get("priority", 0)),
            int(task.get("seq", 0)),
        )
        for task in tasks
    ]
    claimable, conflicts_result = core().plan_claimable(prepared)
    return {
        "engine": "rust",
        "claimable": claimable,
        "conflicts": [
            {"task_id": task_id, "blocked_by": blocked_by}
            for task_id, blocked_by in conflicts_result
        ],
    }


# ---------------------------------------------------------------------------
# Deterministic command supervisor
# ---------------------------------------------------------------------------
def run_command(
    command: str,
    cwd: str,
    timeout_seconds: float,
    extra_env: dict[str, str] | None,
    output_limit: int,
) -> dict[str, Any]:
    return core().run_command(
        command,
        cwd,
        float(timeout_seconds),
        extra_env,
        int(output_limit),
    )


__all__ = [
    "RUST_AVAILABLE",
    "available",
    "conflicts",
    "core",
    "describe",
    "engine_name",
    "expand_write_set",
    "file_paths_for_locks",
    "is_covered_by_write_set",
    "is_sub_write_set",
    "normalize",
    "plan_claimable",
    "promote_new_files",
    "run_command",
    "scan_workspace_repo",
    "version",
]
