from __future__ import annotations

import pytest

from app.engine.lock_algebra import LockAlgebra
from app.mcp_server.syscalls import DiffBoundaryInspector
from app.mcp_server.syscalls import LockAlgebra as ApiLockAlgebra


def test_normalize_dotted_symbol_segments() -> None:
    assert (
        ApiLockAlgebra.normalize("src/order/pricing.py::PricingEngine.calc_discount")
        == "src/order/pricing.py::PricingEngine::calc_discount"
    )
    assert (
        ApiLockAlgebra.normalize("./src\\auth.py::AuthService::login")
        == "src/auth.py::AuthService::login"
    )


def test_normalize_rejects_traversal_and_absolute_escape() -> None:
    with pytest.raises(ValueError):
        ApiLockAlgebra.normalize("../../etc/passwd")
    with pytest.raises(ValueError):
        ApiLockAlgebra.normalize("src/a.py::../../evil")
    with pytest.raises(ValueError):
        ApiLockAlgebra.normalize("   ")


def test_expand_write_set_builds_hierarchical_intent_locks() -> None:
    write_locks, ancestors = LockAlgebra.expand_write_set(
        ["src/order/pricing.py::PricingEngine.calc_discount"]
    )
    assert write_locks == ["src/order/pricing.py::PricingEngine::calc_discount"]
    assert "src" in ancestors
    assert "src/order" in ancestors
    assert "src/order/pricing.py" in ancestors
    assert "src/order/pricing.py::PricingEngine" in ancestors
    assert "src/order/pricing.py::PricingEngine::calc_discount" not in ancestors


def test_disjoint_symbols_in_one_file_do_not_conflict() -> None:
    left = LockAlgebra.lock_set(["src/auth.py::AuthService::login"])
    right = LockAlgebra.lock_set(["src/auth.py::AuthService::logout"])
    assert not left.conflicts_with(right)


def test_file_lock_conflicts_with_symbol_lock() -> None:
    file_lock = LockAlgebra.lock_set(["src/auth.py"])
    symbol_lock = LockAlgebra.lock_set(["src/auth.py::AuthService::login"])
    assert file_lock.conflicts_with(symbol_lock)
    assert symbol_lock.conflicts_with(file_lock)


def test_class_lock_conflicts_with_its_method() -> None:
    class_lock = LockAlgebra.lock_set(["src/auth.py::AuthService"])
    method_lock = LockAlgebra.lock_set(["src/auth.py::AuthService::login"])
    assert class_lock.conflicts_with(method_lock)


def test_is_sub_write_set() -> None:
    parent = ["src/auth.py::AuthService"]
    assert LockAlgebra.is_sub_write_set(parent, ["src/auth.py::AuthService::login"])
    assert LockAlgebra.is_sub_write_set(["src/auth.py"], ["src/auth.py::AuthService"])
    assert not LockAlgebra.is_sub_write_set(parent, ["src/other.py::X"])


def test_promote_new_files_serializes_creation_of_the_same_new_file() -> None:
    promoted = LockAlgebra.promote_new_files(
        ["tests/test_x.py::test_a", "tests/test_x.py::test_b"], set()
    )
    assert promoted == ["tests/test_x.py"]

    kept = LockAlgebra.promote_new_files(
        ["tests/test_x.py::test_a", "tests/test_x.py::test_b"],
        {"tests/test_x.py"},
    )
    assert kept == ["tests/test_x.py::test_a", "tests/test_x.py::test_b"]


def test_diff_inspector_extracts_and_guards_files() -> None:
    diff = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1 @@
-...
+return 1
"""
    assert DiffBoundaryInspector.extract_touched_files(diff) == {"src/a.py"}
    assert DiffBoundaryInspector.assert_within_write_set(
        diff, ["src/a.py::f"]
    ) == {"src/a.py"}

    with pytest.raises(PermissionError):
        DiffBoundaryInspector.assert_within_write_set(diff, ["src/b.py::g"])


def test_diff_inspector_rejects_diff_without_headers() -> None:
    with pytest.raises(ValueError):
        DiffBoundaryInspector.assert_within_write_set("not a diff", ["src/a.py"])


def test_quoted_diff_paths_are_unquoted() -> None:
    diff = '--- "a/src/a b.py"\n+++ "b/src/a b.py"\n'
    assert DiffBoundaryInspector.extract_touched_files(diff) == {"src/a b.py"}
