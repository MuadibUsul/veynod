"""Unified product-task lifecycle (ADR-015): single Run, monotonic projection."""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import pytest_asyncio

from tests.conftest import run_server


class _RuntimeState:
    def __init__(self) -> None:
        self.runs: dict[str, dict] = {}
        self.steps: dict[str, list] = {}
        self.externals: dict[str, list] = {}
        self.cancelled: list[str] = []
        self.fail_start = False
        self.lock = threading.Lock()

    def create(self, body: dict) -> dict:
        run_id = str(uuid.uuid4())
        run = {
            "run_id": run_id,
            "status": "CREATED",
            "version": 1,
            "tenant_id": body.get("tenant_id"),
            "workspace_id": body.get("workspace_id"),
            "product_task_id": body.get("product_task_id"),
            "project_id": body.get("project_id"),
            "repo_path": body.get("repo_path"),
            "trace_id": body.get("trace_id"),
            "budget": body.get("budget") or {},
            "usage": {},
            "last_error": None,
            "kernel": body.get("kernel") or {},
        }
        with self.lock:
            self.runs[run_id] = run
            self.steps[run_id] = []
            self.externals[run_id] = []
        return run

    def get(self, run_id: str) -> dict | None:
        with self.lock:
            return self.runs.get(run_id)

    def set_status(self, run_id: str, status: str, **extra) -> None:
        with self.lock:
            run = self.runs[run_id]
            run["status"] = status
            run.update(extra)

    def add_external(self, run_id: str, payload: dict) -> None:
        with self.lock:
            self.externals[run_id].append(payload)


