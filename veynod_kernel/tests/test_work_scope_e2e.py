"""Real PostgreSQL, Kernel lease snapshot, diff veto and deterministic gate."""

from __future__ import annotations

import pytest

from contracts.work_scope import WorkScope
from tests.test_integration import CALC_FILES, _freeze_calc, _patch, kernel

pytestmark = pytest.mark.integration


def test_frozen_scope_reaches_lease_and_vetoes_excluded_diff(tmp_path) -> None:
    files = {**CALC_FILES, "src/private/key.py": "secret = 'old'\n"}
    scope = WorkScope.from_dict({
        "scope_version": 1,
        "paths": {"include": ["src/**"], "exclude": ["src/private/**"]},
    })
    with kernel(tmp_path, files) as harness:
        _freeze_calc(harness)
        headers = {
            "X-Veynod-Service-Key": "integration-service-key",
            "X-Workspace-Id": harness.workspace_id,
        }
        body = {
            "request_key": "diagram:api:attempt-1",
            "title": "Implement add",
            "spec_prompt": "Implement add(a,b) so the frozen test passes.",
            "type": "IMPLEMENT_BODY",
            # Host delivery has a frozen path scope but may not predeclare an
            # AST target symbol; the Kernel must still accept and gate it.
            "target_symbols": [],
            "test_symbols": ["tests/test_calc.py::test_add"],
            "work_scope": scope.to_dict(),
        }
        created = harness.client.post("/v1/runtime/tasks", headers=headers, json=body)
        assert created.status_code == 201, created.text
        task_id = created.json()["task_id"]
        assert created.json()["scope_hash"] == scope.scope_hash
        widened = {**body, "work_scope": WorkScope.from_legacy_paths(["src/**", "tests/**"]).to_dict()}
        replay = harness.client.post("/v1/runtime/tasks", headers=headers, json=widened)
        assert replay.status_code == 409, replay.text

        bundle = harness.pull()
        assert bundle["leased"] and bundle["task_id"] == task_id
        assert bundle["work_scope"] == scope.to_dict()
        assert bundle["scope_hash"] == scope.scope_hash
        private_diff = _patch(
            harness.base, harness.bare, harness.head,
            "src/private/key.py", "old", "new",
        )
        rejected = harness.client.post(
            "/v2/syscalls/submit_patch",
            headers={"Authorization": f"Bearer {harness.token}"},
            json={
                "task_id": task_id,
                "lease_token": bundle["lease_token"],
                "git_diff": private_diff,
                "touched_symbols": ["src/private/key.py"],
                "local_test_passed": True,
            },
        )
        assert rejected.status_code == 403, rejected.text

        allowed_diff = _patch(
            harness.base, harness.bare, harness.head,
            "src/calc.py", "    ...", "    return a + b",
        )
        result = harness.submit(
            task_id, bundle["lease_token"], allowed_diff,
            ["src/calc.py::add"],
        )
        assert result["new_status"] == "COMMITTED", result
