"""Rust core parity and sandbox tests (skipped if the extension is not built)."""

from __future__ import annotations

import sys
import time

import pytest

veynod_core = pytest.importorskip("veynod_core")

from app.engine import rust_bridge  # noqa: E402
from app.engine.lock_algebra import (  # noqa: E402
    _plan_claimable_python,
    plan_claimable,
)
from app.mcp_server.syscalls import LockAlgebra as ApiLockAlgebra  # noqa: E402
from app.services.ast_slicer import (  # noqa: E402
    ASTContextSlicer,
    RustContextSlicer,
    get_slicer,
)

SOURCE = '''"""module doc"""
import os


class A(Base):
    """A doc."""
    x: int = 1

    @staticmethod
    async def f(a: int, b: str = "x") -> int:
        """f doc."""
        return a + 1

    def g(self):
        def inner():
            return 2
        return inner()


@decorator
def h(x: int) -> int:
    """h doc"""
    y = x * 2
    return y


def stub(a: int) -> int:
    """stub doc"""
    ...
'''


def _py_expand(paths: list[str]) -> tuple[list[str], list[str]]:
    write: set[str] = set()
    ancestors: set[str] = set()
    for raw in paths:
        norm = ApiLockAlgebra.normalize(raw)
        write.add(norm)
        parts = norm.split("::")
        file_part = parts[0]
        directories = file_part.split("/")
        for index in range(1, len(directories)):
            ancestors.add("/".join(directories[:index]))
        ancestors.add(file_part)
        for index in range(1, len(parts)):
            ancestors.add("::".join(parts[: index + 1]))
    ancestors -= write
    return sorted(write), sorted(ancestors)


def test_lock_normalization_parity() -> None:
    corpus = [
        "src/a.py",
        "src/a.py::f",
        "src/a.py::C.m",
        "./src\\b.py::D::n",
        "src/order/pricing.py::PricingEngine.calc_discount",
        "tests/test_x.py::test_a",
    ]
    for path in corpus:
        assert veynod_core.normalize_symbol_path(path) == ApiLockAlgebra.normalize(path)

    for bad in ["../../etc/passwd", "src/a.py::../../evil", "   "]:
        with pytest.raises(ValueError):
            veynod_core.normalize_symbol_path(bad)


def test_lock_expansion_and_relations_parity() -> None:
    corpus = [
        "src/a.py::C::f",
        "src/a.py::C::g",
        "src/b.py",
        "tests/test_x.py::test_a",
    ]
    rust_write, rust_ancestors = veynod_core.expand_write_set(corpus)
    py_write, py_ancestors = _py_expand(corpus)
    assert rust_write == py_write
    assert rust_ancestors == py_ancestors

    assert veynod_core.is_sub_write_set(["src/a.py"], ["src/a.py::C::f"])
    assert veynod_core.is_sub_write_set(["src/a.py::C"], ["src/a.py::C::f"])
    assert not veynod_core.is_sub_write_set(["src/a.py::C"], ["src/b.py::C::f"])

    assert veynod_core.is_covered_by_write_set("src/a.py::C::f", ["src/a.py"])
    assert not veynod_core.is_covered_by_write_set("src/a.py::C::f", ["src/b.py"])

    assert not veynod_core.conflicts("src/a.py::C::f", "src/a.py::C::g")
    assert veynod_core.conflicts("src/a.py", "src/a.py::C::f")

    promoted = veynod_core.promote_new_files(
        ["tests/test_x.py::test_a", "tests/test_x.py::test_b"], set()
    )
    assert promoted == ["tests/test_x.py"]


def test_ast_signature_parity() -> None:
    python_slicer = ASTContextSlicer()
    for symbol in [
        "src/a.py",
        "src/a.py::A",
        "src/a.py::A::f",
        "src/a.py::h",
        "src/a.py::stub",
        "src/a.py::A::nope",
    ]:
        py_info = python_slicer.extract_artifact_signature(SOURCE, symbol)
        rs_info = veynod_core.signature(SOURCE, symbol)
        assert (
            py_info.skeleton,
            py_info.signature_hash,
            py_info.docstring,
            py_info.found,
        ) == rs_info


def test_ast_stub_and_references_parity() -> None:
    python_slicer = ASTContextSlicer()
    py_stubs = [
        (stub["symbol_path"], stub["signature"].signature_hash)
        for stub in python_slicer.discover_stubs(SOURCE, "src/a.py")
    ]
    rs_stubs = [
        (stub[0], stub[2]) for stub in veynod_core.discover_stubs(SOURCE, "src/a.py")
    ]
    assert py_stubs == rs_stubs

    for symbol in ["src/a.py::h", "src/a.py::A::g"]:
        assert sorted(python_slicer.extract_referenced_names(SOURCE, symbol)) == (
            veynod_core.referenced_names(SOURCE, symbol)
        )

    assert python_slicer.extract_node_source(SOURCE, "src/a.py::h") == (
        veynod_core.node_source(SOURCE, "src/a.py::h")
    )


