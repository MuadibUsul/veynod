"""Multi-workspace / multi-tenant isolation with real database state."""

from __future__ import annotations

import pytest

from tests.test_integration import CALC_FILES, _calc_task, _freeze_calc, _patch, kernel

pytestmark = pytest.mark.integration


def _second_workspace(harness) -> tuple[str, str]:
    workspace = harness.admin(
        "POST",
        "/v1/admin/workspaces",
        {"name": "other", "repo_path": str(harness.bare), "main_branch": "main"},
    )
    token = harness.admin(
        "POST",
        "/v1/admin/tokens",
        {"label": "other-runner", "workspace_id": workspace["workspace_id"]},
    )["token"]
    return workspace["workspace_id"], token


def test_a_runner_cannot_read_another_workspaces_task_evidence_or_files(tmp_path) -> None:
    with kernel(tmp_path, CALC_FILES) as harness:
        _freeze_calc(harness)
        task = _calc_task(harness)
        other_ws, other_token = _second_workspace(harness)
        assert other_ws != harness.workspace_id
        auth = {"Authorization": f"Bearer {other_token}"}

        assert harness.client.get(
            f"/v1/runner/tasks/{task['task_id']}", headers=auth
        ).status_code == 404
        assert harness.client.get(
            f"/v1/runner/tasks/{task['task_id']}/evidence", headers=auth
        ).status_code == 404
        assert harness.client.get(
            f"/v1/runner/workspaces/{harness.workspace_id}/file",
            headers=auth,
            params={"path": "src/calc.py"},
        ).status_code == 404
        # An explicit route to the other workspace is refused, not silently
        # redirected to the token's own workspace.
        cross = harness.client.post(
            "/v1/syscalls/pull_task_bundle",
            headers={**auth, "X-Workspace-Id": harness.workspace_id},
            json={"worker_hub_id": "other"},
        )
        assert cross.status_code == 403, cross.text


def test_a_patch_outside_the_write_set_is_refused(tmp_path) -> None:
    with kernel(tmp_path, CALC_FILES) as harness:
        _freeze_calc(harness)
        task = _calc_task(harness)
        bundle = harness.pull()
        diff = _patch(
            harness.base,
            harness.bare,
            harness.head,
            "tests/test_calc.py",
            "assert add(2, 3) == 5",
            "assert add(2, 3) == 6",
        )
        response = harness.client.post(
            "/v2/syscalls/submit_patch",
            headers={"Authorization": f"Bearer {harness.token}"},
            json={
                "task_id": task["task_id"],
                "lease_token": bundle["lease_token"],
                "git_diff": diff,
                "touched_symbols": ["tests/test_calc.py::test_add"],
                "local_test_passed": True,
            },
        )
        assert response.status_code == 403, response.text
        assert "write_set" in response.text or "Symbol Violation" in response.text


def test_admin_and_service_planes_reject_unknown_callers(tmp_path) -> None:
    with kernel(tmp_path, CALC_FILES) as harness:
        assert harness.client.get("/v1/admin/tasks").status_code in {401, 403}
        assert harness.client.post(
            "/v1/admin/workspaces",
            json={"name": "x", "repo_path": str(harness.bare)},
        ).status_code in {401, 403}
        assert harness.client.get(
            "/v1/runtime/events",
            params={"workspace_id": harness.workspace_id},
            headers={"X-Veynod-Service-Key": "wrong"},
        ).status_code == 401
        assert harness.client.post(
            "/v1/runtime/tasks",
            headers={"X-Veynod-Service-Key": "wrong",
                     "X-Workspace-Id": harness.workspace_id},
            json={"title": "x", "spec_prompt": "y" * 20},
        ).status_code == 401
