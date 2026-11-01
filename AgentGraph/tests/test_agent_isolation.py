"""Workspace isolation on the product plane (real database, two tenants)."""

from __future__ import annotations

import uuid

import httpx
import pytest


async def _workspace(client: httpx.AsyncClient, admin: dict[str, str]) -> dict[str, str]:
    response = await client.post(
        "/v1/admin/workspaces",
        json={"name": f"iso-{uuid.uuid4().hex[:8]}", "tier": "developer_5usd"},
        headers=admin,
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['api_key']}"}


@pytest.mark.integration
async def test_another_workspace_cannot_read_documents_tasks_or_runs(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    owner = await _workspace(client, admin_headers)
    outsider = await _workspace(client, admin_headers)
    suffix = uuid.uuid4().hex[:8]

    document = await client.put(
        "/v1/documents/iso-secret",
        headers=owner,
        json={"content": "tenant A only", "expected_version": 0},
    )
    assert document.status_code == 200, document.text
    task_key = f"iso-task-{suffix}"
    task = await client.post(
        "/v1/tasks",
        headers=owner,
        json={"task_key": task_key, "title": "isolated", "status": "READY"},
    )
    assert task.status_code in (200, 201), task.text

    assert (
        await client.get("/v1/documents/iso-secret", headers=outsider)
    ).status_code == 404
    assert (
        await client.get(f"/v1/tasks/{task_key}", headers=outsider)
    ).status_code == 404
    assert (
        await client.post(f"/v1/tasks/{task_key}/run", headers=outsider, json={})
    ).status_code == 404

    listed = await client.get("/v1/tasks", headers=outsider, params={"limit": 200})
    assert listed.status_code == 200, listed.text
    keys = {item.get("task_key") for item in listed.json().get("items", [])}
    assert task_key not in keys


@pytest.mark.integration
async def test_a_workspace_key_cannot_reach_the_admin_plane(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    workspace = await _workspace(client, admin_headers)
    assert (
        await client.get("/v1/admin/workspaces", headers=workspace)
    ).status_code in {401, 403}
    assert (
        await client.post(
            "/v1/admin/workspaces",
            headers=workspace,
            json={"name": "x", "tier": "developer_5usd"},
        )
    ).status_code in {401, 403}
