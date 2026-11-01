from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_runtime.api import create_app

URL = os.environ.get("RUNTIME_TEST_DATABASE_URL") or os.environ.get(
    "TEST_DATABASE_URL"
)

pytestmark = pytest.mark.integration


@pytest.fixture()
def client():
    if not URL:
        pytest.skip("RUNTIME_TEST_DATABASE_URL/TEST_DATABASE_URL is not set")
    name = URL.rsplit("/", 1)[-1].split("?")[0].lower()
    if "test" not in name and os.environ.get("VEYNOD_ALLOW_DESTRUCTIVE_TESTS") != "1":
        pytest.skip("runtime API tests require a disposable database")
    app = create_app(
        URL,
        "test-runtime-key",
        schema="agent_runtime_api_test",
        reset_schema=True,
    )
    with TestClient(app) as test_client:
        yield test_client


def _headers() -> dict[str, str]:
    return {"X-Runtime-Key": "test-runtime-key"}


def _create(client: TestClient) -> dict:
    response = client.post(
        "/v1/runs",
        headers=_headers(),
        json={
            "tenant_id": str(uuid4()),
            "workspace_id": str(uuid4()),
            "product_task_id": "product-1",
            "budget": {"max_model_calls": 10},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_runtime_api_auth_and_lifecycle(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200
    assert client.post("/v1/runs", json={}).status_code == 401

    run = _create(client)
    run_id = run["run_id"]
    version = run["version"]
    for target in ("QUEUED", "INITIALIZING", "PLANNING", "READY", "RUNNING"):
        response = client.post(
            f"/v1/runs/{run_id}/transition",
            headers=_headers(),
            json={"target": target, "expected_version": version},
        )
        assert response.status_code == 200, response.text
        version = response.json()["version"]

    paused = client.post(
        f"/v1/runs/{run_id}/pause",
        headers=_headers(),
        json={"expected_version": version, "reason": "operator"},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "PAUSED"

    resumed = client.post(
        f"/v1/runs/{run_id}/resume",
        headers=_headers(),
        json={"reason": "operator"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "RUNNING"

    checkpoint = client.post(
        f"/v1/runs/{run_id}/checkpoint",
        headers=_headers(),
        json={"state": {"step": 1}, "idempotency_key": "cp-1"},
    )
    assert checkpoint.status_code == 200, checkpoint.text
    checkpoints = client.get(
        f"/v1/runs/{run_id}/checkpoints", headers=_headers()
    )
    assert checkpoints.status_code == 200
    assert checkpoints.json()[-1]["version"] == 1

    cancelled = client.post(
        f"/v1/runs/{run_id}/cancel",
        headers=_headers(),
        json={"reason": "test"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"

    events = client.get(f"/v1/runs/{run_id}/events", headers=_headers())
    assert events.status_code == 200
    assert any(
        event["event_type"] == "AgentRunCheckpointed"
        for event in events.json()
    )

    stats = client.get("/v1/stats", headers=_headers())
    assert stats.status_code == 200
    assert "CANCELLED" in stats.json()["statuses"]
    usage = client.get(f"/v1/usage/{run['tenant_id']}", headers=_headers())
    assert usage.status_code == 200
    assert "model_calls" in usage.json()

    quota = client.get(f"/v1/quota/{run['tenant_id']}", headers=_headers())
    assert quota.status_code == 200, quota.text
    assert quota.json()["exceeded"] is False
    assert "max_model_calls" in quota.json()["policy"]

    health = client.get("/v1/provider-health", headers=_headers())
    assert health.status_code == 200, health.text
    assert health.json()["status"] in {"unknown", "healthy", "degraded"}
    assert client.get("/v1/quota/tenant-x").status_code == 401


def test_runtime_api_invalid_transition(client: TestClient) -> None:
    run = _create(client)
    response = client.post(
        f"/v1/runs/{run['run_id']}/transition",
        headers=_headers(),
        json={"target": "COMPLETED", "expected_version": run["version"]},
    )
    assert response.status_code == 409, response.text


def test_runtime_api_delegation(client: TestClient) -> None:
    run = _create(client)
    run_id = run["run_id"]
    version = run["version"]
    for target in ("QUEUED", "INITIALIZING", "PLANNING", "READY", "RUNNING"):
        response = client.post(
            f"/v1/runs/{run_id}/transition",
            headers=_headers(),
            json={"target": target, "expected_version": version},
        )
        assert response.status_code == 200, response.text
        version = response.json()["version"]

    delegated = client.post(
        f"/v1/runs/{run_id}/delegate",
        headers=_headers(),
        json={"product_task_id": "child-task", "budget": {"max_model_calls": 2}},
    )
    assert delegated.status_code == 200, delegated.text
    body = delegated.json()
    assert body["parent"]["status"] == "WAITING_CHILDREN"
    child = body["child"]
    assert child["status"] == "RUNNING"
    assert child["parent_run_id"] == run_id

    children = client.get(f"/v1/runs/{run_id}/children", headers=_headers())
    assert children.status_code == 200
    assert len(children.json()) == 1

    completed = client.post(
        f"/v1/runs/{child['run_id']}/transition",
        headers=_headers(),
        json={"target": "COMPLETED", "expected_version": child["version"]},
    )
    assert completed.status_code == 200, completed.text

    collected = client.post(f"/v1/runs/{run_id}/collect", headers=_headers())
    assert collected.status_code == 200, collected.text
    assert collected.json()["ready"] is True
    assert collected.json()["parent"]["status"] == "RUNNING"
