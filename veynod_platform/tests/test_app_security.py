"""The deprecated gateway cannot start or write without operator opt-in."""

import httpx
import pytest

from veynod_platform.app import app


@pytest.mark.asyncio
async def test_legacy_gateway_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("VN_ENABLE_LEGACY_PLATFORM", raising=False)
    with pytest.raises(RuntimeError, match="disabled"):
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.asyncio
async def test_platform_write_requires_api_key(monkeypatch):
    monkeypatch.setenv("PLATFORM_API_KEY", "x" * 40)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        response = await client.post("/v1/platform/code-runs", json={})
    assert response.status_code == 401
