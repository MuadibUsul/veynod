"""Write fencing: a worker whose lease was taken over cannot write.

Reproduces the race from the qualification brief:

    Worker A claims (epoch 1) -> blocks in a slow model call
    lease expires -> Worker B claims (epoch 2)
    A returns -> A's writes must fail, B's must win
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from agent_runtime import LoopController, ModelAction, RunStore, ToolResult
from agent_runtime.errors import LeaseLost

URL = os.environ.get("RUNTIME_TEST_DATABASE_URL") or os.environ.get(
    "TEST_DATABASE_URL"
)
pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def store():
    if not URL:
        pytest.skip("RUNTIME_TEST_DATABASE_URL/TEST_DATABASE_URL is not set")
    name = URL.rsplit("/", 1)[-1].split("?")[0].lower()
    if "test" not in name and os.environ.get("VEYNOD_ALLOW_DESTRUCTIVE_TESTS") != "1":
        pytest.skip("runtime tests require a disposable database")
    runtime = RunStore(URL)
    await runtime.init_schema(reset=True)
    try:
        yield runtime
    finally:
        await runtime.close()


async def _running(store: RunStore) -> dict:
    run = await store.create_run(
        tenant_id=str(uuid4()),
        workspace_id=str(uuid4()),
        product_task_id="fence-task",
    )
    for target in ("QUEUED", "INITIALIZING", "PLANNING", "READY", "RUNNING"):
        await store.transition(run["run_id"], target, expected_version=run["version"])
        run = await store.get_run(run["run_id"])
    return run


async def _expire_lease(store: RunStore, run_id: str) -> None:
    async with store._sessions() as session:  # noqa: SLF001 - fault injection
        await session.execute(
            text(
                f"UPDATE {store.schema}.agent_runs "
                "SET lease_expires_at = NOW() - INTERVAL '1 second' "
                "WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        )
        await session.commit()


class _BlockingModel:
    def __init__(self, gate: asyncio.Event) -> None:
        self.gate = gate
        self.entered = asyncio.Event()

    async def decide(self, run, context):
        self.entered.set()
        await self.gate.wait()
        return ModelAction(kind="finish", output={"by": "stale-worker"})


class _EchoTool:
    async def execute(self, name, arguments, idempotency_key):
        return ToolResult(True, {"ok": True})


@pytest.mark.asyncio
async def test_claim_increments_the_fencing_epoch(store: RunStore) -> None:
    await _running(store)
    first = await store.claim_run("worker-a", lease_seconds=30)
    assert first is not None
    await _expire_lease(store, first["run"]["run_id"])
    second = await store.claim_run("worker-b", lease_seconds=30)
    assert second is not None
    assert second["lease_epoch"] == first["lease_epoch"] + 1


@pytest.mark.asyncio
async def test_stale_worker_cannot_transition_or_write(store: RunStore) -> None:
    run = await _running(store)
    run_id = run["run_id"]
    first = await store.claim_run("worker-a", lease_seconds=30)
    epoch_a = int(first["lease_epoch"])
    await _expire_lease(store, run_id)
    second = await store.claim_run("worker-b", lease_seconds=30)
    epoch_b = int(second["lease_epoch"])
    assert epoch_b > epoch_a

    current = await store.get_run(run_id)
    with pytest.raises(LeaseLost):
        await store.transition(
            run_id,
            "COMPLETED",
            expected_version=current["version"],
            reason="stale worker finished",
            fence=epoch_a,
        )
    with pytest.raises(LeaseLost):
        await store.checkpoint(run_id, {"who": "stale"}, fence=epoch_a)
    with pytest.raises(LeaseLost):
        await store.record_step(
            run_id, 1, "MODEL", "SUCCEEDED", {"who": "stale"}, fence=epoch_a
        )
    with pytest.raises(LeaseLost):
        await store.add_usage(run_id, fence=epoch_a, model_calls=1)

    # Nothing from the stale worker was persisted.
    assert await store.list_checkpoints(run_id) == []
    assert await store.list_steps(run_id, 10) == []

    # The live worker still owns the Run.
    current = await store.get_run(run_id)
    done = await store.transition(
        run_id,
        "COMPLETED",
        expected_version=current["version"],
        reason="live worker finished",
        fence=epoch_b,
    )
    assert done["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_slow_model_loses_write_ownership_mid_step(store: RunStore) -> None:
    run = await _running(store)
    run_id = run["run_id"]
    gate = asyncio.Event()
    model = _BlockingModel(gate)
    controller_a = LoopController(
        store, model, _EchoTool(), fence=int((await store.claim_run("worker-a", 30))["lease_epoch"])
    )

    step = asyncio.create_task(controller_a.step(run_id))
    await asyncio.wait_for(model.entered.wait(), timeout=5)

    await _expire_lease(store, run_id)
    second = await store.claim_run("worker-b", lease_seconds=30)
    assert second is not None

    gate.set()
    with pytest.raises(LeaseLost):
        await asyncio.wait_for(step, timeout=10)

    # The stale worker left no model step behind.
    kinds = [row["kind"] for row in await store.list_steps(run_id, 10)]
    assert "MODEL" not in kinds
    assert (await store.get_run(run_id))["status"] == "RUNNING"


@pytest.mark.asyncio
async def test_deferred_submission_from_stale_worker_is_rejected(store: RunStore) -> None:
    run = await _running(store)
    run_id = run["run_id"]
    claim = await store.claim_run("worker-a", 30)
    epoch_a = int(claim["lease_epoch"])
    await _expire_lease(store, run_id)
    await store.claim_run("worker-b", 30)

    with pytest.raises(LeaseLost):
        await store.defer_external(
            run_id,
            tool="submit_patch",
            idempotency_key="stale-submit",
            submission_id=str(uuid4()),
            fence=epoch_a,
        )
    assert await store.list_external(run_id) == []


@pytest.mark.asyncio
async def test_a_run_left_in_waiting_tool_is_recovered_on_claim(store: RunStore) -> None:
    """A worker killed inside a tool call must not strand the Run."""
    run = await _running(store)
    run_id = run["run_id"]
    first = await store.claim_run("worker-a", 30)
    assert first is not None
    current = await store.get_run(run_id)
    await store.transition(
        run_id,
        "WAITING_TOOL",
        expected_version=current["version"],
        reason="tool submit_patch",
        fence=int(first["lease_epoch"]),
    )
    await _expire_lease(store, run_id)

    second = await store.claim_run("worker-b", 30)
    assert second is not None
    assert second["recovered_from"] == "WAITING_TOOL"
    assert second["run"]["status"] == "RUNNING"
    assert second["lease_epoch"] > int(first["lease_epoch"])

    # The new worker can make progress with its own fencing token.
    current = await store.get_run(run_id)
    done = await store.transition(
        run_id,
        "COMPLETED",
        expected_version=current["version"],
        reason="recovered worker finished",
        fence=int(second["lease_epoch"]),
    )
    assert done["status"] == "COMPLETED"
