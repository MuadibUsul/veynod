from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from app.config import settings
from app.services import gate_worker as gw


class _FakeResult:
    def __init__(self, row: dict | None) -> None:
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class _FakeSession:
    def __init__(self, row: dict | None) -> None:
        self.row = row

    async def execute(self, *args, **kwargs):
        return _FakeResult(self.row)

    async def commit(self) -> None:
        return None


def _scope(row: dict | None):
    @asynccontextmanager
    async def _fake_scope():
        yield _FakeSession(row)

    return _fake_scope


async def _run_watcher_case(
    monkeypatch, row: dict | None, lease_hash: str
) -> tuple[asyncio.Event, asyncio.Event]:
    worker = gw.GateWorker("cancel-watch-test")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_gate(record, lost):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def renew(record) -> bool:
        return True

    monkeypatch.setattr(worker, "_process_with_gate", slow_gate)
    monkeypatch.setattr(worker, "renew", renew)
    monkeypatch.setattr(gw, "session_scope", _scope(row))
    monkeypatch.setattr(settings, "cancel_poll_seconds", 0.05)

    record = {
        "submission_id": uuid4(),
        "task_id": uuid4(),
        "attempts": 1,
        "lease_token_hash": lease_hash,
    }
    await asyncio.wait_for(worker.process(record), timeout=3)
    return started, cancelled


@pytest.mark.asyncio
async def test_gate_worker_watcher_cancels_cancelled_task(monkeypatch) -> None:
    started, cancelled = await _run_watcher_case(
        monkeypatch,
        {"status": "CANCELLED", "lease_token_hash": "hash"},
        "hash",
    )
    assert started.is_set()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_gate_worker_watcher_cancels_lost_lease(monkeypatch) -> None:
    started, cancelled = await _run_watcher_case(
        monkeypatch,
        {"status": "VERIFYING", "lease_token_hash": "new-hash"},
        "old-hash",
    )
    assert started.is_set()
    assert cancelled.is_set()
