"""One open submission per DAG task (stale-worker / duplicate-submit guard)."""

from __future__ import annotations

import pytest

from tests.test_integration import (
    CALC_FILES,
    _calc_task,
    _freeze_calc,
    _patch,
    kernel,
)

pytestmark = pytest.mark.integration


def test_a_second_open_submission_is_refused(tmp_path) -> None:
    # A slow gate keeps the first submission open long enough to observe the
    # guard deterministically (no sleep in the product path).
    slow_gate = {
        "VN_TEST_CMD": "sleep 4",
        "VN_COLLECT_CMD": "true",
        "VN_SKIP_TYPECHECK": "true",
        "VN_SKIP_LINT": "true",
    }
    with kernel(tmp_path, CALC_FILES, worker_env=slow_gate) as harness:
        _freeze_calc(harness)
        task = _calc_task(harness)
        bundle = harness.pull()
        assert bundle["leased"] is True
        token = bundle["lease_token"]
        first = _patch(
            harness.base, harness.bare, harness.head, "src/calc.py", "    ...", "    return a + b"
        )
        second = _patch(
            harness.base, harness.bare, harness.head, "src/calc.py", "    ...", "    return b + a"
        )

        def submit(diff: str, key: str):
            return harness.client.post(
                "/v2/syscalls/submit_patch",
                headers={
                    "Authorization": f"Bearer {harness.token}",
                    "Idempotency-Key": key,
                },
                json={
                    "task_id": task["task_id"],
                    "lease_token": token,
                    "git_diff": diff,
                    "touched_symbols": ["src/calc.py::add"],
                    "local_test_passed": True,
                },
            )

        accepted = submit(first, "guard-first")
        assert accepted.status_code == 202, accepted.text
        submission_id = accepted.json()["submission_id"]

        # Same patch, different key: the open submission is returned, not queued
        # twice.  This is what a stale worker or a lost ACK looks like.
        duplicate = submit(first, "guard-second")
        assert duplicate.status_code == 202, duplicate.text
        body = duplicate.json()
        assert body["submission_id"] == submission_id
        assert body["deduplicated"] is True

        # A different patch while the gate is still working would queue a
        # competing submission for one task: refused with the open id.
        conflicting = submit(second, "guard-third")
        assert conflicting.status_code == 409, conflicting.text
        detail = conflicting.json()["detail"]
        assert detail["error"] == "submission_in_progress"
        assert submission_id in detail["message"]

        queued = harness.client.get(
            f"/v2/submissions/{submission_id}",
            headers={"Authorization": f"Bearer {harness.token}"},
        ).json()
        assert queued["status"] in {"QUEUED", "RUNNING"}
