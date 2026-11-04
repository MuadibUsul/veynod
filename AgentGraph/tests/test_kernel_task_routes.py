"""The task-scoped Kernel routes look the task up before anything else.

``_product_task`` once referenced ``task_lifecycle`` without importing it
(fixed in #91), so every call to these three routes answered 500 (NameError)
instead of reaching the lookup. Lint caught it; nothing exercised the routes.
An unknown task must be a plain 404.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("POST", "kernel/lease"), ("POST", "kernel/submit"), ("GET", "kernel/delivery")],
)
async def test_kernel_routes_answer_404_for_an_unknown_task(client, workspace, method, suffix):
    response = await client.request(
        method,
        f"/v1/tasks/TASK-NOSUCHTASK/{suffix}",
        headers=workspace["headers"],
        json={} if method == "POST" else None,
    )
    assert response.status_code == 404, response.text
