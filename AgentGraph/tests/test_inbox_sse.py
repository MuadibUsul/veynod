"""Inbox SSE lifetime, heartbeat, and 429 Retry-After. No database."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.api import inbox_stream
from app.api.stream_routes import _acquire_sse, _release_sse
from app.config import get_settings
from app.errors import ApiError


def test_lifetime_jitter_is_zero_to_300_seconds():
    assert inbox_stream.inbox_lifetime_seconds(3600, roll=lambda: 0) == 3600
    assert inbox_stream.inbox_lifetime_seconds(3600, roll=lambda: 1) == 3900
    assert inbox_stream.LIFETIME_JITTER_SECONDS == 300
    assert inbox_stream.HEARTBEAT_SECONDS == 20


def test_inbox_sse_headers_disable_proxy_buffering():
    headers = inbox_stream.SSE_HEADERS
    assert "no-cache" in headers["Cache-Control"]
    assert "no-transform" in headers["Cache-Control"]
    assert headers["X-Accel-Buffering"] == "no"
    assert headers["Connection"] == "keep-alive"
    frame = inbox_stream.planned_close_frame("max_lifetime")
    assert frame.startswith("retry: 3000\n")
    assert "event: planned-close\n" in frame
    assert "max_lifetime" in frame


async def test_capacity_429_sends_retry_after():
    principal = SimpleNamespace(
        workspace=SimpleNamespace(id="ws-sse-capacity"),
        auth_kind=SimpleNamespace(value="agent"),
        token_id="tok-sse-capacity",
        agent_signature="sig",
    )
    settings = get_settings()
    previous = settings.sse_max_connections_per_principal
    settings.sse_max_connections_per_principal = 1
    try:
        await _acquire_sse(principal)
        with pytest.raises(ApiError) as caught:
            await _acquire_sse(principal)
    finally:
        settings.sse_max_connections_per_principal = previous
        _release_sse(principal)
    error = caught.value
    assert error.status_code == 429
    assert error.code == "sse_capacity"
    assert error.headers["Retry-After"] == "60"


async def test_idle_stream_pings_then_planned_close_and_unsubscribes(monkeypatch):
    flags = {"entered": False, "exited": False, "closed": 0, "pending": 0}

    async def identities(_principal):
        return "agent:me", ["agent:me"], 0

    async def presence_open(*_args, **_kwargs):
        return None

    async def presence_close(_principal):
        flags["closed"] += 1

    async def presence_touch(_principal):
        return None

    async def pending(*_args, **_kwargs):
        flags["pending"] += 1
        return []

    @asynccontextmanager
    async def subscribe():
        flags["entered"] = True
        try:
            yield asyncio.Queue()
        finally:
            flags["exited"] = True

    monkeypatch.setattr(inbox_stream, "_identities", identities)
    monkeypatch.setattr(inbox_stream, "_pending", pending)
    monkeypatch.setattr(inbox_stream.pokes, "presence_open", presence_open)
    monkeypatch.setattr(inbox_stream.pokes, "presence_close", presence_close)
    monkeypatch.setattr(inbox_stream.pokes, "presence_touch", presence_touch)
    monkeypatch.setattr(inbox_stream.event_bus, "subscribe", subscribe)
    monkeypatch.setattr(inbox_stream, "inbox_lifetime_seconds", lambda _base: 0.4)
    monkeypatch.setattr(inbox_stream, "HEARTBEAT_SECONDS", 0.05)

    principal = SimpleNamespace(
        workspace=SimpleNamespace(id="ws-ping"),
        auth_kind=SimpleNamespace(value="agent"),
        token_id="tok-ping",
        agent_signature="sig",
    )

    async def revalidate():
        return principal

    frames: list[str] = []
    async for frame in inbox_stream._poke_stream(principal, None, revalidate):
        frames.append(frame)

    text = "".join(frames)
    assert "event: connected\n" in text
    assert ": ping\n\n" in text
    assert "event: planned-close\n" in text
    assert "event: closed\n" in text
    assert flags["entered"] is True
    assert flags["exited"] is True
    assert flags["closed"] == 1
    assert flags["pending"] >= 1
