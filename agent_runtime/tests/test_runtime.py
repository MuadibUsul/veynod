from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio

from agent_runtime import (
    ContextBuilder,
    ContextSection,
    DelegationManager,
    LoopController,
    ModelAction,
    ModelRoute,
    QuotaPolicy,
    ReconciliationLoop,
    RoutingModelGateway,
    RunState,
    RunStore,
    RuntimeWorker,
    RuntimeWorkerPool,
    ToolResult,
)
from agent_runtime.gateway import ModelGatewayError
from agent_runtime.errors import (
    IdempotencyConflict,
    InvalidTransition,
    LeaseLost,
    RunConflict,
    RunNotFound,
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


def _ids() -> tuple[str, str]:
    return str(uuid4()), str(uuid4())


async def _to_running(store: RunStore, budget: dict | None = None) -> dict:
    tenant, workspace = _ids()
    run = await store.create_run(
        tenant_id=tenant,
        workspace_id=workspace,
        product_task_id="product-task-1",
        budget=budget,
    )
    for target in ("QUEUED", "INITIALIZING", "PLANNING", "READY", "RUNNING"):
        await store.transition(
            run["run_id"], target, expected_version=run["version"]
        )
        run = await store.get_run(run["run_id"])
    return run


class _ScriptedModel:
    def __init__(self, actions: list[ModelAction]) -> None:
        self.actions = list(actions)
        self.calls = 0
        self.contexts: list[dict] = []

    async def decide(self, run, context):
        self.calls += 1
        self.contexts.append(context)
        return self.actions.pop(0)


class _CountingTool:
    def __init__(self) -> None:
        self.calls = 0
        self.keys: list[str] = []

    async def execute(self, name, arguments, idempotency_key):
        self.calls += 1
        self.keys.append(idempotency_key)
        return ToolResult(True, {"name": name, "arguments": arguments})


@pytest.mark.asyncio
async def test_run_lifecycle_and_version_conflict(store: RunStore) -> None:
    run = await _to_running(store)
    assert run["status"] == RunState.RUNNING.value
    with pytest.raises(RunConflict):
        await store.transition(
            run["run_id"], "PAUSING", expected_version=run["version"] + 99
        )
    paused = await store.transition(
        run["run_id"], "PAUSING", expected_version=run["version"]
    )
    assert paused["status"] == "PAUSING"
    with pytest.raises(InvalidTransition):
        await store.transition(run["run_id"], "COMPLETED")


@pytest.mark.asyncio
async def test_invalid_transition_and_missing_run(store: RunStore) -> None:
    tenant, workspace = _ids()
    run = await store.create_run(tenant_id=tenant, workspace_id=workspace)
    with pytest.raises(InvalidTransition):
        await store.transition(run["run_id"], "COMPLETED")
    with pytest.raises(RunNotFound):
        await store.transition(str(uuid4()), "QUEUED")


@pytest.mark.asyncio
async def test_create_and_transition_idempotency(store: RunStore) -> None:
    tenant, workspace = _ids()
    first = await store.create_run(
        tenant_id=tenant,
        workspace_id=workspace,
        idempotency_key="create-1",
    )
    second = await store.create_run(
        tenant_id=tenant,
        workspace_id=workspace,
        idempotency_key="create-1",
    )
    assert first["run_id"] == second["run_id"]

    transitioned = await store.transition(
        first["run_id"],
        "QUEUED",
        expected_version=1,
        idempotency_key="queue-1",
    )
    repeated = await store.transition(
        first["run_id"],
        "QUEUED",
        expected_version=1,
        idempotency_key="queue-1",
    )
    assert repeated == transitioned
    with pytest.raises(IdempotencyConflict):
        await store.transition(
            first["run_id"],
            "CANCELLED",
            idempotency_key="queue-1",
        )


@pytest.mark.asyncio
async def test_checkpoint_reload_and_restart(store: RunStore) -> None:
    run = await _to_running(store)
    checkpoint = await store.checkpoint(
        run["run_id"],
        {"context": {"goal": "ship"}, "step": 3},
        idempotency_key="cp-1",
    )
    repeated = await store.checkpoint(
        run["run_id"],
        {"context": {"goal": "ship"}, "step": 3},
        idempotency_key="cp-1",
    )
    assert repeated["checkpoint_id"] == checkpoint["checkpoint_id"]
    checkpoints = await store.list_checkpoints(run["run_id"])
    assert checkpoints[-1]["version"] == checkpoint["version"]
    events = await store.list_events(run["run_id"])
    assert any(event["event_type"] == "AgentRunCheckpointed" for event in events)

    await store.close()
    reopened = RunStore(URL)
    try:
        reloaded = await reopened.get_run(run["run_id"])
        assert reloaded["status"] == "RUNNING"
        assert len(await reopened.list_checkpoints(run["run_id"])) == 1
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_failure_records_error(store: RunStore) -> None:
    run = await _to_running(store)
    failed = await store.transition(
        run["run_id"],
        "FAILED",
        expected_version=run["version"],
        reason="model gateway unavailable",
    )
    assert failed["status"] == "FAILED"
    reloaded = await store.get_run(run["run_id"])
    assert reloaded["last_error"] == "model gateway unavailable"
    assert reloaded["finished_at"] is not None


@pytest.mark.asyncio
async def test_loop_executes_tool_then_finishes(store: RunStore) -> None:
    run = await _to_running(store)
    model = _ScriptedModel(
        [
            ModelAction(
                kind="tool",
                tool="echo",
                arguments={"value": 1},
                idempotency_key="tool-1",
                prompt_tokens=10,
                completion_tokens=5,
                cost_usd=0.01,
            ),
            ModelAction(kind="finish", output={"done": True}),
        ]
    )
    tools = _CountingTool()
    controller = LoopController(store, model, tools)

    first = await controller.step(run["run_id"])
    assert first["executed"] is True
    assert tools.calls == 1
    assert (await store.get_run(run["run_id"]))["status"] == "RUNNING"

    second = await controller.step(run["run_id"])
    assert second["executed"] is True
    final = await store.get_run(run["run_id"])
    assert final["status"] == "COMPLETED"
    assert model.calls == 2
    assert tools.calls == 1
    assert len(await store.list_checkpoints(run["run_id"])) >= 2


@pytest.mark.asyncio
async def test_tool_idempotency_key_prevents_duplicate_side_effect(
    store: RunStore,
) -> None:
    run = await _to_running(store)
    model = _ScriptedModel(
        [
            ModelAction(
                kind="tool",
                tool="charge",
                arguments={"amount": 10},
                idempotency_key="charge-1",
            ),
            ModelAction(
                kind="tool",
                tool="charge",
                arguments={"amount": 10},
                idempotency_key="charge-1",
            ),
            ModelAction(kind="finish"),
        ]
    )
    tools = _CountingTool()
    controller = LoopController(store, model, tools)

    await controller.step(run["run_id"])
    second = await controller.step(run["run_id"])
    assert second["executed"] is False
    assert tools.calls == 1
    await controller.step(run["run_id"])
    assert (await store.get_run(run["run_id"]))["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_budget_blocks_tool_execution(store: RunStore) -> None:
    run = await _to_running(store, budget={"max_prompt_tokens": 5})
    model = _ScriptedModel(
        [
            ModelAction(
                kind="tool",
                tool="echo",
                arguments={},
                idempotency_key="budget-tool",
                prompt_tokens=10,
            )
        ]
    )
    tools = _CountingTool()
    controller = LoopController(store, model, tools)
    result = await controller.step(run["run_id"])
    assert result["run"]["status"] == "BUDGET_EXCEEDED"
    assert tools.calls == 0


@pytest.mark.asyncio
async def test_existing_over_budget_does_not_call_model(store: RunStore) -> None:
    run = await _to_running(store, budget={"max_model_calls": 1})
    await store.add_usage(run["run_id"], model_calls=2)
    model = _ScriptedModel([ModelAction(kind="finish")])
    controller = LoopController(store, model, _CountingTool())
    result = await controller.step(run["run_id"])
    assert result["run"]["status"] == "BUDGET_EXCEEDED"
    assert model.calls == 0


@pytest.mark.asyncio
async def test_cancelled_run_does_not_call_model(store: RunStore) -> None:
    run = await _to_running(store)
    await store.transition(
        run["run_id"],
        "CANCELLED",
        expected_version=run["version"],
        reason="user cancelled",
    )
    model = _ScriptedModel([ModelAction(kind="finish")])
    controller = LoopController(store, model, _CountingTool())
    result = await controller.step(run["run_id"])
    assert result["run"]["status"] == "CANCELLED"
    assert model.calls == 0


@pytest.mark.asyncio
async def test_loop_resumes_after_store_restart(store: RunStore) -> None:
    run = await _to_running(store)
    first_model = _ScriptedModel(
        [
            ModelAction(
                kind="tool",
                tool="echo",
                arguments={"step": 1},
                idempotency_key="restart-tool",
            )
        ]
    )
    await LoopController(store, first_model, _CountingTool()).step(run["run_id"])

    await store.close()
    reopened = RunStore(URL)
    try:
        second_model = _ScriptedModel([ModelAction(kind="finish")])
        result = await LoopController(
            reopened, second_model, _CountingTool()
        ).step(run["run_id"])
        assert result["run"]["status"] == "COMPLETED"
        assert (await reopened.get_run(run["run_id"]))["status"] == "COMPLETED"
        assert len(await reopened.list_checkpoints(run["run_id"])) >= 2
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_lease_claim_renew_and_release(store: RunStore) -> None:
    run = await _to_running(store)
    first = await store.claim_run("worker-1", lease_seconds=30)
    assert first is not None
    assert first["run"]["run_id"] == run["run_id"]
    assert await store.claim_run("worker-2", lease_seconds=30) is None

    await store.assert_lease(run["run_id"], first["lease_token"])
    with pytest.raises(LeaseLost):
        await store.assert_lease(run["run_id"], "wrong-token")
    await store.renew_lease(run["run_id"], first["lease_token"], 30)
    await store.release_lease(run["run_id"], first["lease_token"])

    second = await store.claim_run("worker-2", lease_seconds=30)
    assert second is not None
    await store.release_lease(run["run_id"], second["lease_token"])


@pytest.mark.asyncio
async def test_runtime_worker_executes_one_step_per_claim(store: RunStore) -> None:
    run = await _to_running(store)
    model = _ScriptedModel(
        [
            ModelAction(
                kind="tool",
                tool="echo",
                arguments={"worker": True},
                idempotency_key="worker-tool",
            ),
            ModelAction(kind="finish"),
        ]
    )
    tools = _CountingTool()
    worker = RuntimeWorker(
        store, model, tools, worker_id="worker-1", lease_seconds=30
    )

    first = await worker.run_once()
    assert first is not None
    assert first["executed"] is True
    assert tools.calls == 1
    assert (await store.get_run(run["run_id"]))["status"] == "RUNNING"

    second = await worker.run_once()
    assert second is not None
    assert (await store.get_run(run["run_id"]))["status"] == "COMPLETED"
    assert tools.calls == 1


def test_context_builder_respects_priority_and_budget() -> None:
    builder = ContextBuilder(token_budget=128)
    run = {
        "run_id": "r1",
        "product_task_id": "p1",
        "budget": {"max_model_calls": 5},
        "usage": {},
    }
    context = builder.build(
        run=run,
        checkpoint={"huge": "x" * 5000},
        extra=[
            ContextSection("doc", "doc/low", "y" * 5000, priority=1),
        ],
        token_budget=128,
    )
    kinds = {section["kind"] for section in context["sections"]}
    assert "system_policy" in kinds
    assert "product_task" in kinds
    assert context["token_estimate"] <= 128
    assert context["omitted"] or any(
        section["truncated"] for section in context["sections"]
    )


@pytest.mark.asyncio
async def test_loop_passes_built_context_to_model(store: RunStore) -> None:
    run = await _to_running(store)
    model = _ScriptedModel([ModelAction(kind="finish")])
    controller = LoopController(
        store,
        model,
        _CountingTool(),
        context_builder=ContextBuilder(token_budget=500),
    )
    await controller.step(run["run_id"])
    assert model.contexts
    context = model.contexts[0]
    assert context["token_estimate"] <= 500
    assert "sections" in context
    assert any(
        section["kind"] == "product_task" for section in context["sections"]
    )


@pytest.mark.asyncio
async def test_delegation_waits_for_children_and_collects(store: RunStore) -> None:
    parent = await _to_running(store)
    manager = DelegationManager(store)
    delegated = await manager.delegate(
        parent["run_id"], product_task_id="child-task"
    )
    assert delegated["parent"]["status"] == "WAITING_CHILDREN"
    child = delegated["child"]
    assert child["status"] == "RUNNING"
    assert child["parent_run_id"] == parent["run_id"]
    assert child["tenant_id"] == parent["tenant_id"]
    assert child["workspace_id"] == parent["workspace_id"]

    not_ready = await manager.collect(parent["run_id"])
    assert not_ready["ready"] is False

    await store.transition(
        child["run_id"],
        "COMPLETED",
        expected_version=child["version"],
        reason="child done",
    )
    collected = await manager.collect(parent["run_id"])
    assert collected["ready"] is True
    assert collected["parent"]["status"] == "RUNNING"
    assert collected["children"][0]["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_delegation_is_idempotent_and_reconciles_orphans(
    store: RunStore,
) -> None:
    parent = await _to_running(store)
    manager = DelegationManager(store)
    first = await manager.delegate(parent["run_id"], idempotency_key="d-1")
    second = await manager.delegate(parent["run_id"], idempotency_key="d-1")
    assert second["idempotent"] is True
    assert second["child"]["run_id"] == first["child"]["run_id"]

    orphan = await _to_running(store)
    await store.transition(
        orphan["run_id"],
        "WAITING_CHILDREN",
        expected_version=orphan["version"],
        reason="no children",
    )
    reconciled = await manager.reconcile(orphan["run_id"])
    assert reconciled["ready"] is True
    assert reconciled["parent"]["status"] == "RUNNING"


@pytest.mark.asyncio
async def test_runtime_worker_pool_executes_and_stops(store: RunStore) -> None:
    run = await _to_running(store)
    model = _ScriptedModel([ModelAction(kind="finish")])
    pool = RuntimeWorkerPool(
        store,
        model,
        _CountingTool(),
        workers=2,
        lease_seconds=30,
        poll_seconds=0.05,
    )
    await pool.start()
    try:
        for _ in range(100):
            if (await store.get_run(run["run_id"]))["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.05)
        assert (await store.get_run(run["run_id"]))["status"] == "COMPLETED"
        snapshot = pool.snapshot()
        assert snapshot["workers"] == 2
        assert snapshot["started"] == 2
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_tenant_quota_blocks_model_call(store: RunStore) -> None:
    tenant = str(uuid4())
    workspace = str(uuid4())
    first = await store.create_run(tenant_id=tenant, workspace_id=workspace)
    for target in ("QUEUED", "INITIALIZING", "PLANNING", "READY", "RUNNING"):
        await store.transition(
            first["run_id"], target, expected_version=first["version"]
        )
        first = await store.get_run(first["run_id"])
    await store.add_usage(first["run_id"], model_calls=2)

    second = await store.create_run(tenant_id=tenant, workspace_id=workspace)
    for target in ("QUEUED", "INITIALIZING", "PLANNING", "READY", "RUNNING"):
        await store.transition(
            second["run_id"], target, expected_version=second["version"]
        )
        second = await store.get_run(second["run_id"])

    model = _ScriptedModel([ModelAction(kind="finish")])
    controller = LoopController(
        store,
        model,
        _CountingTool(),
        quota=QuotaPolicy(max_model_calls=1),
    )
    result = await controller.step(second["run_id"])
    assert result["run"]["status"] == "BUDGET_EXCEEDED"
    assert model.calls == 0


@pytest.mark.asyncio
async def test_model_router_prefers_then_falls_back() -> None:
    class _Gateway:
        def __init__(self, name: str, fails: bool = False) -> None:
            self.name = name
            self.fails = fails
            self.calls = 0

        async def decide(self, run, context):
            self.calls += 1
            if self.fails:
                raise ModelGatewayError(f"{self.name} down")
            return ModelAction(kind="finish")

    bad = _Gateway("bad", fails=True)
    good = _Gateway("good")
    router = RoutingModelGateway(
        [
            ModelRoute("bad", bad, priority=10),
            ModelRoute("good", good, priority=1),
        ]
    )
    action = await router.decide({"run_id": "r1"}, {})
    assert action.kind == "finish"
    assert bad.calls == 1
    assert good.calls == 1

    preferred = RoutingModelGateway(
        [
            ModelRoute("bad", bad, priority=10),
            ModelRoute("good", good, priority=1),
        ]
    )
    await preferred.decide(
        {"run_id": "r1", "budget": {"model_provider": "good"}}, {}
    )
    assert good.calls == 2


@pytest.mark.asyncio
async def test_reconciliation_loop_resumes_orphan_parent(store: RunStore) -> None:
    parent = await _to_running(store)
    await store.transition(
        parent["run_id"],
        "WAITING_CHILDREN",
        expected_version=parent["version"],
        reason="no children",
    )
    loop = ReconciliationLoop(store, interval_seconds=1)
    stats = await loop.run_once()
    assert stats["resumed"] >= 1
    assert (await store.get_run(parent["run_id"]))["status"] == "RUNNING"

