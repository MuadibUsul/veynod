from __future__ import annotations

from types import SimpleNamespace

from app.services.code_engine import (
    KernelEngineClient,
    cancel_kernel_run,
    event_task_ids,
    gate_updates_from_evidence,
    lifecycle_action,
    merge_kernel_workspace_ids,
)

COMMIT = "a" * 40


def _evidence(*, tests_passed: bool = True, complete: bool = True) -> dict:
    results = [
        {"stage": "WRITE_SET_BOUNDARY", "passed": True},
        {"stage": "AST_SCOPE_DIFF", "passed": True},
        {"stage": "CONTRACT_HASH_GUARD", "passed": True},
        {"stage": "STATIC_TYPECHECK", "passed": True},
        {"stage": "UNIT_TEST_SUITE", "passed": tests_passed},
    ]
    return {
        "task_id": "task-1",
        "task_status": "COMMITTED" if complete else "VERIFYING",
        "result_commit_sha": COMMIT,
        "submission": {
            "submission_id": "sub-1",
            "status": "COMMITTED" if complete else "RUNNING",
            "result_commit_sha": COMMIT,
        },
        "merge_intent": {
            "merge_id": "merge-1",
            "status": "MERGED" if complete else "PENDING",
            "merged_commit_sha": COMMIT if complete else None,
        },
        "gate_results": results if complete else [],
        "evidence_complete": complete,
    }


def test_complete_evidence_maps_to_ci_and_tests() -> None:
    updates = gate_updates_from_evidence(_evidence(), COMMIT)
    by_kind = {item["gate_kind"]: item for item in updates}
    assert set(by_kind) == {"CI", "TESTS"}
    assert by_kind["CI"]["status"] == "PASS"
    assert by_kind["CI"]["evidence"]["counts"] == {"passed": 4, "failed": 0}
    assert by_kind["TESTS"]["status"] == "PASS"
    assert by_kind["TESTS"]["evidence"]["commit_sha"] == COMMIT
    assert by_kind["TESTS"]["evidence"]["run_url"].endswith("/sub-1")


def test_failed_unit_test_is_not_a_pass() -> None:
    updates = gate_updates_from_evidence(_evidence(tests_passed=False), COMMIT)
    by_kind = {item["gate_kind"]: item for item in updates}
    assert by_kind["CI"]["status"] == "PASS"
    assert by_kind["TESTS"]["status"] == "FAIL"
    assert by_kind["TESTS"]["evidence"]["counts"] == {"passed": 0, "failed": 1}


def test_incomplete_evidence_never_becomes_pass() -> None:
    assert gate_updates_from_evidence(_evidence(complete=False), COMMIT) == []


def test_commit_mismatch_never_becomes_pass() -> None:
    assert gate_updates_from_evidence(_evidence(), "b" * 40) == []


def test_missing_gate_results_never_becomes_pass() -> None:
    evidence = _evidence()
    evidence["gate_results"] = []
    assert gate_updates_from_evidence(evidence, COMMIT) == []


async def test_cancel_kernel_run_propagates_to_admin_api(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_cancel(self, task_id: str, reason: str):
        calls.append((task_id, reason))
        return {"status": "CANCELLED"}

    monkeypatch.setattr(KernelEngineClient, "cancel_task", fake_cancel)
    task = SimpleNamespace(
        input_context={
            "code_engine": "veynod-kernel",
            "kernel": {"workspace_id": "ws-1", "task_id": "kernel-1"},
        }
    )
    assert await cancel_kernel_run(task, "stop") is True
    assert calls == [("kernel-1", "stop")]


async def test_cancel_kernel_run_ignores_non_kernel_tasks(monkeypatch) -> None:
    async def fake_cancel(self, task_id: str, reason: str):
        raise AssertionError("must not call the Kernel for a non-code task")

    monkeypatch.setattr(KernelEngineClient, "cancel_task", fake_cancel)
    task = SimpleNamespace(input_context={"code_engine": "other"})
    assert await cancel_kernel_run(task, "stop") is False


def test_lifecycle_projection_actions() -> None:
    assert lifecycle_action("CANCELLED", "READY") == "cancel"
    assert lifecycle_action("FAILED", "READY") == "fail"
    assert lifecycle_action("COMMITTED", "READY") == "commit"
    assert lifecycle_action("LEASED", "READY") is None
    assert lifecycle_action("CANCELLED", "REVIEWING") is None
    assert lifecycle_action("FAILED", "DONE") is None
    assert lifecycle_action("COMMITTED", "REVIEWING") is None


def test_event_task_ids_deduplicates_and_skips_payloads_without_task() -> None:
    events = [
        {"payload": {"task_id": "t-1"}},
        {"payload": {"task_id": "t-1"}},
        {"payload": {"task_id": "t-2"}},
        {"payload": {"token_id": "token-1"}},
        {"payload": None},
    ]
    assert event_task_ids(events) == ["t-1", "t-2"]


def test_kernel_workspace_ids_merge_and_deduplicate() -> None:
    assert merge_kernel_workspace_ids(
        "ws-1", ["ws-2", "ws-1", ""]
    ) == ["ws-1", "ws-2"]
    assert merge_kernel_workspace_ids(None, []) == []
