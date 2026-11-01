"""Hierarchical write-set lock algebra + a safe in-memory claim planner.

The pure functions prefer the Rust core (`veynod_core`) when it is installed;
the Python implementation below remains the reference and fallback. The
transactional claim itself stays in PostgreSQL (GIN + advisory lock); the Rust
planner is a read-only snapshot engine for diagnostics and large graph checks.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.engine import rust_bridge
from app.mcp_server.syscalls import LockAlgebra as ApiLockAlgebra


@dataclass(frozen=True)
class LockSet:
    write_locks: frozenset[str]
    ancestor_locks: frozenset[str]

    def conflicts_with(self, other: "LockSet") -> bool:
        return bool(
            (self.write_locks & other.write_locks)
            or (self.write_locks & other.ancestor_locks)
            or (self.ancestor_locks & other.write_locks)
        )


class LockAlgebra(ApiLockAlgebra):
    """Canonical paths + hierarchical intent locks."""

    @staticmethod
    def normalize(symbol_path: str) -> str:
        if rust_bridge.available():
            return rust_bridge.normalize(symbol_path)
        return ApiLockAlgebra.normalize(symbol_path)

    @classmethod
    def conflicts(cls, path_a: str, path_b: str) -> bool:
        if rust_bridge.available():
            return rust_bridge.conflicts(path_a, path_b)
        return ApiLockAlgebra.conflicts(path_a, path_b)

    @classmethod
    def is_covered_by_write_set(cls, target_path: str, allowed_write_set: list[str]) -> bool:
        if rust_bridge.available():
            return rust_bridge.is_covered_by_write_set(target_path, allowed_write_set)
        return ApiLockAlgebra.is_covered_by_write_set(target_path, allowed_write_set)

    @classmethod
    def extract_file_level_whitelist(cls, write_set: list[str]) -> set[str]:
        if rust_bridge.available():
            return rust_bridge.file_paths_for_locks(write_set)
        return ApiLockAlgebra.extract_file_level_whitelist(write_set)

    @classmethod
    def expand_write_set(cls, raw_paths: Iterable[str]) -> tuple[list[str], list[str]]:
        """
        Input:  ['src/order/pricing.py::PricingEngine.calc_discount']
        Output:
          write_locks:    ['src/order/pricing.py::PricingEngine::calc_discount']
          ancestor_locks: ['src', 'src/order', 'src/order/pricing.py',
                           'src/order/pricing.py::PricingEngine']
        """
        paths = list(raw_paths)
        if rust_bridge.available():
            return rust_bridge.expand_write_set(paths)

        write_set: set[str] = set()
        ancestor_set: set[str] = set()

        for raw in paths:
            norm = cls.normalize(raw)
            write_set.add(norm)
            parts = norm.split("::")
            file_part = parts[0]

            directories = file_part.split("/")
            for i in range(1, len(directories)):
                ancestor_set.add("/".join(directories[:i]))
            ancestor_set.add(file_part)
            for i in range(1, len(parts)):
                ancestor_set.add("::".join(parts[: i + 1]))

        ancestor_set -= write_set
        return sorted(write_set), sorted(ancestor_set)

    @classmethod
    def lock_set(cls, raw_paths: Iterable[str]) -> LockSet:
        write_locks, ancestor_locks = cls.expand_write_set(raw_paths)
        return LockSet(frozenset(write_locks), frozenset(ancestor_locks))

    @classmethod
    def is_sub_write_set(cls, parent_write_locks: Iterable[str], child_raw_paths: Iterable[str]) -> bool:
        """Every child path must equal a parent lock or descend from one."""
        parents = list(parent_write_locks)
        children = list(child_raw_paths)
        if rust_bridge.available():
            return rust_bridge.is_sub_write_set(parents, children)

        for raw in children:
            norm = cls.normalize(raw)
            covered = any(
                norm == parent
                or norm.startswith(f"{parent}::")
                or norm.startswith(f"{parent}/")
                for parent in parents
            )
            if not covered:
                return False
        return True

    @classmethod
    def promote_new_files(
        cls, raw_paths: Iterable[str], existing_files: set[str]
    ) -> list[str]:
        """
        A symbol-level lock on a file that does not exist yet is unsound: two
        tasks writing different symbols of the same new file would both pass the
        lock check and race to create the file. Promote such locks to file level.
        """
        paths = list(raw_paths)
        if rust_bridge.available():
            return rust_bridge.promote_new_files(paths, existing_files)

        promoted: set[str] = set()
        for raw in paths:
            norm = cls.normalize(raw)
            file_part = norm.split("::")[0]
            promoted.add(norm if file_part in existing_files else file_part)
        return sorted(promoted)

    @staticmethod
    def file_paths_for_locks(locks: Iterable[str]) -> set[str]:
        values = list(locks)
        if rust_bridge.available():
            return rust_bridge.file_paths_for_locks(values)
        return {lock.split("::", 1)[0] for lock in values}

    @staticmethod
    def symbols_conflict(left: Iterable[str], right: Iterable[str]) -> bool:
        return LockAlgebra.lock_set(left).conflicts_with(LockAlgebra.lock_set(right))


def _plan_claimable_python(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    active: list[tuple[str, LockSet]] = []
    for task in tasks:
        if task["status"] in {"LEASED", "VERIFYING"}:
            active.append(
                (
                    str(task["task_id"]),
                    LockSet(
                        frozenset(task["write_locks"]),
                        frozenset(task["ancestor_locks"]),
                    ),
                )
            )

    ready = [task for task in tasks if task["status"] == "READY"]
    ready.sort(
        key=lambda task: (-int(task.get("priority", 0)), int(task.get("seq", 0)))
    )

    claimable: list[str] = []
    conflicts: list[dict[str, Any]] = []
    for task in ready:
        locks = LockSet(
            frozenset(task["write_locks"]),
            frozenset(task["ancestor_locks"]),
        )
        blockers = [
            task_id for task_id, active_locks in active if locks.conflicts_with(active_locks)
        ]
        if blockers:
            conflicts.append({"task_id": str(task["task_id"]), "blocked_by": blockers})
        else:
            claimable.append(str(task["task_id"]))
            active.append((str(task["task_id"]), locks))

    return {
        "engine": "python",
        "claimable": claimable,
        "conflicts": conflicts,
    }


def plan_claimable(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the maximal concurrently-claimable set from a snapshot."""
    if rust_bridge.available():
        return rust_bridge.plan_claimable(tasks)
    return _plan_claimable_python(tasks)


__all__ = ["LockAlgebra", "LockSet", "plan_claimable"]
