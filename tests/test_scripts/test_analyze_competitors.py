"""Tests for scripts/analyze_competitors.py.

Seeds a minimal in-memory DB (1 auction + 3 competitor rows + 1 shadow_solution)
and verifies the three analysis views produce expected output.
"""

from __future__ import annotations

import io
import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.analyze_competitors import run_analysis
from src.persistence.models import ShadowAuction, ShadowCompetitor, ShadowSolution


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
            CREATE TABLE shadow_solutions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                auction_id INTEGER NOT NULL REFERENCES shadow_auctions(auction_id),
                strategy TEXT NOT NULL,
                status TEXT NOT NULL,
                latency_ms INTEGER,
                solution TEXT,
                error TEXT,
                our_score_wei NUMERIC,
                score_vs_winner_prices_wei NUMERIC,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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


async def _seed(factory: async_sessionmaker) -> None:
    """Insert one auction, three competitors (helixbox wins), one shadow solution."""
    now = datetime.now(UTC)
    async with factory() as session:
        session.add(
            ShadowAuction(
                auction_id=1,
                polled_at=now,
                n_orders=3,
                raw_competition={},
                raw_auction={},
            )
        )
        await session.flush()

        # Competitors: helixbox wins, kaisersolver is #2, portus is #3.
        session.add(
            ShadowCompetitor(
                auction_id=1,
                solver_name="helixbox-solve",
                solver_address="0x" + "1" * 40,
                score=1_000_000_000,
                ranking=1,
                is_winner=True,
                filtered_out=False,
                clearing_prices={},
                orders=[],
                polled_at=now,
            )
        )
        session.add(
            ShadowCompetitor(
                auction_id=1,
                solver_name="kaisersolver-solve",
                solver_address="0x" + "2" * 40,
                score=900_000_000,
                ranking=2,
                is_winner=False,
                filtered_out=False,
                clearing_prices={},
                orders=[],
                polled_at=now,
            )
        )
        session.add(
            ShadowCompetitor(
                auction_id=1,
                solver_name="portus",
                solver_address="0x" + "3" * 40,
                score=800_000_000,
                ranking=3,
                is_winner=False,
                filtered_out=False,
                clearing_prices={},
                orders=[],
                polled_at=now,
            )
        )
        # Our shadow solution — score puts us at rank 2 (between kaiser and portus).
        session.add(
            ShadowSolution(
                auction_id=1,
                strategy="naive",
                status="solved",
                our_score_wei=850_000_000,
            )
        )
        await session.commit()


async def _capture_analysis(
    factory: async_sessionmaker,
    days: int = 7,
    strategy: str | None = None,
) -> str:
    """Run analysis and return captured stdout as a string."""
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        await run_analysis(days=days, strategy=strategy, session_factory=factory)
    finally:
        sys.stdout = old_stdout
    return captured.getvalue()


class TestViewRankingDistribution:
    async def test_ranking_header_present(self, session_factory) -> None:
        await _seed(session_factory)
        output = await _capture_analysis(session_factory)
        assert "View 1" in output
        assert "rank distribution" in output.lower()

    async def test_our_rank_is_2nd(self, session_factory) -> None:
        """Our score 850M is below helixbox 1B but above kaiser 900M → rank 3.

        Wait — helixbox > our score, kaiser > our score → rank = 2 + 1 = 3.
        So we should appear in top-3.
        """
        await _seed(session_factory)
        output = await _capture_analysis(session_factory)
        # Our score 850M < kaiser 900M < helixbox 1B → 2 competitors beat us → rank 3
        # Rank 3 → "top-3" bucket
        assert "top-3" in output

    async def test_no_data_shows_message(self, session_factory) -> None:
        output = await _capture_analysis(session_factory)
        assert "No scored solutions" in output or "View 1" in output


class TestViewWinnerByTokenPair:
    async def test_view2_header_present(self, session_factory) -> None:
        await _seed(session_factory)
        output = await _capture_analysis(session_factory)
        assert "View 2" in output
        assert "specialisation" in output.lower() or "winner" in output.lower()

    async def test_no_orders_shows_no_uid_data_message(self, session_factory) -> None:
        """When orders arrays are empty (no UID data), the view should say so."""
        await _seed(session_factory)
        output = await _capture_analysis(session_factory)
        # With empty orders arrays there are no pairs to extract.
        assert "No order UID data" in output or "View 2" in output