def _runtime_handler(state: _RuntimeState):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status_code: int, payload) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            parts = [part for part in path.split("/") if part]
            if path == "/healthz":
                self._send(200, {"status": "ok"})
                return
            if len(parts) >= 3 and parts[1] == "runs":
                run_id = parts[2]
                if len(parts) == 3:
                    run = state.get(run_id)
                    self._send(200 if run else 404, run or {"detail": "nope"})
                    return
                if parts[3] == "steps":
                    self._send(200, state.steps.get(run_id, []))
                    return
                if parts[3] == "external":
                    self._send(200, state.externals.get(run_id, []))
                    return
                if parts[3] == "events":
                    self._send(200, [])
                    return
            if len(parts) >= 2 and parts[1] == "usage":
                self._send(200, {"model_calls": 0, "cost_usd": 0})
                return
            if len(parts) >= 2 and parts[1] == "quota":
                self._send(
                    200,
                    {
                        "exceeded": False,
                        "policy": {},
                        "usage": {},
                        "reason": None,
                    },
                )
                return
            self._send(404, {"detail": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            body = self._body()
            if path == "/v1/runs":
                run = state.create(body)
                self._send(201, run)
                return
            parts = [part for part in path.split("/") if part]
            if len(parts) >= 4 and parts[1] == "runs":
                run_id = parts[2]
                if parts[3] == "start":
                    if state.fail_start:
                        state.fail_start = False
                        self._send(500, {"detail": "runtime is down"})
                        return
                    state.set_status(run_id, "RUNNING")
                    self._send(200, state.get(run_id))
                    return
                if parts[3] == "cancel":
                    state.set_status(run_id, "CANCELLED")
                    with state.lock:
                        state.cancelled.append(run_id)
                    self._send(200, state.get(run_id))
                    return
                if parts[3] == "wakeups":
                    self._send(200, {"inserted": True})
                    return
            self._send(404, {"detail": "not found"})

        def log_message(self, format, *args):  # noqa: A002
            return

    return Handler


@pytest.fixture(scope="module")
def runtime_state() -> _RuntimeState:
    state = _RuntimeState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _runtime_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
    try:
        yield state
    finally:
        server.shutdown()


@pytest_asyncio.fixture(scope="module")
async def lifecycle_client(runtime_state):
    async with run_server(
        {
            "AGENT_RUNTIME_URL": runtime_state.base_url,
            "AGENT_RUNTIME_KEY": "test-runtime",
            "AGENT_RUNTIME_PROJECTION_ENABLED": "false",
            "CODE_PROJECTION_ENABLED": "false",
            "EVENT_DISPATCH_ENABLED": "false",
            "SCHEDULER_ENABLED": "false",
        },
        ".server-lifecycle.log",
    ) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as http:
            yield http


@pytest_asyncio.fixture
async def workspace_headers(lifecycle_client, admin_headers) -> dict[str, str]:
    response = await lifecycle_client.post(
        "/v1/admin/workspaces",
        json={"name": f"life-{uuid.uuid4().hex[:8]}", "tier": "developer_5usd"},
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['api_key']}"}


async def _task(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    key = f"t-{uuid.uuid4().hex[:10]}"
    response = await client.post(
        "/v1/tasks",
        headers=headers,
        json={
            "task_key": key,
            "title": "lifecycle",
            "description": "do the thing",
            "status": "READY",
            "project": "proj",
            "repo": "/repo",
        },
    )
    assert response.status_code in (200, 201), response.text
    return key


@pytest.mark.integration
async def test_start_run_is_single_and_sets_correlation(
    lifecycle_client, workspace_headers
) -> None:
    key = await _task(lifecycle_client, workspace_headers)
    first = await lifecycle_client.post(
        f"/v1/tasks/{key}/run", headers=workspace_headers, json={}
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["run_id"]
    assert body["trace_id"]
    # The product task stays READY while the Run executes (IN_PROGRESS carries
    # a human/agent lease by invariant); the Run is the execution truth.
    assert body["agentgraph_status"] == "READY"
    task = (
        await lifecycle_client.get(
            f"/v1/tasks/{key}", headers=workspace_headers
        )
    ).json()
    assert task["correlation"]["run_id"] == body["run_id"]
    assert task["active_run_id"] == body["run_id"]

    second = await lifecycle_client.post(
        f"/v1/tasks/{key}/run", headers=workspace_headers, json={}
    )
    assert second.status_code == 200, second.text
    assert second.json()["run_id"] == body["run_id"], "a task has one active Run"

    view = await lifecycle_client.get(
        f"/v1/tasks/{key}/execution", headers=workspace_headers
    )
    assert view.status_code == 200, view.text
    payload = view.json()
    assert payload["loop"]["status"] == "RUNNING"
    assert payload["correlation"]["run_id"] == body["run_id"]


@pytest.mark.integration
async def test_failed_run_fails_the_task(
    lifecycle_client, workspace_headers, runtime_state
) -> None:
    key = await _task(lifecycle_client, workspace_headers)
    started = (
        await lifecycle_client.post(
            f"/v1/tasks/{key}/run", headers=workspace_headers, json={}
        )
    ).json()
    runtime_state.set_status(started["run_id"], "FAILED", last_error="model crashed")
    projected = await lifecycle_client.get(
        f"/v1/tasks/{key}/agent-run", headers=workspace_headers
    )
    assert projected.status_code == 200, projected.text
    assert projected.json()["agentgraph_status"] == "FAILED"


@pytest.mark.integration
async def test_completed_run_without_kernel_evidence_does_not_advance(
    lifecycle_client, workspace_headers, runtime_state
) -> None:
    key = await _task(lifecycle_client, workspace_headers)
    started = (
        await lifecycle_client.post(
            f"/v1/tasks/{key}/run", headers=workspace_headers, json={}
        )
    ).json()
    runtime_state.set_status(started["run_id"], "COMPLETED")
    projected = await lifecycle_client.get(
        f"/v1/tasks/{key}/agent-run", headers=workspace_headers
    )
    assert projected.status_code == 200, projected.text
    body = projected.json()
    assert body["projected"] is None
    assert body["agentgraph_status"] == "READY"

    # Second read must not produce a second side effect either.
    again = await lifecycle_client.get(
        f"/v1/tasks/{key}/agent-run", headers=workspace_headers
    )
    assert again.json()["agentgraph_status"] == "READY"


@pytest.mark.integration
async def test_runtime_owned_task_is_not_placed_by_the_scheduler(
    lifecycle_client, workspace_headers
) -> None:
    key = await _task(lifecycle_client, workspace_headers)
    await lifecycle_client.post(
        f"/v1/tasks/{key}/run", headers=workspace_headers, json={}
    )
    agenda = await lifecycle_client.get(
        "/v1/loop/agenda", headers=workspace_headers
    )
    if agenda.status_code == 404:
        pytest.skip("loop agenda is not exposed in this build")
    assert agenda.status_code == 200, agenda.text
    keys = json.dumps(agenda.json())
    assert key not in keys, "a Runtime-owned task must not wait for a runner host"


@pytest.mark.integration
async def test_failed_start_cancels_the_run_instead_of_orphaning_it(
    lifecycle_client, workspace_headers, runtime_state
) -> None:
    key = await _task(lifecycle_client, workspace_headers)
    runtime_state.fail_start = True
    response = await lifecycle_client.post(
        f"/v1/tasks/{key}/run", headers=workspace_headers, json={}
    )
    assert response.status_code >= 500, response.text

    task = (
        await lifecycle_client.get(f"/v1/tasks/{key}", headers=workspace_headers)
    ).json()
    assert task["active_run_id"] is None
    assert task["correlation"].get("run_id") is None
    # The Runtime was told to cancel the Run it had already created.
    assert runtime_state.cancelled, "an orphan Run must be compensated"
    status = runtime_state.get(runtime_state.cancelled[-1])
    assert status is not None and status["status"] == "CANCELLED"


@pytest.mark.integration
async def test_trace_view_joins_all_three_planes(
    lifecycle_client, workspace_headers
) -> None:
    key = await _task(lifecycle_client, workspace_headers)
    started = (
        await lifecycle_client.post(
            f"/v1/tasks/{key}/run", headers=workspace_headers, json={}
        )
    ).json()
    response = await lifecycle_client.get(
        f"/v1/tasks/{key}/trace", headers=workspace_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task"]["task_key"] == key
    assert body["ids"]["run_id"] == started["run_id"]
    assert body["ids"]["trace_id"] == started["trace_id"]
    assert isinstance(body["timeline"], list)
    assert body["run"] is None or "run_id" in (body["run"] or {})
