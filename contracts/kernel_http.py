"""One HTTP transport to the Kernel for every caller in the repository.

The product API (code engine, Host delivery) and the Agent Runtime each had
their own Kernel client: their own headers, their own error parsing, their own
retry rule. They drifted: one folded every rejection into a generic code (the
runner could not tell "wait for the gate" from "lease again"), one retried and
the others did not, and only one kept lease tokens out of error messages.

This module is the shared part: how a request is authenticated, sent,
retried and how a refusal is described. Each caller keeps its own domain
error type by catching :class:`KernelHTTPError`.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import httpx

#: Answers worth one more try: overload, a restart, a proxy cut.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_CODE = re.compile(r"[a-z][a-z0-9_]{1,63}")
_SECRET = re.compile(r"token|secret|password|api_key|authorization", re.IGNORECASE)


@dataclass(frozen=True)
class KernelAuth:
    """Who is calling. Exactly one credential is sent."""

    workspace_id: str | None = None
    admin_key: str | None = None
    service_key: str | None = None
    bearer: str | None = None

    def headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.workspace_id:
            headers["X-Workspace-Id"] = self.workspace_id
        if self.admin_key:
            headers["X-Admin-Key"] = self.admin_key
        elif self.service_key:
            headers["X-Veynod-Service-Key"] = self.service_key
        elif self.bearer:
            headers["Authorization"] = f"Bearer {self.bearer}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers


class KernelHTTPError(RuntimeError):
    """The Kernel refused, or could not be reached (``status`` 0)."""

    def __init__(self, status: int, code: str | None, message: str,
                 payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload or {}

    @property
    def unreachable(self) -> bool:
        return self.status == 0

    @property
    def retryable(self) -> bool:
        return self.status == 0 or self.status in RETRYABLE_STATUS


def redact(value: Any) -> Any:
    """Drop anything that looks like a credential before it reaches a message or log."""
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items() if not _SECRET.search(str(key))}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def error_code(payload: Any) -> str | None:
    """The Kernel's own code: ``{"detail": {"error": code}}``, ``{"error": code}``
    or ``{"error": {"code": code}}``."""
    if not isinstance(payload, dict):
        return None
    for holder in (payload.get("detail"), payload):
        if not isinstance(holder, dict):
            continue
        code = holder.get("error", holder.get("code"))
        if isinstance(code, dict):
            code = code.get("code")
        if isinstance(code, str) and _CODE.fullmatch(code):
            return code
    return None


def parse(response: httpx.Response) -> Any:
    """The JSON body as sent (object or list); ``{}`` when empty, text as ``detail``."""
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {"detail": response.text[:500]}


def describe(status: int, payload: Any) -> str:
    detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
    return f"Kernel HTTP {status}: {str(redact(detail))[:800]}"


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    auth: KernelAuth,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    idempotency_key: str | None = None,
    timeout: float | None = None,
    retries: int = 0,
    backoff_s: float = 0.5,
) -> tuple[int, Any]:
    """Send one call; retry overload and transport failures ``retries`` times.

    Returns ``(status, body)`` for a 2xx/3xx answer and raises
    :class:`KernelHTTPError` otherwise.
    """
    headers = auth.headers(idempotency_key)
    last: KernelHTTPError | None = None
    for attempt in range(retries + 1):
        try:
            response = await client.request(
                method, url, headers=headers, params=params, json=json_body,
                **({"timeout": timeout} if timeout is not None else {}))
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = KernelHTTPError(0, "kernel_unreachable", f"Kernel unreachable: {exc}")
        else:
            payload = parse(response)
            if response.status_code < 400:
                return response.status_code, payload
            last = KernelHTTPError(
                response.status_code, error_code(payload), describe(response.status_code, payload),
                redact(payload) if isinstance(payload, dict) else {"items": payload})
            if response.status_code not in RETRYABLE_STATUS:
                raise last
        if attempt < retries:
            await asyncio.sleep(min(8.0, backoff_s * (2 ** attempt)))
    assert last is not None
    raise last


__all__ = [
    "RETRYABLE_STATUS",
    "KernelAuth",
    "KernelHTTPError",
    "describe",
    "error_code",
    "parse",
    "redact",
    "request",
]
