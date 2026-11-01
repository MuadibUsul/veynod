"""Machine (Runner) token authentication.

Tokens are opaque ``vnk_...`` strings; only their SHA-256 hash is stored. The
worker hub id is deterministically derived from the hash, which keeps the same
runner identity across reconnects and matches the legacy v3 behaviour.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AgentToken, Workspace

TOKEN_PREFIX = "vnk_"

_token_cache: dict[str, tuple["RunnerIdentity", float]] = {}
_token_warm_lock = asyncio.Lock()


@dataclass(frozen=True)
class RunnerIdentity:
    worker_id: str
    workspace_id: UUID | None = None


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def derive_worker_hub_id(raw_token: str) -> str:
    return f"hub_{hash_token(raw_token)[:16]}"


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


async def register_token(
    session: AsyncSession,
    raw_token: str,
    label: str = "runner",
    *,
    workspace_id: UUID | None = None,
) -> AgentToken:
    """Idempotently register an existing raw token.

    New tokens are bound to ``workspace_id`` (or the configured default
    workspace) when that workspace exists.  Legacy rows without a binding keep
    ``workspace_id`` null; callers can still read their scope from the row.
    """
    token_hash = hash_token(raw_token)
    existing = (
        await session.execute(select(AgentToken).where(AgentToken.token_hash == token_hash))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    target_workspace = workspace_id or settings.default_workspace_id
    if target_workspace is not None:
        exists = await session.scalar(
            select(Workspace.workspace_id).where(
                Workspace.workspace_id == target_workspace
            )
        )
        if exists is None:
            target_workspace = None

    record = AgentToken(
        worker_hub_id=derive_worker_hub_id(raw_token),
        label=label,
        token_hash=token_hash,
        active=True,
        workspace_id=target_workspace,
    )
    session.add(record)
    await session.flush()
    return record


async def issue_token(
    session: AsyncSession,
    label: str = "runner",
    *,
    workspace_id: UUID | None = None,
) -> tuple[str, AgentToken]:
    raw = new_token()
    record = await register_token(
        session, raw, label, workspace_id=workspace_id
    )
    await session.commit()
    return raw, record


async def authenticate_worker_context(
    session: AsyncSession, raw_token: str
) -> RunnerIdentity | None:
    """Return the token's runner identity and bound workspace, or None."""
    if not raw_token.startswith(TOKEN_PREFIX):
        return None

    token_hash = hash_token(raw_token)
    now = time.monotonic()
    cached = _token_cache.get(token_hash)
    if cached is not None and cached[1] > now:
        if settings.require_bound_tokens and cached[0].workspace_id is None:
            return None
        return cached[0]

    # A cold API plane gets one bulk warmup instead of one PK query per runner.
    async with _token_warm_lock:
        cached = _token_cache.get(token_hash)
        if cached is not None and cached[1] > time.monotonic():
            if settings.require_bound_tokens and cached[0].workspace_id is None:
                return None
            return cached[0]
        if not _token_cache:
            try:
                await warm_token_cache(session)
            except Exception:  # noqa: BLE001 - fall back to the single-token query
                pass
        cached = _token_cache.get(token_hash)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]

    started = time.perf_counter()
    record = (
        await session.execute(select(AgentToken).where(AgentToken.token_hash == token_hash))
    ).scalar_one_or_none()
    try:
        from app.services.runtime_metrics import get_runtime_metrics

        get_runtime_metrics().observe(
            "auth.ms", (time.perf_counter() - started) * 1000
        )
    except Exception:  # noqa: BLE001 - metrics must not break auth
        pass

    if record is not None:
        if not record.active:
            return None
        if record.workspace_id is None and settings.require_bound_tokens:
            return None
        identity = RunnerIdentity(
            worker_id=record.worker_hub_id,
            workspace_id=record.workspace_id,
        )
        if len(_token_cache) > 20_000:
            _token_cache.clear()
        _token_cache[token_hash] = (
            identity, now + settings.auth_cache_ttl_seconds
        )
        return identity

    if settings.auth_mode == "dev":
        if settings.require_bound_tokens and settings.default_workspace_id is None:
            return None
        return RunnerIdentity(
            worker_id=derive_worker_hub_id(raw_token),
            workspace_id=settings.default_workspace_id,
        )
    return None


async def authenticate_worker_token(
    session: AsyncSession, raw_token: str
) -> str | None:
    """Backward-compatible worker id lookup used by the syscall plane."""
    identity = await authenticate_worker_context(session, raw_token)
    return identity.worker_id if identity is not None else None


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))



async def warm_token_cache(session: AsyncSession, limit: int = 20_000) -> int:
    """Prefetch active runner identities so a cold API plane has no auth storm."""
    from sqlalchemy import select as _select

    rows = (
        await session.execute(
            _select(
                AgentToken.token_hash,
                AgentToken.worker_hub_id,
                AgentToken.workspace_id,
            )
            .where(AgentToken.active.is_(True))
            .limit(limit)
        )
    ).all()
    now = time.monotonic()
    for token_hash, worker_hub_id, workspace_id in rows:
        _token_cache[token_hash] = (
            RunnerIdentity(worker_id=worker_hub_id, workspace_id=workspace_id),
            now + settings.auth_cache_ttl_seconds,
        )
    return len(rows)


def clear_token_cache() -> None:
    """Drop cached identities after revoke/rotate so the change is immediate."""
    _token_cache.clear()
