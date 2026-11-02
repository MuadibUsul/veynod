from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.mcp_server.syscalls import (
    ActiveLeaseContext,
    SubmitPatchRequest,
    TaskStatus,
    _validate_submission,
)
from contracts.work_scope import WorkScope
from app.services.ast_slicer import ASTContextSlicer
from app.services.verification_gate import DeterministicVerificationGate


class _Engine:
    def __init__(self, scope: WorkScope, *, wrong_hash: bool = False) -> None:
        self.scope = scope
        self.wrong_hash = wrong_hash

    async def verify_active_lease(self, task_id, lease_token, worker_id):
        return ActiveLeaseContext(
            task_id=task_id,
            worker_hub_id=worker_id,
            write_set=["src"],
            work_scope=self.scope.to_dict(),
            scope_hash="0" * 64 if self.wrong_hash else self.scope.scope_hash,
            read_set_artifact_ids=[],
            status=TaskStatus.LEASED,
        )


def _request(path: str) -> SubmitPatchRequest:
    return SubmitPatchRequest(
        task_id=uuid4(),
        lease_token="vnl_valid_token_123456",
        git_diff=f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-a\n+b\n",
        touched_symbols=[path],
        local_test_passed=True,
    )


@pytest.mark.asyncio
async def test_submit_checks_frozen_scope_after_kernel_write_lock() -> None:
    scope = WorkScope.from_dict({
        "scope_version": 1,
        "paths": {"include": ["src/**"], "exclude": ["src/private/**"]},
    })
    lease, files = await _validate_submission(_Engine(scope), _request("src/public.py"), "runner")
    assert lease.scope_hash == scope.scope_hash
    assert files == {"src/public.py"}
    with pytest.raises(HTTPException) as error:
        await _validate_submission(_Engine(scope), _request("src/private/key.py"), "runner")
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_submit_rejects_scope_hash_mismatch() -> None:
    scope = WorkScope.from_legacy_paths(["src/**"])
    with pytest.raises(HTTPException) as error:
        await _validate_submission(_Engine(scope, wrong_hash=True), _request("src/public.py"), "runner")
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_submit_rejects_symbol_outside_narrower_scope() -> None:
    scope = WorkScope.from_dict({
        "scope_version": 1,
        "paths": {"include": ["src/**"]},
        "symbols": {"include": ["src/public.py::approved"]},
    })
    with pytest.raises(HTTPException) as error:
        await _validate_submission(_Engine(scope), _request("src/public.py"), "runner")
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_gate_derives_symbols_instead_of_trusting_runner_claim(tmp_path) -> None:
    source = (
        "def approved():\n    return 1\n\n"
        "def forbidden():\n    return 1\n"
    )
    changed = source.replace("def forbidden():\n    return 1", "def forbidden():\n    return 2")
    path = tmp_path / "src" / "public.py"
    path.parent.mkdir()
    path.write_text(changed, encoding="utf-8")
    scope = WorkScope.from_dict({
        "scope_version": 1,
        "paths": {"include": ["src/**"]},
        "symbols": {"include": ["src/public.py::approved"]},
    })

    class Repo:
        async def read_file(self, file_path, base_sha):
            return source

    gate = object.__new__(DeterministicVerificationGate)
    gate.slicer = ASTContextSlicer()
    task = SimpleNamespace(write_locks=["src"], lease_scope=scope.to_dict())
    ok, detail = await gate._check_ast_scope(
        Repo(), tmp_path, "base", task, ["src/public.py"]
    )
    assert not ok
    assert "forbidden" in detail and "WorkScope" in detail

    path.write_text(source.replace("def approved():\n    return 1", "def approved():\n    return 2"), encoding="utf-8")
    ok, detail = await gate._check_ast_scope(
        Repo(), tmp_path, "base", task, ["src/public.py"]
    )
    assert ok, detail


@pytest.mark.asyncio
async def test_path_scope_authorizes_new_file_under_ancestor_lock(tmp_path) -> None:
    path = tmp_path / "src" / "new.py"
    path.parent.mkdir()
    path.write_text("def created():\n    return 1\n", encoding="utf-8")
    scope = WorkScope.from_legacy_paths(["src/**"])

    class Repo:
        async def read_file(self, file_path, base_sha):
            return None

    gate = object.__new__(DeterministicVerificationGate)
    gate.slicer = ASTContextSlicer()
    task = SimpleNamespace(write_locks=["src"], lease_scope=scope.to_dict())
    ok, detail = await gate._check_ast_scope(
        Repo(), tmp_path, "base", task, ["src/new.py"]
    )
    assert ok, detail
