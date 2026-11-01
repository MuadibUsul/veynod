"""KernelToolRuntime: capability registry, routing, idempotency, error classes."""

from __future__ import annotations

import json
import os
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from agent_runtime.loop import ToolResult  # noqa: F401  (typing reference)
from agent_runtime.store import RunStore
from agent_runtime.tool_context import ToolContext, tool_context
from agent_runtime.tools import (
    KernelBinding,
    KernelToolConfig,
    KernelToolRuntime,
    classify_status,
)
from agent_runtime.wakeups import KernelEventSource, KernelEventWatcher

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


def _context(*, workspace: str | None = None, run_id: str | None = None) -> ToolContext:
    return ToolContext(
        run_id=run_id or str(uuid4()),
        tenant_id="ten_1",
        workspace_id=str(uuid4()),
        product_task_id="task-1",
        project_id="proj",
        repo_path="/repo",
        trace_id="trace-1",
        kernel={
            "base_url": "http://kernel.test",
            "workspace_id": workspace or str(uuid4()),
            "credential_ref": "TEST_KERNEL_TOKEN",
        },
    )


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("TEST_KERNEL_TOKEN", "vnk_test_token")


@pytest.mark.asyncio
async def test_pull_task_bundle_redacts_lease_and_persists_it(store: RunStore) -> None:
    task_id = str(uuid4())
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/runtime/tasks":
            return httpx.Response(201, json={"task_id": task_id, "status": "READY"})
        return httpx.Response(
            200,
            json={
                "leased": True,
                "task_id": task_id,
                "lease_token": "vnl_secret",
                "write_set": ["src/x.py"],
                "mutable_stubs": [],
            },
        )

    runtime = KernelToolRuntime(
        KernelToolConfig(service_key="svc-test"),
        store=store,
        transport=httpx.MockTransport(handler),
    )
    context = _context()
    await store.create_run(
        tenant_id="ten",
        workspace_id=context.workspace_id,
        product_task_id="task-1",
        kernel=context.kernel,
    )
    run_id = (await store.list_runs_by_status("CREATED", 1))[0]["run_id"]
    context = ToolContext(
        run_id=str(run_id),
        tenant_id="ten",
        workspace_id=context.workspace_id,
        product_task_id="task-1",
        project_id="proj",
        repo_path="/repo",
        trace_id="trace-1",
        kernel=context.kernel,
    )
    with tool_context(context):
        created = await runtime.execute(
            "create_task",
            {"title": "t", "spec_prompt": "implement the thing", "target_symbols": ["src/x.py::f"]},
            "create-1",
        )
        assert created.ok
        result = await runtime.execute(
            "pull_task_bundle", {"max_context_tokens": 4000}, "pull-1"
        )
    assert result.ok
    assert "lease_token" not in result.output
    assert result.output["lease_token_present"] is True
    assert result.output["task_id"] == task_id
    assert await store.kernel_lease(context.run_id, task_id) == "vnl_secret"

    request = seen[-1]
    assert request.headers["X-Workspace-Id"] == context.kernel["workspace_id"]
    assert request.headers["Authorization"] == "Bearer vnk_test_token"
    body = json.loads(request.content)
    assert body["worker_hub_id"]
    # The Run leases exactly the node it created.
    assert body["task_id"] == task_id
    await runtime.close()


@pytest.mark.asyncio
async def test_submit_patch_returns_deferred_on_202(store: RunStore) -> None:
    task_id = str(uuid4())
    submission_id = str(uuid4())
    context = _context()
    run = await store.create_run(
        tenant_id="ten",
        workspace_id=context.workspace_id,
        product_task_id="task-1",
        kernel=context.kernel,
    )
    context = ToolContext(
        run_id=run["run_id"],
        tenant_id="ten",
        workspace_id=context.workspace_id,
        product_task_id="task-1",
        project_id="proj",
        repo_path="/repo",
        trace_id="trace-1",
        kernel=context.kernel,
    )
    await store.remember_kernel_lease(
        run_id=context.run_id,
        kernel_task_id=task_id,
        kernel_workspace_id=context.kernel["workspace_id"],
        lease_token="vnl_secret",
    )
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            202,
            json={
                "submission_id": submission_id,
                "status": "QUEUED",
                "status_url": f"/v2/submissions/{submission_id}",
            },
        )

    runtime = KernelToolRuntime(
        KernelToolConfig(), store=store, transport=httpx.MockTransport(handler)
    )
    with tool_context(context):
        result = await runtime.execute(
            "submit_patch",
            {"task_id": task_id, "git_diff": "--- a\n+++ b\n", "touched_symbols": ["a"]},
            "submit-key-1",
        )
    assert result.ok
    assert result.deferred is not None
    assert result.deferred["submission_id"] == submission_id
    assert result.deferred["kernel_task_id"] == task_id
    assert captured["headers"]["idempotency-key"] == "submit-key-1"
    # The lease token is injected server-side, never taken from model input.
    assert captured["body"]["lease_token"] == "vnl_secret"
    assert captured["body"]["correlation"]["trace_id"] == "trace-1"
    await runtime.close()


