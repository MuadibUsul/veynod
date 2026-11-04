"""HTTP surface of the Host -> Kernel delivery path (runner.mjs calls these).

The routes had no HTTP test, and a missing module import made every call a
500 before the service code ran. These assert the routes reach the delivery
service and answer with its typed errors.
"""

from __future__ import annotations

import uuid

import httpx


async def _task(client: httpx.AsyncClient, workspace: dict) -> str:
    key = f"KD-{uuid.uuid4().hex[:8]}"
    made = await client.post(
        "/v1/tasks", headers=workspace["headers"], json={"task_key": key, "title": "kd"}
    )
    assert made.status_code == 201, made.text
    return key


async def test_kernel_lease_without_binding_is_a_typed_409(
    client: httpx.AsyncClient, workspace: dict
) -> None:
    key = await _task(client, workspace)
    response = await client.post(
        f"/v1/tasks/{key}/kernel/lease", headers=workspace["headers"], json={}
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "kernel_unbound"


async def test_kernel_delivery_view_for_an_unbound_task(
    client: httpx.AsyncClient, workspace: dict
) -> None:
    key = await _task(client, workspace)
    response = await client.get(
        f"/v1/tasks/{key}/kernel/delivery", headers=workspace["headers"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "absent"


async def test_kernel_routes_404_for_an_unknown_task(
    client: httpx.AsyncClient, workspace: dict
) -> None:
    response = await client.post(
        "/v1/tasks/NOPE-404/kernel/submit",
        headers=workspace["headers"],
        json={"git_diff": "", "touched_symbols": []},
    )
    assert response.status_code == 404, response.text
