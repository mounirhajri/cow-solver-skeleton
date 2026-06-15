"""Async SQLAlchemy engine + session factory for Postgres."""

from __future__ import annotations

from functools import cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import settings


@cache
def get_engine() -> AsyncEngine:
    """Singleton async engine. Cached per-process."""
    return create_async_engine(
        settings.database_url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        # Defense-in-depth against the 2026-06-13 degradation (process decayed
        # into all-timeouts after ~1 day; the shared Postgres had crashed into
        # recovery on 06-11, which can leave a pool connection checked-out and
        # awaiting a reply that never comes). pool_recycle caps a connection's
        # lifetime so stale/half-dead ones are replaced; pool_timeout makes a
        # genuinely-exhausted pool FAIL FAST (raise) instead of hanging a
        # request forever — fail-open code paths then degrade gracefully
        # rather than silently stalling the whole solve.
        pool_recycle=1800,
        pool_timeout=10,
        echo=False,
    )


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)