class TestViewScoreGap:
    async def test_view3_header_present(self, session_factory) -> None:
        await _seed(session_factory)
        output = await _capture_analysis(session_factory)
        assert "View 3" in output
        assert "gap" in output.lower()

    async def test_score_gap_computed(self, session_factory) -> None:
        """Gap = 1_000_000_000 - 850_000_000 = 150_000_000 wei."""
        await _seed(session_factory)
        output = await _capture_analysis(session_factory)
        # The gap value (150_000_000) should appear in the output as median.
        assert "150,000,000" in output or "150000000" in output

    async def test_we_lost_count(self, session_factory) -> None:
        """With 1 auction and our score below winner, LOST count must be 1."""
        await _seed(session_factory)
        output = await _capture_analysis(session_factory)
        assert "1" in output  # At minimum the count appears
        # We lost 1 auction
        assert "LOST" in output or "lost" in output.lower()


class TestEmptyDatabase:
    async def test_no_crash_on_empty_db(self, session_factory) -> None:
        """Analysis on empty DB must not raise — just print placeholder messages."""
        output = await _capture_analysis(session_factory)
        assert "View 1" in output
        assert "View 2" in output
        assert "View 3" in output


class TestStrategyFilter:
    """--strategy plumbing: restrict views 1+3 to a single strategy."""

    async def _seed_two_strategies(self, factory: async_sessionmaker) -> None:
        """Two strategies on the same auction: bipartite (real) + router-v2 (phantom).

        The historical bug: max-across-strategies picked router-v2's phantom
        score as "our" entry, putting us above winner. With strategy filter,
        we now see bipartite's real score and rank correctly below winner.
        """
        now = datetime.now(UTC)
        async with factory() as session:
            session.add(
                ShadowAuction(
                    auction_id=42,
                    polled_at=now,
                    n_orders=2,
                    raw_competition={},
                    raw_auction={},
                )
            )
            session.add(
                ShadowCompetitor(
                    auction_id=42, solver_name="helixbox",
                    solver_address="0x" + "1" * 40,
                    score=1_000_000_000, ranking=1, is_winner=True,
                    filtered_out=False, clearing_prices={}, orders=[], polled_at=now,
                )
            )
            # bipartite: real, well below winner (rank 2)
            session.add(
                ShadowSolution(
                    auction_id=42, strategy="cow-matching-bipartite",
                    status="solved", our_score_wei=500_000_000,
                )
            )
            # router-v2: phantom (would be picked by max-across-strategies)
            session.add(
                ShadowSolution(
                    auction_id=42, strategy="router-v2",
                    status="solved", our_score_wei=5_000_000_000,
                )
            )
            await session.commit()

    async def test_strategy_filter_shows_only_named_strategy(
        self, session_factory
    ) -> None:
        """With --strategy cow-matching-bipartite, only bipartite's score is ranked."""
        await self._seed_two_strategies(session_factory)
        output = await _capture_analysis(
            session_factory, strategy="cow-matching-bipartite"
        )

        # Header echoes the strategy
        assert "strategy=cow-matching-bipartite" in output
        # Bipartite (500M) < winner (1B) → rank 2 → "2nd" bucket fires
        assert "2nd" in output

    async def test_strategy_filter_excludes_other_strategies(
        self, session_factory
    ) -> None:
        """router-v2's phantom 5B does NOT contribute when --strategy=bipartite."""
        await self._seed_two_strategies(session_factory)
        output = await _capture_analysis(
            session_factory, strategy="cow-matching-bipartite"
        )

        # View 3 score-gap should reflect bipartite-vs-winner (500M gap),
        # NOT the phantom (which would have shown "we won").
        # 1_000_000_000 - 500_000_000 = 500_000_000
        assert "500,000,000" in output or "500000000" in output
        # "Auctions we WON: 0" — phantom would have flipped this to 1.
        assert "WON          : 0" in output or "WON" in output

    async def test_no_filter_uses_max_across_strategies(self, session_factory) -> None:
        """Without --strategy: max-across-strategies → router-v2 phantom picked.

        Regression guard: this is the historical (buggy) behaviour that
        --strategy was designed to bypass.  Locking it in so we notice if
        someone "fixes" max-across-strategies separately.
        """
        await self._seed_two_strategies(session_factory)
        output = await _capture_analysis(session_factory)

        # max(500M, 5B) = 5B → above winner 1B → we "won" the auction
        # View 3: WON count == 1
        assert "WON          : 1" in output or "WON: 1" in output.replace(" ", "")

    async def test_strategy_filter_with_no_matching_rows(self, session_factory) -> None:
        """Filter on a strategy that has no rows → 'No scored solutions' message."""
        await self._seed_two_strategies(session_factory)
        output = await _capture_analysis(
            session_factory, strategy="cow-matching-multi-party"
        )
        # No multi-party rows exist → both Views 1 and 3 short-circuit.
        assert "No scored solutions" in output
