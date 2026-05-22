"""Tests for surplus analyzer (JSONL legacy + Postgres-backed)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.persistence.models import (
    ShadowAuction,
    ShadowSolution,
    ShadowWinner,
)
from src.shadow.analyzer import AnalysisWindow, _compute_solution_surplus, analyze
from src.shadow.jsonl_analyzer import analyze as jsonl_analyze

# ---------------------------------------------------------------------------
# JSONL legacy tests
# ---------------------------------------------------------------------------


def test_analyzer_reports_basic_stats(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    lines = [
        {
            "auction_id": "1",
            "our_solution": {"id": 1, "trades": [{"kind": "fulfillment"}]},
            "winner_solution": {"solver": "barter"},
            "timestamp": "2026-05-22T12:00:00+00:00",
        },
        {
            "auction_id": "2",
            "our_solution": None,
            "winner_solution": {"solver": "rizzolver"},
            "timestamp": "2026-05-22T12:01:00+00:00",
        },
        {
            "auction_id": "3",
            "our_solution": {"id": 3, "trades": []},
            "winner_solution": None,
            "timestamp": "2026-05-22T12:02:00+00:00",
        },
    ]
    log_path.write_text("\n".join(json.dumps(line) for line in lines))

    stats = jsonl_analyze(log_path)
    assert stats["total_auctions"] == 3
    assert stats["our_attempts"] == 2  # 1 + 3 had our_solution non-None
    assert stats["winners_seen"] == 2  # 1 + 2 had winner
    assert stats["winner_counts"] == {"barter": 1, "rizzolver": 1}


# ---------------------------------------------------------------------------
# Postgres-backed analyzer tests (in-memory SQLite)
# ---------------------------------------------------------------------------


@pytest.fixture  # type: ignore[misc]
async def session_factory(monkeypatch: pytest.MonkeyPatch) -> async_sessionmaker:  # type: ignore[type-arg]
    """In-memory sqlite engine with raw DDL (INTEGER not BIGINT) for autoincrement compat."""
    from sqlalchemy import text

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
    factory: async_sessionmaker = async_sessionmaker(engine, expire_on_commit=False)  # type: ignore[type-arg]
    monkeypatch.setattr("src.shadow.analyzer.get_session_factory", lambda: factory)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_analyze_empty_window(session_factory: async_sessionmaker) -> None:  # type: ignore[type-arg]
    window = AnalysisWindow.days(7)
    summary = await analyze(window)
    assert summary.n_auctions == 0
    assert summary.n_solved_any == 0
    assert summary.per_strategy == {}
    assert summary.win_rate_hypothetical == 0.0
    assert summary.surplus_delta_mean is None


@pytest.mark.asyncio  # type: ignore[misc]
async def test_analyze_counts_per_strategy(session_factory: async_sessionmaker) -> None:  # type: ignore[type-arg]
    now = datetime.now(UTC)
    async with session_factory() as s:
        s.add(ShadowAuction(
            auction_id=1, polled_at=now, n_orders=2,
            raw_competition={}, raw_auction={"orders": []},
        ))
        s.add(ShadowSolution(auction_id=1, strategy="naive", status="solved",
                             latency_ms=100, solution={"trades": [], "prices": {}}, error=None))
        s.add(ShadowSolution(auction_id=1, strategy="naive", status="no_solution",
                             latency_ms=50, solution=None, error=None))
        s.add(ShadowSolution(auction_id=1, strategy="router-v2", status="solved",
                             latency_ms=200, solution={"trades": [], "prices": {}}, error=None))
        await s.commit()

    window = AnalysisWindow(since=now - timedelta(hours=1), until=now + timedelta(hours=1))
    summary = await analyze(window)
    assert summary.n_auctions == 1
    assert summary.n_solved_any == 1
    assert "naive" in summary.per_strategy
    assert "router-v2" in summary.per_strategy
    assert summary.per_strategy["naive"]["solved"] == 1
    assert summary.per_strategy["naive"]["no_solution"] == 1
    assert summary.per_strategy["router-v2"]["solved"] == 1


def test_compute_surplus_basic() -> None:
    auction = {"orders": [
        {"uid": "o1", "sellToken": "0xa", "buyToken": "0xb",
         "sellAmount": 1000, "buyAmount": 900},
    ]}
    sol = {
        "prices": {"0xa": 100, "0xb": 100},
        "trades": [{"orderUid": "o1", "executedAmount": 1000}],
    }
    # executed_buy = 1000 * 100 / 100 = 1000, required = 900, surplus = 100
    surplus = _compute_solution_surplus(sol, auction)
    assert surplus == 100


def test_compute_surplus_handles_missing_data() -> None:
    assert _compute_solution_surplus(None, None) is None
    assert _compute_solution_surplus({}, {"orders": []}) == 0
    assert _compute_solution_surplus(
        {"trades": [{"orderUid": "nope", "executedAmount": 100}]},
        {"orders": []},
    ) == 0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_analyze_computes_win_rate(session_factory: async_sessionmaker) -> None:  # type: ignore[type-arg]
    now = datetime.now(UTC)
    async with session_factory() as s:
        # Two auctions
        s.add(ShadowAuction(
            auction_id=10, polled_at=now, n_orders=1,
            raw_competition={},
            raw_auction={"orders": [{"uid": "o1", "sellToken": "0xa", "buyToken": "0xb",
                                     "sellAmount": 1000, "buyAmount": 900}]},
        ))
        s.add(ShadowAuction(
            auction_id=20, polled_at=now, n_orders=1,
            raw_competition={},
            raw_auction={"orders": [{"uid": "o2", "sellToken": "0xa", "buyToken": "0xb",
                                     "sellAmount": 1000, "buyAmount": 900}]},
        ))
        # Auction 10: ours surplus +200, winner +100 -> delta=+100 (we win)
        s.add(ShadowSolution(
            auction_id=10, strategy="router-v2", status="solved", latency_ms=200,
            solution={"prices": {"0xa": 110, "0xb": 100},
                      "trades": [{"orderUid": "o1", "executedAmount": 1000}]},
            error=None,
        ))
        s.add(ShadowWinner(
            auction_id=10, winner_solver="0xZ", score=None,
            raw_solution={"prices": {"0xa": 100, "0xb": 100},
                          "trades": [{"orderUid": "o1", "executedAmount": 1000}]},
        ))
        # Auction 20: ours equals winner -> delta=0
        s.add(ShadowSolution(
            auction_id=20, strategy="router-v2", status="solved", latency_ms=200,
            solution={"prices": {"0xa": 100, "0xb": 100},
                      "trades": [{"orderUid": "o2", "executedAmount": 1000}]},
            error=None,
        ))
        s.add(ShadowWinner(
            auction_id=20, winner_solver="0xZ", score=None,
            raw_solution={"prices": {"0xa": 100, "0xb": 100},
                          "trades": [{"orderUid": "o2", "executedAmount": 1000}]},
        ))
        await s.commit()

    window = AnalysisWindow(since=now - timedelta(hours=1), until=now + timedelta(hours=1))
    summary = await analyze(window)
    assert summary.n_with_winner == 2
    # 1 of 2 comparable has positive delta
    assert summary.n_positive_delta == 1
    assert summary.win_rate_hypothetical == 0.5
