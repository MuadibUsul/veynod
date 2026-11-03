"""Run metadata cannot choose a Kernel host or process secret."""

from __future__ import annotations

import pytest

from agent_runtime.tool_context import ToolContext
from agent_runtime.tools.kernel import (
    KernelBinding,
    KernelToolConfig,
    KernelToolConfigError,
    KernelToolRuntime,
)


@pytest.mark.asyncio
async def test_run_binding_requires_configured_host_and_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KERNEL_URL", "http://kernel-api:8001")
    monkeypatch.delenv("KERNEL_ALLOWED_BASE_URLS", raising=False)
    monkeypatch.delenv("KERNEL_ALLOWED_CREDENTIAL_REFS", raising=False)
    runtime = KernelToolRuntime(KernelToolConfig(default_binding=KernelBinding(
        "http://kernel-api:8001", "workspace")))
    try:
        def context(url: str, ref: str) -> ToolContext:
            return ToolContext("run", "tenant", "workspace", kernel={
                "base_url": url, "workspace_id": "workspace", "credential_ref": ref})

        assert runtime._binding_from_run(context(
            "http://kernel-api:8001", "KERNEL_RUNTIME_TOKEN")) is not None
        monkeypatch.setenv("RUNTIME_ENVIRONMENT", "production")
        assert runtime._binding_from_run(ToolContext(
            "run", "tenant", "workspace")) is None
        with pytest.raises(KernelToolConfigError):
            runtime._binding_from_run(context(
                "http://kernel-api:8001", "KERNEL_RUNTIME_TOKEN"))
        monkeypatch.setenv("RUNTIME_ENVIRONMENT", "development")
        with pytest.raises(KernelToolConfigError):
            runtime._binding_from_run(context(
                "https://attacker.example", "KERNEL_RUNTIME_TOKEN"))
        with pytest.raises(KernelToolConfigError):
            runtime._binding_from_run(context(
                "http://kernel-api:8001", "MODEL_API_KEY"))
    finally:
        await runtime.close()
