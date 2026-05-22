"""Tests for shadow persistence.

These tests use sqlite+aiosqlite as a transient in-memory backend to verify
the persist function constructs correct SQL and runs without error.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.models.auction import Auction
from src.persistence.models import ShadowAuction, ShadowSolution
from src.shadow.persist import persist_shadow_attempt, persist_shadow_attempt_safe
from src.solver.orchestrator import AttemptRecord


@pytest.fixture
async def session_factory(monkeypatch):
    """In-memory sqlite engine, schema created fresh per test.

    SQLite doesn't autoincrement BIGINT columns (only INTEGER), so we
    create the tables with raw DDL that uses INTEGER for the id columns.
    """
    from sqlalchemy import text

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # Create tables with INTEGER (not BIGINT) so sqlite autoincrement works
        await conn.execute(text("""
            CREATE TABLE shadow_auctions (
                auction_id INTEGER PRIMARY KEY,
                chain TEXT NOT NULL DEFAULT 'arbitrum_one',
                polled_at TEXT NOT NULL,
                deadline TEXT,
                n_orders INTEGER NOT NULL,
                raw_competition TEXT NOT NULL,
                raw_auction TEXT NOT NULL
            )
        """))
        await conn.execute(text("""
            CREATE TABLE shadow_solutions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                auction_id INTEGER NOT NULL REFERENCES shadow_auctions(auction_id),
                strategy TEXT NOT NULL,
                status TEXT NOT NULL,
                latency_ms INTEGER,
                solution TEXT,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.shadow.persist.get_session_factory", lambda: factory)
    yield factory
    await engine.dispose()


def _auction(auction_id: str = "1234") -> Auction:
    return Auction(
        id=auction_id,
        tokens={},
        orders=[],
        liquidity=[],
        effectiveGasPrice=0,
        deadline=None,
    )


@pytest.mark.asyncio
async def test_persist_writes_auction_and_solutions(session_factory) -> None:
    auction = _auction("1234")
    attempts = [
        AttemptRecord(
            strategy="naive",
            status="solved",
            latency_ms=312,
            solution={"id": 1234},
            error=None,
        ),
        AttemptRecord(
            strategy="cow-matching-multi-party",
            status="no_solution",
            latency_ms=45,
            solution=None,
            error=None,
        ),
    ]

    await persist_shadow_attempt(auction, attempts, raw_competition={"test": True})

    async with session_factory() as session:
        auction_rows = (await session.execute(select(ShadowAuction))).scalars().all()
        assert len(auction_rows) == 1
        assert auction_rows[0].auction_id == 1234
        assert auction_rows[0].n_orders == 0
        assert auction_rows[0].raw_competition == {"test": True}

        sol_rows = (await session.execute(select(ShadowSolution))).scalars().all()
        assert len(sol_rows) == 2
        statuses = {r.strategy: r.status for r in sol_rows}
        assert statuses["naive"] == "solved"
        assert statuses["cow-matching-multi-party"] == "no_solution"


@pytest.mark.asyncio
async def test_persist_is_idempotent_on_auction(session_factory) -> None:
    """Calling persist twice for the same auction_id must not duplicate auction row."""
    auction = _auction("999")
    attempts = [
        AttemptRecord(
            strategy="naive", status="no_solution", latency_ms=10, solution=None, error=None
        ),
    ]

    await persist_shadow_attempt(auction, attempts)
    await persist_shadow_attempt(auction, attempts)  # second call — must not raise or duplicate

    async with session_factory() as session:
        rows = (await session.execute(select(ShadowAuction))).scalars().all()
        assert len(rows) == 1  # still only one auction row


@pytest.mark.asyncio
async def test_persist_records_error_attempt(session_factory) -> None:
    auction = _auction("555")
    attempts = [
        AttemptRecord(
            strategy="naive",
            status="error",
            latency_ms=50,
            solution=None,
            error="something went wrong",
        ),
    ]

    await persist_shadow_attempt(auction, attempts)

    async with session_factory() as session:
        sol_rows = (await session.execute(select(ShadowSolution))).scalars().all()
        assert len(sol_rows) == 1
        assert sol_rows[0].status == "error"
        assert sol_rows[0].error == "something went wrong"


@pytest.mark.asyncio
async def test_persist_safe_swallows_errors(monkeypatch) -> None:
    """persist_shadow_attempt_safe must never raise even if underlying call fails."""

    async def broken(*args, **kwargs) -> None:
        raise RuntimeError("DB down")

    monkeypatch.setattr("src.shadow.persist.persist_shadow_attempt", broken)
    auction = _auction("1")
    # Must NOT raise
    await persist_shadow_attempt_safe(auction, [], None)