def test_planner_parity() -> None:
    tasks = [
        {
            "task_id": "active-file",
            "write_locks": ["src/a.py"],
            "ancestor_locks": ["src"],
            "status": "LEASED",
            "priority": 1,
            "seq": 0,
        },
        {
            "task_id": "conflict",
            "write_locks": ["src/a.py::f"],
            "ancestor_locks": ["src", "src/a.py"],
            "status": "READY",
            "priority": 100,
            "seq": 1,
        },
        {
            "task_id": "ok",
            "write_locks": ["src/b.py::h"],
            "ancestor_locks": ["src", "src/b.py"],
            "status": "READY",
            "priority": 50,
            "seq": 2,
        },
    ]
    rust_plan = veynod_core.plan_claimable(
        [
            (
                task["task_id"],
                task["write_locks"],
                task["ancestor_locks"],
                task["status"],
                task["priority"],
                task["seq"],
            )
            for task in tasks
        ]
    )
    python_plan = _plan_claimable_python(tasks)
    assert list(rust_plan[0]) == python_plan["claimable"]
    assert sorted(rust_plan[1]) == sorted(
        (item["task_id"], item["blocked_by"]) for item in python_plan["conflicts"]
    )
    if rust_bridge.available():
        assert plan_claimable(tasks)["engine"] == "rust"


def test_slicer_backend_selection() -> None:
    if rust_bridge.available():
        assert isinstance(get_slicer(), RustContextSlicer)
    else:
        assert isinstance(get_slicer(), ASTContextSlicer)


def test_sandbox_echo_and_timeout() -> None:
    echo = veynod_core.run_command("echo rust-ok", ".", 10.0, None, 4000)
    assert echo["returncode"] == 0
    assert "rust-ok" in echo["output"]

    started = time.perf_counter()
    timeout = veynod_core.run_command(
        f'"{sys.executable}" -c "import time; time.sleep(5)"',
        ".",
        0.5,
        None,
        4000,
    )
    elapsed = time.perf_counter() - started
    assert timeout["returncode"] == 124
    assert timeout["timed_out"] is True
    assert elapsed < 5


def test_sandbox_output_is_bounded() -> None:
    result = veynod_core.run_command(
        f'"{sys.executable}" -c "print(\'x\' * 10000)"',
        ".",
        10.0,
        None,
        200,
    )
    assert result["returncode"] == 0
    assert "[output truncated" in result["output"]
    assert len(result["output"]) < 1000


def test_sandbox_kills_process_tree() -> None:
    # The parent spawns a child that inherits stdout and sleeps; only a process
    # group kill (Unix) / taskkill /T (Windows) lets the supervisor return.
    code = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)']);"
        "time.sleep(20)"
    )
    started = time.perf_counter()
    result = veynod_core.run_command(
        f'"{sys.executable}" -c "{code}"',
        ".",
        0.7,
        None,
        4000,
    )
    elapsed = time.perf_counter() - started
    assert result["returncode"] == 124
    assert result["timed_out"] is True
    assert elapsed < 5


def test_scheduler_core_claim_and_revert() -> None:
    if not hasattr(veynod_core, "SchedulerCore"):
        pytest.skip("installed veynod_core predates the authoritative scheduler core")
    core = veynod_core.SchedulerCore()
    core.upsert_tasks(
        [
            ("a", "ws", "IMPLEMENT_BODY", "LEASED", 1, 0, 1, ["src/a.py"], ["src"], None, None),
            (
                "b",
                "ws",
                "IMPLEMENT_BODY",
                "READY",
                100,
                1,
                2,
                ["src/a.py::f"],
                ["src", "src/a.py"],
                None,
                None,
            ),
            (
                "c",
                "ws",
                "IMPLEMENT_BODY",
                "READY",
                50,
                2,
                3,
                ["src/b.py::h"],
                ["src", "src/b.py"],
                None,
                None,
            ),
        ]
    )
    assert core.claim_next("ws", [], "runner", "t1", 100) == "c"
    assert core.claim_next("ws", [], "runner", "t2", 200) is None
    assert core.revert_claim("ws", "c", "t1") is True
    assert core.claim_next("ws", [], "runner", "t3", 300) == "c"
    stats = core.stats("ws")
    assert stats["tasks"] == 3
    assert stats["active"] == 2


def test_whole_repo_parallel_scanner(tmp_path) -> None:
    if not hasattr(veynod_core, "scan_workspace_repo"):
        pytest.skip("installed veynod_core predates the whole-repo scanner")
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.py").write_text(
        "def f(a: int) -> int:\n"
        "    'f'\n"
        "    ...\n"
        "\n"
        "def g():\n"
        "    return f(1)\n",
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text(
        "def test_f():\n    ...\n", encoding="utf-8"
    )

    scans = veynod_core.scan_workspace_repo(str(tmp_path), False)
    assert [scan[0] for scan in scans] == ["src/a.py"]
    assert any(stub[0] == "src/a.py::f" for stub in scans[0][1])
    assert scans[0][2] == ["src/a.py::f", "src/a.py::g"]
    assert scans[0][3]

    with_tests = veynod_core.scan_workspace_repo(str(tmp_path), True)
    assert {scan[0] for scan in with_tests} == {"src/a.py", "tests/test_a.py"}
