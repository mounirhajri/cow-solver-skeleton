"""Tests for scripts/sync_competitions.py.

The script uses ``curl_cffi.requests.AsyncSession`` for the live HTTP path
(CloudFront fronting api.cow.fi rate-limits Python-default-httpx TLS
fingerprints to 429 — verified 2026-05-25). Tests bypass the HTTP layer
entirely by monkeypatching ``_fetch_competition`` to return canned JSON
payloads. This decouples test coverage from the HTTP-library choice and
keeps tests fast.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.sync_competitions import _sync
from src.persistence.models import ShadowAuction, ShadowCompetitor

# Trimmed real-world example based on auction 7370873.
# 8 solvers exactly as described in the task spec.
SAMPLE_COMPETITION_RESPONSE: dict[str, Any] = {
    "auctionId": 7370873,
    "transactionHashes": ["0x" + "a" * 64],
    "auctionStartBlock": 123_456_789,
    "competitionSimulationBlock": 123_456_790,
    "solutions": [
        {
            "solver": "helixbox-solve",
            "solverAddress": "0x" + "1" * 40,
            "score": "1000000000000000782",
            "ranking": 1,
            "isWinner": True,
            "filteredOut": False,
            "clearingPrices": {"0x" + "a" * 40: "1000000000000000000"},
            "orders": [],
        },
        {
            "solver": "kaisersolver-solve",
            "solverAddress": "0x" + "2" * 40,
            "score": "1000000000000000000",
            "ranking": 2,
            "isWinner": False,
            "filteredOut": False,
            "clearingPrices": {},
            "orders": [],
        },
        {
            "solver": "portus",
            "solverAddress": "0x" + "3" * 40,
            "score": "999999999999999000",
            "ranking": 3,
            "isWinner": False,
            "filteredOut": False,
            "clearingPrices": {},
            "orders": [],
        },
        {
            "solver": "bitget-solve",
            "solverAddress": "0x" + "4" * 40,
            "score": "999999999999998000",
            "ranking": 4,
            "isWinner": False,
            "filteredOut": False,
            "clearingPrices": {},
            "orders": [],
        },
        {
            "solver": "okx-solve",
            "solverAddress": "0x" + "5" * 40,
            "score": "999999999999997000",
            "ranking": 5,
            "isWinner": False,
            "filteredOut": False,
            "clearingPrices": {},
            "orders": [],
        },
        {
            "solver": "baseline",
            "solverAddress": "0x" + "6" * 40,
            "score": "999999999999996000",
            "ranking": 6,
            "isWinner": False,
            "filteredOut": False,
            "clearingPrices": {},
            "orders": [],
        },
        {
            "solver": "zeroex-solve",
            "solverAddress": "0x" + "7" * 40,
            "score": "999999999999995000",
            "ranking": 7,
            "isWinner": False,
            "filteredOut": False,
            "clearingPrices": {},
            "orders": [],
        },
        {
            "solver": "arc-solve",
            "solverAddress": "0x" + "8" * 40,
            "score": None,
            "ranking": 8,
            "isWinner": False,
            "filteredOut": True,
            "clearingPrices": {},
            "orders": [],
        },
    ],
}


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
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
            CREATE TABLE shadow_competitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                auction_id INTEGER NOT NULL REFERENCES shadow_auctions(auction_id),
                solver_name TEXT NOT NULL,
                solver_address TEXT NOT NULL,
                score NUMERIC,
                ranking INTEGER NOT NULL,
                is_winner INTEGER NOT NULL DEFAULT 0,
                filtered_out INTEGER NOT NULL DEFAULT 0,
                clearing_prices TEXT NOT NULL DEFAULT '{}',
                orders TEXT NOT NULL DEFAULT '[]',
                polled_at TEXT NOT NULL,
                UNIQUE(auction_id, solver_address)
            )
        """))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _add_auction(factory: async_sessionmaker, auction_id: int = 7370873) -> None:
    async with factory() as session:
        session.add(
            ShadowAuction(
                auction_id=auction_id,
                polled_at=datetime.now(UTC),
                n_orders=10,
                raw_competition={},
                raw_auction={},
            )
        )
        await session.commit()


def _make_fetch_stub(
    response: dict[str, Any] | None,
    *,
    call_log: list[int] | None = None,
) -> Any:
    """Build an async stub for `_fetch_competition` returning a canned payload.

    Pass `response=None` to simulate 404. `call_log`, if given, captures
    the auction_id of each call so tests can assert N HTTP attempts.
    """
    async def _stub(_client: Any, auction_id: int) -> dict[str, Any] | None:
        if call_log is not None:
            call_log.append(auction_id)
        return response
    return _stub


