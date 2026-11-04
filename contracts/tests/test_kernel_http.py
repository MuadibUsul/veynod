"""The one Kernel transport: auth, retries, refusal codes, no leaked secrets."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from contracts import kernel_http


def _run(handler, **kwargs):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await kernel_http.request(client, "POST", "http://kernel/x",
                                             backoff_s=0.0, **kwargs)
    return asyncio.run(go())


def test_exactly_one_credential_is_sent() -> None:
    auth = kernel_http.KernelAuth(workspace_id="w", service_key="s", bearer="b")
    assert auth.headers("k") == {"X-Workspace-Id": "w", "X-Veynod-Service-Key": "s",
                                 "Idempotency-Key": "k"}
    assert kernel_http.KernelAuth(bearer="b").headers() == {"Authorization": "Bearer b"}


def test_overload_is_retried_and_a_refusal_is_not() -> None:
    calls = []

    def flaky(request):
        calls.append(1)
        return httpx.Response(503 if len(calls) < 3 else 200, json={"ok": True})

    assert _run(flaky, auth=kernel_http.KernelAuth(), retries=2) == (200, {"ok": True})
    assert len(calls) == 3

    refused = []

    def conflict(request):
        refused.append(1)
        return httpx.Response(409, json={"detail": {"error": "submission_in_progress",
                                                    "lease_token": "vnl_secret"}})

    with pytest.raises(kernel_http.KernelHTTPError) as raised:
        _run(conflict, auth=kernel_http.KernelAuth(), retries=3)
    assert len(refused) == 1
    assert raised.value.status == 409 and raised.value.code == "submission_in_progress"
    assert "vnl_secret" not in str(raised.value)
    assert "lease_token" not in raised.value.payload["detail"]


def test_an_unreachable_kernel_is_status_zero() -> None:
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(kernel_http.KernelHTTPError) as raised:
        _run(down, auth=kernel_http.KernelAuth(), retries=1)
    assert raised.value.unreachable and raised.value.retryable
    assert raised.value.code == "kernel_unreachable"


def test_error_codes_in_every_shape_the_kernel_uses() -> None:
    code = kernel_http.error_code
    assert code({"detail": {"error": "lease_not_active"}}) == "lease_not_active"
    assert code({"error": {"code": "validation_error"}}) == "validation_error"
    assert code({"error": "not a code; DROP"}) is None
    assert code("text") is None
