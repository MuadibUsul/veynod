from __future__ import annotations

import json

import httpx
import pytest

from agent_runtime.gateway import (
    HTTPModelGateway,
    HTTPToolRuntime,
    ModelGatewayConfig,
    ModelGatewayError,
    ToolGatewayConfig,
)


@pytest.mark.asyncio
async def test_http_model_gateway_parses_action_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "kind": "tool",
                                    "tool": "read_file",
                                    "arguments": {"path": "a.py"},
                                    "idempotency_key": "read-1",
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )

    gateway = HTTPModelGateway(
        ModelGatewayConfig(
            base_url="http://model.test/v1",
            api_key="secret",
            model="test-model",
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        action = await gateway.decide({"run_id": "r1"}, {"step": 1})
    finally:
        await gateway.close()
    assert action.kind == "tool"
    assert action.tool == "read_file"
    assert action.prompt_tokens == 11
    assert action.completion_tokens == 7
    assert action.idempotency_key == "read-1"


@pytest.mark.asyncio
async def test_http_model_gateway_rejects_non_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not json"}}]},
        )

    gateway = HTTPModelGateway(
        ModelGatewayConfig(
            base_url="http://model.test/v1",
            api_key="secret",
            model="test-model",
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ModelGatewayError):
            await gateway.decide({"run_id": "r1"}, {})
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_http_tool_runtime_enforces_allowlist_and_idempotency() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.url.path,
                request.headers.get("idempotency-key", ""),
            )
        )
        return httpx.Response(200, json={"ok": True, "output": {"done": True}})

    tools = HTTPToolRuntime(
        ToolGatewayConfig(
            base_url="http://tools.test",
            allowed_tools=frozenset({"echo"}),
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        denied = await tools.execute("shell", {}, "id-1")
        assert denied.ok is False
        assert not seen

        result = await tools.execute("echo", {"x": 1}, "id-1")
        assert result.ok is True
        assert result.output == {"done": True}
        assert seen == [("/tools/echo", "id-1")]
    finally:
        await tools.close()


@pytest.mark.asyncio
async def test_http_tool_runtime_reports_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad arguments")

    tools = HTTPToolRuntime(
        ToolGatewayConfig(
            base_url="http://tools.test",
            allowed_tools=frozenset({"echo"}),
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await tools.execute("echo", {}, "id-2")
    finally:
        await tools.close()
    assert result.ok is False
    assert "400" in (result.error or "")
