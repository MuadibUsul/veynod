from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.mcp_server.syscalls import (
    LockAlgebra,
    RequestTaskSplitRequest,
    SubmitPatchRequest,
)


def _split_request(sub_tasks: list[dict]) -> RequestTaskSplitRequest:
    return RequestTaskSplitRequest.model_validate(
        {
            "task_id": "00000000-0000-0000-0000-000000000001",
            "lease_token": "x" * 20,
            "split_reason": "Estimated 350 LOC across three state machines.",
            "sub_tasks": sub_tasks,
        }
    )


def test_valid_split_dag_is_accepted() -> None:
    request = _split_request(
        [
            {
                "local_key": "a1",
                "task_type": "IMPLEMENT_BODY",
                "specification": "Implement part A thoroughly.",
                "write_set_symbols": ["src/x.py::f"],
            },
            {
                "local_key": "b1",
                "task_type": "WRITE_TEST",
                "specification": "Write tests for part A.",
                "write_set_symbols": ["tests/test_x.py::test_f"],
                "depends_on_local_keys": ["a1"],
            },
        ]
    )
    assert len(request.sub_tasks) == 2


def test_cycle_is_rejected_by_kahn_validation() -> None:
    with pytest.raises(ValidationError, match="Cycle detected"):
        _split_request(
            [
                {
                    "local_key": "a1",
                    "task_type": "IMPLEMENT_BODY",
                    "specification": "Implement part A thoroughly.",
                    "write_set_symbols": ["src/x.py::f"],
                    "depends_on_local_keys": ["b1"],
                },
                {
                    "local_key": "b1",
                    "task_type": "IMPLEMENT_BODY",
                    "specification": "Implement part B thoroughly.",
                    "write_set_symbols": ["src/x.py::g"],
                    "depends_on_local_keys": ["a1"],
                },
            ]
        )


def test_unordered_siblings_may_not_share_a_write_hierarchy() -> None:
    with pytest.raises(ValidationError, match="conflicting write paths"):
        _split_request(
            [
                {
                    "local_key": "a1",
                    "task_type": "IMPLEMENT_BODY",
                    "specification": "Implement part A thoroughly.",
                    "write_set_symbols": ["src/x.py"],
                },
                {
                    "local_key": "b1",
                    "task_type": "IMPLEMENT_BODY",
                    "specification": "Implement part B thoroughly.",
                    "write_set_symbols": ["src/x.py::f"],
                },
            ]
        )


def test_ordered_siblings_may_overlap_because_they_are_serialized() -> None:
    request = _split_request(
        [
            {
                "local_key": "a1",
                "task_type": "DESIGN_CONTRACT",
                "specification": "Define the interface first.",
                "write_set_symbols": ["src/x.py"],
            },
            {
                "local_key": "b1",
                "task_type": "IMPLEMENT_BODY",
                "specification": "Implement the interface second.",
                "write_set_symbols": ["src/x.py::f"],
                "depends_on_local_keys": ["a1"],
            },
        ]
    )
    assert request.sub_tasks[1].depends_on_local_keys == ["a1"]


def test_submit_patch_normalizes_touched_symbols() -> None:
    request = SubmitPatchRequest.model_validate(
        {
            "task_id": "00000000-0000-0000-0000-000000000001",
            "lease_token": "x" * 20,
            "git_diff": "--- a/src/x.py\n+++ b/src/x.py\n",
            "touched_symbols": ["src/x.py::A.f"],
            "local_test_passed": True,
        }
    )
    assert request.touched_symbols == ["src/x.py::A::f"]


def test_file_level_write_set_covers_symbol_targets() -> None:
    assert LockAlgebra.is_covered_by_write_set(
        "src/a.py::Class::method", ["src/a.py"]
    )
    assert not LockAlgebra.is_covered_by_write_set(
        "src/a.py::Class::method", ["src/b.py"]
    )
