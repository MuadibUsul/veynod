"""Durable external wait: defer, wake, resume, expire (ADR-014)."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
import pytest_asyncio

from agent_runtime import (
    LoopController,
    ModelAction,
    RunState,
    RunStore,
    ToolResult,
)

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
        product_task_id="task-1",
        trace_id="trace-1",
        kernel={"base_url": "http://kernel.test", "workspace_id": str(uuid4())},
    )
    for target in ("QUEUED", "INITIALIZING", "PLANNING", "READY", "RUNNING"):
        await store.transition(
            run["run_id"], target, expected_version=run["version"]
        )
        run = await store.get_run(run["run_id"])
    return run


class _DeferringTool:
    def __init__(self, submission_id: str) -> None:
        submission_id = submission_id
        self.submission_id = submission_id
        self.calls = 0

    async def execute(self, name, arguments, idempotency_key):
        self.calls += 1
        return ToolResult(
            True,
            {"accepted": True, "submission_id": self.submission_id},
            deferred={
                "submission_id": self.submission_id,
                "kernel_workspace_id": "ws",
                "kernel_task_id": "kt",
                "deadline_seconds": 600,
            },
        )


class _ScriptedModel:
    def __init__(self, actions):
        self.actions = list(actions)

    async def decide(self, run, context):
        return self.actions.pop(0)


def _submit_action() -> ModelAction:
    return ModelAction(
        kind="tool",
        tool="submit_patch",
        arguments={"git_diff": "diff --git a/x b/x", "touched_symbols": ["x.py"]},
        idempotency_key="submit-1",
    )


@pytest.mark.asyncio
async def test_deferred_tool_parks_the_run_and_releases_the_lease(store: RunStore) -> None:
    run = await _running(store)
    claim = await store.claim_run("worker-1", lease_seconds=30)
    assert claim is not None
    tools = _DeferringTool("11111111-1111-1111-1111-111111111111")
    controller = LoopController(
        store, _ScriptedModel([_submit_action()]), tools
    )

    result = await controller.step(run["run_id"])

    assert result["deferred"] is True
    assert result["run"]["status"] == RunState.WAITING_EXTERNAL.value
    external = await store.list_external(run["run_id"])
    assert len(external) == 1
    assert external[0]["status"] == "PENDING"
    assert external[0]["submission_id"] == "11111111-1111-1111-1111-111111111111"
    # Not claimable while waiting.
    assert await store.claim_run("worker-2", lease_seconds=30) is None


@pytest.mark.asyncio
async def test_wakeup_resumes_once_and_is_idempotent(store: RunStore) -> None:
    run = await _running(store)
    await store.claim_run("worker-1", lease_seconds=30)
    controller = LoopController(
        store, _ScriptedModel([_submit_action()]), _DeferringTool("22222222-2222-2222-2222-222222222222")
    )
    await controller.step(run["run_id"])

    payload = {
        "submission_id": "22222222-2222-2222-2222-222222222222",
        "status": "COMMITTED",
        "result_commit_sha": "abc123",
    }
    assert await store.add_wakeup(
        kind="SUBMISSION_FINAL", ref_id="22222222-2222-2222-2222-222222222222", kernel_event_seq=10, payload=payload
    )
    # A duplicate delivery of the same event is ignored.
    assert not await store.add_wakeup(
        kind="SUBMISSION_FINAL", ref_id="22222222-2222-2222-2222-222222222222", kernel_event_seq=10, payload=payload
    )

    stats = await store.consume_wakeups()
    assert stats["resumed"] == 1
    resumed = await store.get_run(run["run_id"])
    assert resumed["status"] == RunState.RUNNING.value
    checkpoints = await store.list_checkpoints(run["run_id"])
    assert checkpoints[-1]["state"]["external_result"]["result_commit_sha"] == "abc123"
    external = await store.list_external(run["run_id"])
    assert external[0]["status"] == "FINAL"

    # Second pass: nothing left to consume; the run is not transitioned again.
    assert (await store.consume_wakeups())["resumed"] == 0
    assert (await store.get_run(run["run_id"]))["version"] == resumed["version"]


@pytest.mark.asyncio
async def test_expired_external_fails_the_run(store: RunStore) -> None:
    run = await _running(store)
    await store.claim_run("worker-1", lease_seconds=30)
    controller = LoopController(
        store, _ScriptedModel([_submit_action()]), _DeferringTool("33333333-3333-3333-3333-333333333333")
    )
    await controller.step(run["run_id"])
    external = (await store.list_external(run["run_id"]))[0]
    # Force the deadline into the past.
    await store.finalize_external(external["external_id"], status="PENDING")
    async with store._sessions() as session:  # noqa: SLF001 - test-only SQL
        from sqlalchemy import text

        await session.execute(
            text(
                f"UPDATE {store.schema}.external_invocations "
                "SET deadline = NOW() - INTERVAL '1 minute' "
                "WHERE external_id = :external_id"
            ),
            {"external_id": external["external_id"]},
        )
        await session.commit()

    stats = await store.expire_external()
    assert stats["runs_failed"] == 1
    assert (await store.get_run(run["run_id"]))["status"] == "FAILED"


@pytest.mark.asyncio
async def test_repeated_step_with_same_key_does_not_submit_twice(store: RunStore) -> None:
    run = await _running(store)
    await store.claim_run("worker-1", lease_seconds=30)
    tools = _DeferringTool("44444444-4444-4444-4444-444444444444")
    controller = LoopController(
        store,
        _ScriptedModel([_submit_action(), _submit_action()]),
        tools,
    )
    await controller.step(run["run_id"])
    # A second worker step must see DEFERRED and not call the tool again.
    await store.transition(
        run["run_id"], "RUNNING", expected_version=(await store.get_run(run["run_id"]))["version"]
    )
    await controller.step(run["run_id"])
    assert tools.calls == 1
