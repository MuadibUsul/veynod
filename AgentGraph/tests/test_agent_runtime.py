from __future__ import annotations

import httpx

from app.services.agent_runtime import (
    AgentRuntimeClient,
    runtime_status_action,
)


def test_runtime_status_projection_actions() -> None:
    assert runtime_status_action("COMPLETED", "READY") == "review"
    assert runtime_status_action("FAILED", "READY") == "fail"
    assert runtime_status_action("CANCELLED", "IN_PROGRESS") == "cancel"
    assert runtime_status_action("COMPLETED", "REVIEWING") is None
    assert runtime_status_action("FAILED", "DONE") is None
    assert runtime_status_action("RUNNING", "READY") is None


async def test_agent_runtime_client_protocol() -> None:
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (request.method, request.url.path, request.headers["x-runtime-key"])
        )
        if request.url.path.startswith("/v1/quota/"):
            return httpx.Response(
                200,
                json={"exceeded": False, "policy": {}, "usage": {}, "reason": None},
            )
        if request.url.path == "/v1/runs":
            return httpx.Response(201, json={"run_id": "run-1", "status": "CREATED"})
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"run_id": "run-1", "status": "RUNNING"})
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"run_id": "run-1", "status": "CANCELLED"})
        return httpx.Response(
            200, json={"run_id": "run-1", "status": "RUNNING", "version": 2}
        )

    client = AgentRuntimeClient(
        "http://runtime.test",
        "key-1",
        transport=httpx.MockTransport(handler),
    )
    quota = await client.tenant_quota("t-1")
    assert quota is not None and quota["exceeded"] is False
    created = await client.create_run(
        tenant_id="t-1",
        workspace_id="w-1",
        product_task_id="task-1",
        budget={"max_model_calls": 2},
    )
    assert created["run_id"] == "run-1"
    started = await client.start_run("run-1")
    assert started["status"] == "RUNNING"
    loaded = await client.get_run("run-1")
    assert loaded["status"] == "RUNNING"
    cancelled = await client.cancel_run("run-1", "stop")
    assert cancelled["status"] == "CANCELLED"
    assert seen == [
        ("GET", "/v1/quota/t-1", "key-1"),
        ("POST", "/v1/runs", "key-1"),
        ("POST", "/v1/runs/run-1/start", "key-1"),
        ("GET", "/v1/runs/run-1", "key-1"),
        ("POST", "/v1/runs/run-1/cancel", "key-1"),
    ]