async def test_inserts_8_rows_for_8_solvers(session_factory, monkeypatch) -> None:
    """All 8 solver bids in the sample response become rows in shadow_competitors."""
    await _add_auction(session_factory)

    async def _fast_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("scripts.sync_competitions.asyncio.sleep", _fast_sleep)
    monkeypatch.setattr(
        "scripts.sync_competitions._fetch_competition",
        _make_fetch_stub(SAMPLE_COMPETITION_RESPONSE),
    )

    result = await _sync(days=7, limit=100, session_factory=session_factory)

    assert result.n_fetched == 1
    assert result.n_inserted == 8
    assert result.n_skipped_404 == 0
    assert result.n_errors == 0

    async with session_factory() as session:
        rows = (await session.execute(select(ShadowCompetitor))).scalars().all()
    assert len(rows) == 8


async def test_idempotent_rerun_still_8_rows(session_factory, monkeypatch) -> None:
    """Running sync twice must not duplicate rows."""
    await _add_auction(session_factory)

    async def _fast_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("scripts.sync_competitions.asyncio.sleep", _fast_sleep)
    monkeypatch.setattr(
        "scripts.sync_competitions._fetch_competition",
        _make_fetch_stub(SAMPLE_COMPETITION_RESPONSE),
    )

    await _sync(days=7, limit=100, session_factory=session_factory)
    # Second run: auction now has rows → should be skipped by the LEFT JOIN filter.
    result2 = await _sync(days=7, limit=100, session_factory=session_factory)

    assert result2.n_fetched == 0  # already synced → nothing fetched

    async with session_factory() as session:
        rows = (await session.execute(select(ShadowCompetitor))).scalars().all()
    assert len(rows) == 8


async def test_404_skipped_no_exception(session_factory, monkeypatch) -> None:
    """404 from CoW API (stub returning None) must be skipped without exception."""
    await _add_auction(session_factory)

    async def _fast_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("scripts.sync_competitions.asyncio.sleep", _fast_sleep)
    monkeypatch.setattr(
        "scripts.sync_competitions._fetch_competition",
        _make_fetch_stub(None),  # None → simulates 404 path
    )

    result = await _sync(days=7, limit=100, session_factory=session_factory)

    assert result.n_fetched == 1
    assert result.n_skipped_404 == 1
    assert result.n_inserted == 0
    assert result.n_errors == 0

    async with session_factory() as session:
        rows = (await session.execute(select(ShadowCompetitor))).scalars().all()
    assert len(rows) == 0


async def test_rate_limit_throttle_uses_sleep(session_factory, monkeypatch) -> None:
    """The script calls asyncio.sleep between requests to pace at REQUESTS_PER_SECOND."""
    await _add_auction(session_factory, auction_id=100)
    await _add_auction(session_factory, auction_id=101)

    sleep_calls: list[float] = []

    async def _capture_sleep(s: float) -> None:
        sleep_calls.append(s)

    monkeypatch.setattr("scripts.sync_competitions.asyncio.sleep", _capture_sleep)
    monkeypatch.setattr(
        "scripts.sync_competitions._fetch_competition",
        _make_fetch_stub(SAMPLE_COMPETITION_RESPONSE),
    )

    await _sync(days=7, limit=100, session_factory=session_factory)

    # sleep is called once per auction when the HTTP call is faster than interval.
    # With 2 auctions and a mock that returns instantly, we expect 2 sleep calls.
    assert len(sleep_calls) == 2


async def test_winner_flag_and_score_persisted(session_factory, monkeypatch) -> None:
    """Verify winner flag and score are stored correctly."""
    await _add_auction(session_factory)

    async def _fast_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("scripts.sync_competitions.asyncio.sleep", _fast_sleep)
    monkeypatch.setattr(
        "scripts.sync_competitions._fetch_competition",
        _make_fetch_stub(SAMPLE_COMPETITION_RESPONSE),
    )

    await _sync(days=7, limit=100, session_factory=session_factory)

    async with session_factory() as session:
        rows = (await session.execute(select(ShadowCompetitor))).scalars().all()

    winner_rows = [r for r in rows if bool(r.is_winner)]
    assert len(winner_rows) == 1
    winner = winner_rows[0]
    assert winner.solver_name == "helixbox-solve"
    # SQLite stores large Numeric as float, so we can only check order of magnitude.
    # The exact value is 1_000_000_000_000_000_782 — close enough for the in-memory test.
    assert int(winner.score) > 1_000_000_000_000_000_000

    null_score_rows = [r for r in rows if r.score is None]
    assert len(null_score_rows) == 1
    assert null_score_rows[0].solver_name == "arc-solve"


async def test_no_auctions_returns_empty_result(session_factory) -> None:
    """When shadow_auctions is empty the script returns with no work done."""
    result = await _sync(days=7, limit=100, session_factory=session_factory)
    assert result.n_fetched == 0
    assert result.n_inserted == 0
