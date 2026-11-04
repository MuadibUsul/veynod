"""A submission whose task lease is gone releases the task before it becomes final.

Regression for a CI race: the worker used to commit the submission as final
FAILED and only then release the task, so a client that saw the final
submission could still read the task as LEASED.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.services import gate_worker as gw


class _FakeSession:
    def __init__(self, task) -> None:
        self.task = task

    async def get(self, model, key):
        return self.task

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _scope(task):
    @asynccontextmanager
    async def _fake_scope():
        yield _FakeSession(task)

    return _fake_scope


def _record(lease_hash: str = "hash-a", runner_id: str = "runner-a") -> dict:
    return {
        "submission_id": uuid4(),
        "task_id": uuid4(),
        "lease_token_hash": lease_hash,
        "runner_id": runner_id,
        "work_lease_id": uuid4(),
    }


def _task(*, lease_hash: str = "hash-a", runner_id: str = "runner-a", expired: bool = True):
    now = datetime.now(UTC)
    return SimpleNamespace(
        status=SimpleNamespace(value="LEASED"),
        lease_expires_at=now - timedelta(minutes=1) if expired else now + timedelta(minutes=5),
        lease_token_hash=lease_hash,
        leased_by_runner=runner_id,
    )


async def _run(monkeypatch, task, *, released: bool) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []
    worker = gw.GateWorker("lease-release-test")

    async def release_task(task_id, lease_hash, reason):
        calls.append(("release", {"lease_hash": lease_hash, "reason": reason}))
        return released

    async def finalize(record, **kwargs):
        calls.append(("finalize", kwargs))
        return True

    monkeypatch.setattr(gw, "session_scope", _scope(task))
    monkeypatch.setattr(worker, "release_task", release_task)
    monkeypatch.setattr(worker, "_finalize", finalize)
    await worker._process_with_gate(_record(), None)
    return calls


@pytest.mark.asyncio
async def test_an_expired_lease_releases_the_task_before_the_submission_is_final(monkeypatch):
    calls = await _run(monkeypatch, _task(expired=True), released=True)
    assert [name for name, _ in calls] == ["release", "finalize"]
    assert calls[0][1]["lease_hash"] == "hash-a"
    final = calls[1][1]
    assert final["status"] == "FAILED"
    assert final["error_code"] == "lease_expired"
    assert final["task_status"] == "READY"


@pytest.mark.asyncio
async def test_a_task_claimed_by_another_runner_is_left_alone_and_recorded_as_is(monkeypatch):
    # The release is fenced by this submission's lease hash; when it does not
    # apply, the record must not claim the task went back to READY.
    calls = await _run(
        monkeypatch,
        _task(lease_hash="hash-b", runner_id="runner-b", expired=False),
        released=False,
    )
    assert [name for name, _ in calls] == ["release", "finalize"]
    final = calls[1][1]
    assert final["error_code"] == "lease_lost"
    assert final["task_status"] == "LEASED"
