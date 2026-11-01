"""Async SQLAlchemy engine/session factory and genesis schema bootstrap."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.services.runtime_metrics import InstrumentedAsyncAdaptedQueuePool

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_engine_url: str | None = None


def _create_engine(url: str) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=1800,
        poolclass=InstrumentedAsyncAdaptedQueuePool,
    )


def get_engine() -> AsyncEngine:
    global _engine, _session_factory, _engine_url
    if _engine is None:
        _engine_url = settings.async_database_url
        _engine = _create_engine(_engine_url)
        _session_factory = async_sessionmaker(
            _engine, class_=AsyncSession, expire_on_commit=False
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


async def configure_database(url: str) -> None:
    """Point the process at another database (used by integration tests)."""
    global _engine, _session_factory, _engine_url
    if _engine is not None:
        await _engine.dispose()
    _engine_url = url
    _engine = _create_engine(url)
    _session_factory = async_sessionmaker(
        _engine, class_=AsyncSession, expire_on_commit=False
    )


async def dispose_database() -> None:
    global _engine, _session_factory, _engine_url
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
    _engine_url = None


async def get_db_session() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            # A request that only issued SELECTs must not leave an
            # "idle in transaction" connection behind.
            await session.rollback()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Short-lived session for background loops and scripts."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def _asyncpg_dsn() -> str:
    url = _engine_url or settings.async_database_url
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def init_db() -> None:
    """Apply the idempotent genesis DDL and every versioned migration.

    Each file contains ``DO`` blocks and multiple statements, so they are
    executed through a raw asyncpg connection (SQLAlchemy's prepared-statement
    path does not allow multi-command scripts).  Migrations are ordered by
    filename and must remain idempotent so existing databases can be upgraded
    simply by restarting the kernel.
    """
    if not settings.auto_init_db:
        # Still create the pool entrypoint so the app fails early on a bad URL.
        get_engine()
        return

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        # API instances start concurrently and apply the same idempotent
        # migration list.  CREATE EXTENSION is not race-free even with IF NOT
        # EXISTS, so serialize schema initialization with a session lock.
        await conn.execute(
            "SELECT pg_advisory_lock(hashtextextended('veynod:schema', 0))"
        )
        try:
            for path in settings.schema_paths:
                await conn.execute(path.read_text(encoding="utf-8"))
        finally:
            await conn.execute(
                "SELECT pg_advisory_unlock(hashtextextended('veynod:schema', 0))"
            )
    finally:
        await conn.close()
    get_engine()
