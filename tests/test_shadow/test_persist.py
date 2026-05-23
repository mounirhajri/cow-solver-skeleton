"""Tests for shadow persistence.

These tests use sqlite+aiosqlite as a transient in-memory backend to verify
the persist function constructs correct SQL and runs without error.
"""
from __future__ import annotations

from datetime import UTC, datetime

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
                our_score_wei NUMERIC,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        await conn.execute(text("""
            CREATE TABLE shadow_winners (
                auction_id INTEGER PRIMARY KEY REFERENCES shadow_auctions(auction_id),
                winner_solver TEXT NOT NULL,
                score NUMERIC,
                raw_solution TEXT NOT NULL
            )
        """))
        await conn.execute(text("""
            CREATE TABLE token_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_address TEXT NOT NULL,
                auction_id INTEGER NOT NULL REFERENCES shadow_auctions(auction_id),
                appeared_in_winner INTEGER NOT NULL,
                appeared_in_ours INTEGER NOT NULL,
                caused_revert INTEGER NOT NULL DEFAULT 0,
                observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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


# ─── Winner + token outcome tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_winner_inserts_row(session_factory) -> None:
    from src.persistence.models import ShadowAuction, ShadowWinner
    from src.shadow.persist import persist_winner_and_outcomes

    # Prerequisite: shadow_auction row exists (FK constraint)
    async with session_factory() as s:
        s.add(ShadowAuction(
            auction_id=999, polled_at=datetime.now(UTC),
            n_orders=0, raw_competition={}, raw_auction={},
        ))
        await s.commit()

    comp = {
        "auctionId": "999",
        "solutions": [
            {"solver": "0xabc", "score": "1000000000000000000", "isWinner": True,
             "ranking": 1, "prices": {"0xa": "1"}, "trades": []}
        ],
    }
    auction = {"orders": [{"sellToken": "0xA", "buyToken": "0xB"}]}
    ours = {"prices": {"0xA": "1"}}

    await persist_winner_and_outcomes(999, comp, auction, ours)

    async with session_factory() as s:
        winners = (await s.execute(select(ShadowWinner))).scalars().all()
        assert len(winners) == 1
        assert winners[0].winner_solver == "0xabc"
        assert int(winners[0].score) == 1000000000000000000


@pytest.mark.asyncio
async def test_persist_winner_handles_no_winner(session_factory) -> None:
    from src.persistence.models import ShadowAuction, ShadowWinner, TokenOutcome
    from src.shadow.persist import persist_winner_and_outcomes

    async with session_factory() as s:
        s.add(ShadowAuction(
            auction_id=888, polled_at=datetime.now(UTC),
            n_orders=0, raw_competition={}, raw_auction={},
        ))
        await s.commit()

    comp = {"auctionId": "888", "solutions": []}
    auction = {"orders": [{"sellToken": "0xA", "buyToken": "0xB"}]}

    await persist_winner_and_outcomes(888, comp, auction, None)

    async with session_factory() as s:
        winners = (await s.execute(select(ShadowWinner))).scalars().all()
        assert len(winners) == 0
        # But outcomes still derived
        outcomes = (await s.execute(select(TokenOutcome))).scalars().all()
        assert len(outcomes) == 2  # 0xa, 0xb


@pytest.mark.asyncio
async def test_persist_winner_handles_invalid_score(session_factory) -> None:
    from src.persistence.models import ShadowAuction, ShadowWinner
    from src.shadow.persist import persist_winner_and_outcomes

    async with session_factory() as s:
        s.add(ShadowAuction(
            auction_id=777, polled_at=datetime.now(UTC),
            n_orders=0, raw_competition={}, raw_auction={},
        ))
        await s.commit()

    comp = {
        "auctionId": "777",
        "solutions": [{"solver": "0xabc", "score": "not_a_number", "isWinner": True,
                       "prices": {}, "trades": []}],
    }
    await persist_winner_and_outcomes(777, comp, {"orders": []}, None)

    async with session_factory() as s:
        winners = (await s.execute(select(ShadowWinner))).scalars().all()
        assert len(winners) == 1
        assert winners[0].score is None  # gracefully parsed as None


@pytest.mark.asyncio
async def test_persist_winner_creates_auction_row_when_missing(session_factory) -> None:
    """Ensure FK target (shadow_auctions) is created when missing — handles race."""
    from src.persistence.models import ShadowAuction, ShadowWinner
    from src.shadow.persist import persist_winner_and_outcomes

    # Deliberately do NOT pre-insert the ShadowAuction row
    comp = {
        "auctionId": "666",
        "solutions": [{"solver": "0xdef", "score": "500", "isWinner": True,
                       "prices": {}, "trades": []}],
    }
    auction = {"orders": [{"sellToken": "0xA", "buyToken": "0xB"}]}

    await persist_winner_and_outcomes(666, comp, auction, None)

    async with session_factory() as s:
        auctions = (await s.execute(select(ShadowAuction))).scalars().all()
        assert len(auctions) == 1  # created as side-effect
        winners = (await s.execute(select(ShadowWinner))).scalars().all()
        assert len(winners) == 1
        assert winners[0].winner_solver == "0xdef"


@pytest.mark.asyncio
async def test_persist_winner_outcomes_safe_swallows_errors(monkeypatch) -> None:
    """persist_winner_and_outcomes_safe must never raise."""
    from src.shadow.persist import persist_winner_and_outcomes_safe

    async def broken(*args, **kwargs) -> None:
        raise RuntimeError("DB down")

    monkeypatch.setattr("src.shadow.persist.persist_winner_and_outcomes", broken)
    # Must NOT raise
    await persist_winner_and_outcomes_safe(42, {}, {}, None)


# ─── persist_skipped_auction tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_skipped_auction_inserts_rows(session_factory) -> None:
    """persist_skipped_auction must create auction row + skipped solution row."""
    from src.shadow.persist import persist_skipped_auction

    await persist_skipped_auction(
        auction_id=11111,
        auction_payload={"id": "11111", "tokens": {}, "orders": [], "liquidity": []},
        raw_competition={"auctionId": "11111", "solutions": []},
        n_orders=750,
    )

    async with session_factory() as s:
        auctions = (await s.execute(select(ShadowAuction))).scalars().all()
        assert len(auctions) == 1
        assert auctions[0].auction_id == 11111
        assert auctions[0].n_orders == 750

        solutions = (await s.execute(select(ShadowSolution))).scalars().all()
        assert len(solutions) == 1
        assert solutions[0].strategy == "poller-skipped"
        assert solutions[0].status == "skipped"
        assert "750" in solutions[0].error


@pytest.mark.asyncio
async def test_persist_skipped_auction_idempotent_on_auction_row(session_factory) -> None:
    """If the shadow_auction row already exists, persist_skipped_auction must not duplicate it."""
    from src.shadow.persist import persist_skipped_auction

    # Pre-insert the auction row (e.g., from persist_winner_and_outcomes running first)
    async with session_factory() as s:
        s.add(ShadowAuction(
            auction_id=22222,
            polled_at=datetime.now(UTC),
            n_orders=0,
            raw_competition={},
            raw_auction={},
        ))
        await s.commit()

    await persist_skipped_auction(
        auction_id=22222,
        auction_payload={},
        raw_competition={},
        n_orders=900,
    )

    async with session_factory() as s:
        auctions = (await s.execute(select(ShadowAuction))).scalars().all()
        assert len(auctions) == 1  # still only one — not duplicated

        solutions = (await s.execute(select(ShadowSolution))).scalars().all()
        assert len(solutions) == 1
        assert solutions[0].strategy == "poller-skipped"


@pytest.mark.asyncio
async def test_persist_skipped_auction_safe_swallows_errors(monkeypatch) -> None:
    """persist_skipped_auction_safe must never raise."""
    from src.shadow.persist import persist_skipped_auction_safe

    async def broken(*args, **kwargs) -> None:
        raise RuntimeError("DB down")

    monkeypatch.setattr("src.shadow.persist.persist_skipped_auction", broken)
    # Must NOT raise
    await persist_skipped_auction_safe(99, {}, {}, 500)
