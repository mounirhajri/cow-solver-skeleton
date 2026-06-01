"""Tests for scripts/analyze_feasibility.py.

Seeds an in-memory DB with shadow_solutions carrying mixed feasibility verdicts
(True/False/NULL) across strategies and verifies the two views tally and group
them correctly.
"""

from __future__ import annotations

import io
import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.analyze_feasibility import run_analysis
from src.persistence.models import ShadowAuction, ShadowSolution


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
                feasible INTEGER,
                revert_reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _sol(auction_id: int, strategy: str, feasible, revert_reason=None, **kw):
    """Build an eligible (solved + scored) ShadowSolution with a verdict."""
    return ShadowSolution(
        auction_id=auction_id,
        strategy=strategy,
        status="solved",
        solution={"prices": {}, "trades": []},
        our_score_wei=kw.get("score", 100),
        feasible=feasible,
        revert_reason=revert_reason,
        created_at=datetime.now(UTC),
    )


async def _seed(factory: async_sessionmaker) -> None:
    now = datetime.now(UTC)
    async with factory() as session:
        for aid in range(1, 7):
            session.add(ShadowAuction(
                auction_id=aid, polled_at=now, n_orders=1,
                raw_competition={}, raw_auction={},
            ))
        await session.flush()

        # bipartite: 2 feasible, 1 phantom, 1 unknown
        session.add(_sol(1, "cow-matching-bipartite", True))
        session.add(_sol(2, "cow-matching-bipartite", True))
        session.add(_sol(3, "cow-matching-bipartite", False,
                         "execution reverted: GPv2: invalid signature"))
        session.add(_sol(4, "cow-matching-bipartite", None))
        # router-v2: 1 phantom (token conservation), 1 unknown
        session.add(_sol(5, "router-v2", False,
                         "execution reverted: TransferHelper: transfer failed"))
        session.add(_sol(6, "router-v2", None))
        # naive must be ignored entirely (not eligible)
        session.add(_sol(1, "naive", True))
        await session.commit()


async def _capture(factory, days: int = 7, strategy: str | None = None) -> str:
    captured = io.StringIO()
    old = sys.stdout
    sys.stdout = captured
    try:
        await run_analysis(days=days, strategy=strategy, session_factory=factory)
    finally:
        sys.stdout = old
    return captured.getvalue()


class TestViewFeasibilityByStrategy:
    async def test_header_present(self, session_factory) -> None:
        await _seed(session_factory)
        out = await _capture(session_factory)
        assert "View A" in out
        assert "Feasibility verdict per strategy" in out

    async def test_strategies_listed_naive_excluded(self, session_factory) -> None:
        await _seed(session_factory)
        out = await _capture(session_factory)
        assert "cow-matching-bipartite" in out
        assert "router-v2" in out
        # naive rows are not eligible → no data row tallies them.
        # (The header text says "non-naive", so check for a row, not a substring.)
        assert not any(
            line.strip().startswith("naive") for line in out.splitlines()
        )

    async def test_counts_and_totals(self, session_factory) -> None:
        await _seed(session_factory)
        out = await _capture(session_factory)
        # Grand totals: feasible=2, phantom=2, unknown=2, eligible=6
        # The ALL row carries those numbers.
        all_line = next(line for line in out.splitlines() if line.strip().startswith("ALL"))
        assert "6" in all_line          # eligible
        # check% = decided(4)/eligible(6) = 66.7; feas% = feasible(2)/decided(4) = 50.0
        assert "66.7%" in all_line
        assert "50.0%" in all_line

    async def test_empty_window_message(self, session_factory) -> None:
        out = await _capture(session_factory)
        assert "No eligible solutions" in out


class TestViewRevertReasons:
    async def test_header_present(self, session_factory) -> None:
        await _seed(session_factory)
        out = await _capture(session_factory)
        assert "View B" in out

    async def test_reasons_grouped_and_counted(self, session_factory) -> None:
        await _seed(session_factory)
        out = await _capture(session_factory)
        # Two distinct phantom reasons, one each.
        assert "invalid signature" in out
        assert "transfer failed" in out

    async def test_no_phantom_message(self, session_factory) -> None:
        out = await _capture(session_factory)
        assert "No phantom" in out


class TestStrategyFilter:
    async def test_filter_restricts_to_one_strategy(self, session_factory) -> None:
        await _seed(session_factory)
        out = await _capture(session_factory, strategy="router-v2")
        assert "router-v2" in out
        # bipartite-only reason must not appear under the filter
        assert "invalid signature" not in out
        assert "transfer failed" in out
