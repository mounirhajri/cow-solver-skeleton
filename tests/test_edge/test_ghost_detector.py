"""Tests for the dynamic ghost-order detector."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from edge.matching.ghost_detector import (
    DynamicGhostDetector,
    NoOpGhostDetector,
)
from src.models.order import Order


def _mk_order(uid: str) -> Order:
    return Order(
        uid=uid,
        sellToken="0x" + "a" * 40,
        buyToken="0x" + "b" * 40,
        sellAmount=1000,
        buyAmount=800,
        feePolicies=[],
        validTo=999999,
        kind="sell",
        owner="0x" + "c" * 40,
        partiallyFillable=False,
        **{"class": "limit"},
    )


@pytest.fixture  # type: ignore[misc]
async def session_factory() -> async_sessionmaker:  # type: ignore[type-arg]
    """In-memory sqlite with the ghost_orders table only — matcher path needs nothing else."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE ghost_orders (
                uid TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                sell_token TEXT NOT NULL,
                buy_token TEXT NOT NULL,
                n_auctions_seen INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                last_refreshed_at TEXT NOT NULL
            )
        """))
    factory: async_sessionmaker = async_sessionmaker(engine, expire_on_commit=False)  # type: ignore[type-arg]
    yield factory
    await engine.dispose()


async def _insert_uid(factory: async_sessionmaker, uid: str) -> None:  # type: ignore[type-arg]
    async with factory() as s:
        await s.execute(
            text("""
                INSERT INTO ghost_orders
                  (uid, owner, sell_token, buy_token, n_auctions_seen,
                   first_seen_at, last_seen_at, detected_at, last_refreshed_at)
                VALUES (:uid, '0xowner', '0xs', '0xb', 25,
                        '2026-05-26', '2026-05-26', '2026-05-26', '2026-05-26')
            """),
            {"uid": uid},
        )
        await s.commit()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_noop_detector_never_flags() -> None:
    """NoOpGhostDetector is the safe-default fallback used when DB unavailable."""
    d = NoOpGhostDetector()
    assert await d.is_ghost(_mk_order("anything")) is False


@pytest.mark.asyncio  # type: ignore[misc]
async def test_dynamic_detector_returns_true_for_db_uid(
    session_factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    """A UID present in ghost_orders is flagged on first call."""
    await _insert_uid(session_factory, "ghost-uid-1")
    d = DynamicGhostDetector(session_factory=session_factory)
    assert await d.is_ghost(_mk_order("ghost-uid-1")) is True
    assert await d.is_ghost(_mk_order("real-uid-1")) is False


@pytest.mark.asyncio  # type: ignore[misc]
async def test_dynamic_detector_caches_within_ttl(
    session_factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    """Inserts after the first refresh are NOT seen until TTL expires.

    Guarantees the cache mechanism is actually caching — without it, every
    is_ghost() call would hit the DB and pick up the new row immediately.
    """
    await _insert_uid(session_factory, "g1")
    d = DynamicGhostDetector(session_factory=session_factory, ttl_seconds=3600)
    assert await d.is_ghost(_mk_order("g1")) is True

    # Insert another ghost AFTER cache load — must not be visible until refresh
    await _insert_uid(session_factory, "g2")
    assert await d.is_ghost(_mk_order("g2")) is False
    assert d.cache_size == 1


@pytest.mark.asyncio  # type: ignore[misc]
async def test_dynamic_detector_refresh_when_ttl_expires(
    session_factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    """ttl_seconds=0 forces a refresh on every call → new inserts visible."""
    await _insert_uid(session_factory, "g1")
    d = DynamicGhostDetector(session_factory=session_factory, ttl_seconds=0)
    assert await d.is_ghost(_mk_order("g1")) is True

    await _insert_uid(session_factory, "g2")
    assert await d.is_ghost(_mk_order("g2")) is True
    assert d.cache_size == 2


@pytest.mark.asyncio  # type: ignore[misc]
async def test_dynamic_detector_preserves_cache_on_db_error() -> None:
    """If the DB read fails, the prior cache is kept (graceful degradation)."""

    class _FailingFactory:
        def __call__(self) -> object:  # noqa: D401
            raise RuntimeError("simulated DB outage")

    d = DynamicGhostDetector(session_factory=_FailingFactory(), ttl_seconds=0)
    # First call: cache empty + DB fails → falls back to empty set (no false flags)
    assert await d.is_ghost(_mk_order("anything")) is False
    assert d.cache_size == 0
