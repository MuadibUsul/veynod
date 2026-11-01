"""HTTP adapters for the Agent Runtime (Phase 2E).

These are real adapters, not test doubles: an OpenAI-compatible chat endpoint
and an HTTP tool endpoint.  They keep credentials out of logs and enforce a
tool allowlist.  Production deployments should put them behind a proper model
gateway/tool gateway with quotas and audit.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .loop import ModelAction, ToolResult

logger = logging.getLogger("agent_runtime.gateway")


class ModelGatewayError(RuntimeError):
    pass


@dataclass
class ModelGatewayConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 30.0
    max_retries: int = 2
    temperature: float = 0.0


class HTTPModelGateway:
    """OpenAI-compatible ``/chat/completions`` adapter.

    The model is instructed to return exactly one JSON object matching
    ``ModelAction``.  No tool is executed here.
    """

    def __init__(
        self,
        config: ModelGatewayConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            timeout=config.timeout_seconds, transport=transport
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def decide(self, run: dict[str, Any], context: dict[str, Any]) -> ModelAction:
        prompt = (
            "You are the decision step of a coding Agent Run. Return exactly one "
            "JSON object with keys: kind (tool|finish|pause), tool (string|null), "
            "arguments (object), idempotency_key (string|null), output (object), "
            "reason (string). Do not include markdown fences or prose."
        )
        payload = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "run": {
                                "run_id": run.get("run_id"),
                                "product_task_id": run.get("product_task_id"),
                                "goal": run.get("goal"),
                                "budget": run.get("budget"),
                                "usage": run.get("usage"),
                            },
                            "checkpoint": context,
                        },
                        default=str,
                    ),
                },
            ],
        }
        data = await self._post(payload)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelGatewayError("model response has no message content") from exc
        action_data = _parse_action(content)
        usage = data.get("usage") or {}
        return ModelAction(
            kind=str(action_data.get("kind") or ""),
            tool=action_data.get("tool"),
            arguments=action_data.get("arguments") or {},
            idempotency_key=action_data.get("idempotency_key"),
            output=action_data.get("output") or {},
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cost_usd=float(action_data.get("cost_usd") or 0.0),
            reason=str(action_data.get("reason") or ""),
        )

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = await self._client.post(
                    url, headers=headers, json=payload
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
            else:
                if response.status_code in {429, 500, 502, 503, 504}:
                    last_error = ModelGatewayError(
                        f"model provider HTTP {response.status_code}"
                    )
                elif response.status_code >= 400:
                    raise ModelGatewayError(
                        f"model provider HTTP {response.status_code}: "
                        f"{response.text[:500]}"
                    )
                else:
                    return response.json()
            if attempt < self.config.max_retries:
                await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))
        raise ModelGatewayError(f"model provider unavailable: {last_error}")


@dataclass
class ToolGatewayConfig:
    base_url: str
    api_key: str = ""
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    timeout_seconds: float = 60.0
    max_retries: int = 1


class HTTPToolRuntime:
    """HTTP tool executor with allowlist, timeout and idempotency header."""

    def __init__(
        self,
        config: ToolGatewayConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            timeout=config.timeout_seconds, transport=transport
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def execute(
        self, name: str, arguments: dict[str, Any], idempotency_key: str
    ) -> ToolResult:
        if name not in self.config.allowed_tools:
            return ToolResult(False, error=f"tool {name!r} is not allowed")
        url = self.config.base_url.rstrip("/") + f"/tools/{name}"
        headers = {"Idempotency-Key": idempotency_key}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = await self._client.post(
                    url, headers=headers, json={"arguments": arguments}
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
            else:
                if response.status_code in {429, 500, 502, 503, 504}:
                    last_error = RuntimeError(
                        f"tool endpoint HTTP {response.status_code}"
                    )
                elif response.status_code >= 400:
                    return ToolResult(
                        False,
                        error=(
                            f"tool endpoint HTTP {response.status_code}: "
                            f"{response.text[:500]}"
                        ),
                    )
                else:
                    data = response.json()
                    ok = bool(data.get("ok"))
                    return ToolResult(
                        ok,
                        output=data.get("output") or {},
                        error=data.get("error"),
                    )
            if attempt < self.config.max_retries:
                await asyncio.sleep(min(5.0, 0.5 * (2**attempt)))
        return ToolResult(False, error=f"tool endpoint unavailable: {last_error}")


def _parse_action(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ModelGatewayError("model content is not a JSON object") from exc
        try:
            value = json.loads(text[start : end + 1])
        except (TypeError, ValueError) as exc2:
            raise ModelGatewayError("model content is not a JSON object") from exc2
    if not isinstance(value, dict) or not value.get("kind"):
        raise ModelGatewayError("model action must be a JSON object with kind")
    return value


__all__ = [
    "HTTPModelGateway",
    "HTTPToolRuntime",
    "ModelGatewayConfig",
    "ModelGatewayError",
    "ToolGatewayConfig",
]