@pytest.mark.asyncio
async def test_auth_error_is_not_retried_and_is_classified(store: RunStore) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, json={"detail": "lease belongs to another runner"})

    runtime = KernelToolRuntime(
        KernelToolConfig(max_retries=2),
        store=store,
        transport=httpx.MockTransport(handler),
    )
    with tool_context(_context()):
        result = await runtime.execute(
            "get_task_status", {"task_id": str(uuid4())}, "status-1"
        )
    assert not result.ok
    assert result.error_class == "AUTH"
    assert calls["n"] == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_admission_429_is_retried(store: RunStore) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"detail": "admission"})
        return httpx.Response(200, json={"task_status": "READY"})

    runtime = KernelToolRuntime(
        KernelToolConfig(max_retries=2),
        store=store,
        transport=httpx.MockTransport(handler),
    )
    with tool_context(_context()):
        result = await runtime.execute(
            "get_task_status", {"task_id": str(uuid4())}, "status-2"
        )
    assert result.ok
    assert calls["n"] == 2
    await runtime.close()


@pytest.mark.asyncio
async def test_missing_arguments_never_reach_the_kernel(store: RunStore) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={})

    runtime = KernelToolRuntime(
        KernelToolConfig(), store=store, transport=httpx.MockTransport(handler)
    )
    with tool_context(_context()):
        result = await runtime.execute("submit_patch", {"git_diff": "x"}, "k")
    assert not result.ok
    assert result.error_class == "VALIDATION"
    assert calls["n"] == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_unknown_or_disallowed_tool_is_rejected(store: RunStore) -> None:
    runtime = KernelToolRuntime(
        KernelToolConfig(allowed_tools=frozenset({"get_task_status"})),
        store=store,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    with tool_context(_context()):
        disallowed = await runtime.execute("cancel_task", {"task_id": "x"}, "k1")
        unknown = await runtime.execute("rm_rf", {}, "k2")
    assert disallowed.error_class == "AUTH"
    assert unknown.error_class == "AUTH"
    await runtime.close()


@pytest.mark.asyncio
async def test_binding_comes_from_the_run_not_a_global_default(store: RunStore) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["X-Workspace-Id"])
        return httpx.Response(200, json={"task_status": "READY"})

    runtime = KernelToolRuntime(
        KernelToolConfig(
            default_binding=KernelBinding(
                base_url="http://kernel.test",
                workspace_id="00000000-0000-0000-0000-000000000001",
                credential_ref="TEST_KERNEL_TOKEN",
            )
        ),
        store=store,
        transport=httpx.MockTransport(handler),
    )
    context = _context(workspace="00000000-0000-0000-0000-000000000009")
    with tool_context(context):
        await runtime.execute("get_task_status", {"task_id": str(uuid4())}, "k")
    assert seen == ["00000000-0000-0000-0000-000000000009"]
    await runtime.close()


def test_status_classification() -> None:
    assert classify_status(401) == "AUTH"
    assert classify_status(404) == "NOT_FOUND"
    assert classify_status(409) == "CONFLICT"
    assert classify_status(422) == "VALIDATION"
    assert classify_status(429) == "ADMISSION"
    assert classify_status(503) == "TRANSPORT"


@pytest.mark.asyncio
async def test_kernel_watcher_turns_events_into_wakeups(store: RunStore) -> None:
    workspace = str(uuid4())
    submission = str(uuid4())
    context = ToolContext(
        run_id=str(uuid4()),
        tenant_id="ten",
        workspace_id=str(uuid4()),
        kernel={"base_url": "http://kernel.test", "workspace_id": workspace},
    )
    run = await store.create_run(
        tenant_id="ten",
        workspace_id=context.workspace_id,
        product_task_id="task",
        kernel={"base_url": "http://kernel.test", "workspace_id": workspace},
    )
    for target in ("QUEUED", "INITIALIZING", "PLANNING", "READY", "RUNNING", "WAITING_EXTERNAL"):
        await store.transition(run["run_id"], target, expected_version=run["version"])
        run = await store.get_run(run["run_id"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "workspace_id": workspace,
                "events": [
                    {
                        "event_seq": 7,
                        "event_type": "SUBMISSION_COMMITTED",
                        "payload": {
                            "submission_id": submission,
                            "status": "COMMITTED",
                            "result_commit_sha": "deadbeef",
                        },
                    }
                ],
                "next_after_seq": 7,
            },
        )

    watcher = KernelEventWatcher(
        store,
        KernelEventSource(base_url="http://kernel.test", service_key="svc"),
        transport=httpx.MockTransport(handler),
    )
    stats = await watcher.poll_once()
    assert stats["wakeups"] == 1
    result = await store.consume_wakeups()
    assert result["wakeups"] == 1
    await watcher.close()


@pytest.mark.asyncio
async def test_pull_without_create_is_refused(store: RunStore) -> None:
    runtime = KernelToolRuntime(
        KernelToolConfig(),
        store=store,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    with tool_context(_context()):
        result = await runtime.execute("pull_task_bundle", {}, "pull-nocreate")
    assert not result.ok
    assert result.error_class == "VALIDATION"
    assert "create_task" in (result.error or "")
    await runtime.close()
