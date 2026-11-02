from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from agent_runtime.tool_context import ToolContext, tool_context
from agent_runtime.tools.kernel import KernelToolConfig, KernelToolRuntime
from contracts.work_scope import WorkScope


@pytest.mark.asyncio
async def test_run_scope_overrides_model_write_set_on_kernel_creation(monkeypatch) -> None:
    monkeypatch.setenv("TEST_SCOPE_KERNEL_TOKEN", "vnk_test_scope_token")
    scope = WorkScope.from_legacy_paths(["src/allowed/**"])
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"task_id": str(uuid4()), "status": "READY"})

    runtime = KernelToolRuntime(
        KernelToolConfig(service_key="test-service"),
        transport=httpx.MockTransport(handler),
    )
    context = ToolContext(
        run_id=str(uuid4()),
        tenant_id=str(uuid4()),
        workspace_id=str(uuid4()),
        product_task_id="DG-d-api",
        kernel={
            "base_url": "http://kernel.test",
            "workspace_id": str(uuid4()),
            "credential_ref": "TEST_SCOPE_KERNEL_TOKEN",
            "work_scope": scope.to_dict(),
        },
    )
    try:
        with tool_context(context):
            result = await runtime.execute(
                "create_task",
                {
                    "title": "Implement API",
                    "spec_prompt": "Implement the frozen Diagram API chunk.",
                    "write_set_symbols": ["src/forbidden.py"],
                },
                "scope-create-1",
            )
        assert result.ok, result
        assert seen[0]["work_scope"] == scope.to_dict()
        assert seen[0]["write_set_symbols"] == []
    finally:
        await runtime.close()
