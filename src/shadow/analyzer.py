"""Postgres-backed surplus analyzer for shadow auction outcomes.

Provides aggregate stats over a time window:
- Auctions analyzed, solved per strategy
- Surplus delta histogram (we vs winner)
- Hypothetical win-rate (count where our surplus > winner's)
- Per-strategy latency p50/p95
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from src.log import get_logger
from src.persistence.db import get_session_factory
from src.persistence.models import ShadowAuction, ShadowSolution, ShadowWinner

log = get_logger(__name__)


@dataclass
class AnalysisWindow:
    since: datetime
    until: datetime

    @classmethod
    def days(cls, days: int) -> AnalysisWindow:
        now = datetime.now(UTC)
        return cls(since=now - timedelta(days=days), until=now)


@dataclass
class Summary:
    window: AnalysisWindow
    n_auctions: int
    n_with_winner: int
    n_solved_any: int
    # strategy -> {solved, no_solution, error, timeout, p50_ms, p95_ms}
    per_strategy: dict[str, dict[str, Any]]
    win_rate_hypothetical: float  # 0..1
    surplus_delta_mean: float | None
    surplus_delta_median: float | None
    n_positive_delta: int



async def analyze(window: AnalysisWindow) -> Summary:
    Session = get_session_factory()
    async with Session() as session:
        # Count auctions in window
        n_auctions_q = await session.execute(
            select(func.count(ShadowAuction.auction_id))
            .where(ShadowAuction.polled_at >= window.since)
            .where(ShadowAuction.polled_at <= window.until)
        )
        n_auctions = n_auctions_q.scalar() or 0

        n_with_winner_q = await session.execute(
            select(func.count(ShadowWinner.auction_id))
            .join(ShadowAuction, ShadowAuction.auction_id == ShadowWinner.auction_id)
            .where(ShadowAuction.polled_at >= window.since)
            .where(ShadowAuction.polled_at <= window.until)
        )
        n_with_winner = n_with_winner_q.scalar() or 0

        # Per-strategy stats
        per_strategy: dict[str, dict[str, Any]] = {}
        strategies_q = await session.execute(
            select(ShadowSolution.strategy)
            .join(ShadowAuction, ShadowAuction.auction_id == ShadowSolution.auction_id)
            .where(ShadowAuction.polled_at >= window.since)
            .where(ShadowAuction.polled_at <= window.until)
            .distinct()
        )
        strategies = [s for s in strategies_q.scalars().all()]

        for strat in strategies:
            counts: dict[str, Any] = {}
            for status in ("solved", "no_solution", "error", "timeout"):
                cnt_q = await session.execute(
                    select(func.count(ShadowSolution.id))
                    .join(ShadowAuction, ShadowAuction.auction_id == ShadowSolution.auction_id)
                    .where(ShadowAuction.polled_at >= window.since)
                    .where(ShadowAuction.polled_at <= window.until)
                    .where(ShadowSolution.strategy == strat)
                    .where(ShadowSolution.status == status)
                )
                counts[status] = cnt_q.scalar() or 0
            # Latency percentiles
            lat_q = await session.execute(
                select(ShadowSolution.latency_ms)
                .join(ShadowAuction, ShadowAuction.auction_id == ShadowSolution.auction_id)
                .where(ShadowAuction.polled_at >= window.since)
                .where(ShadowAuction.polled_at <= window.until)
                .where(ShadowSolution.strategy == strat)
                .where(ShadowSolution.latency_ms.is_not(None))
            )
            latencies = sorted([row[0] for row in lat_q.all() if row[0] is not None])
            counts["p50_ms"] = _percentile(latencies, 50)
            counts["p95_ms"] = _percentile(latencies, 95)
            per_strategy[strat] = counts

        # Solved-any count
        n_solved_q = await session.execute(
            select(func.count(func.distinct(ShadowSolution.auction_id)))
            .join(ShadowAuction, ShadowAuction.auction_id == ShadowSolution.auction_id)
            .where(ShadowAuction.polled_at >= window.since)
            .where(ShadowAuction.polled_at <= window.until)
            .where(ShadowSolution.status == "solved")
        )
        n_solved_any = n_solved_q.scalar() or 0

        # Score comparison — use CIP-14 our_score_wei vs winner score.
        # Best solution per auction = highest our_score_wei among solved rows.
        score_q = await session.execute(
            select(
                ShadowSolution.auction_id,
                func.max(ShadowSolution.our_score_wei).label("best_our_score"),
                ShadowWinner.score.label("winner_score"),
            )
            .select_from(ShadowSolution)
            .join(ShadowAuction, ShadowAuction.auction_id == ShadowSolution.auction_id)
            .join(ShadowWinner, ShadowWinner.auction_id == ShadowSolution.auction_id)
            .where(ShadowAuction.polled_at >= window.since)
            .where(ShadowAuction.polled_at <= window.until)
            .where(ShadowSolution.status == "solved")
            .where(ShadowSolution.our_score_wei.is_not(None))
            .where(ShadowWinner.score.is_not(None))
            .group_by(ShadowSolution.auction_id, ShadowWinner.score)
        )
        deltas: list[float] = []
        n_positive = 0
        for _aid, best_ours, winner_score in score_q.all():
            try:
                ours_f = float(best_ours)
                theirs_f = float(winner_score)
            except (TypeError, ValueError):
                continue
            d = ours_f - theirs_f
            deltas.append(d)
            if d > 0:
                n_positive += 1

        return Summary(
            window=window,
            n_auctions=n_auctions,
            n_with_winner=n_with_winner,
            n_solved_any=n_solved_any,
            per_strategy=per_strategy,
            win_rate_hypothetical=(n_positive / len(deltas)) if deltas else 0.0,
            surplus_delta_mean=(statistics.mean(deltas)) if deltas else None,
            surplus_delta_median=(statistics.median(deltas)) if deltas else None,
            n_positive_delta=n_positive,
        )


def _percentile(sorted_values: list[int], pct: int) -> int | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (pct / 100) * (len(sorted_values) - 1)
    f = int(k)
    c = f + 1
    if c >= len(sorted_values):
        return sorted_values[-1]
    frac = k - f
    return int(sorted_values[f] * (1 - frac) + sorted_values[c] * frac)


def format_summary(summary: Summary) -> str:
    """Human-readable text report."""
    lines = []
    since = summary.window.since.isoformat()
    until = summary.window.until.isoformat()
    lines.append(f"Window: {since} -> {until}")
    lines.append(f"Auctions polled:        {summary.n_auctions}")
    lines.append(f"With winner data:       {summary.n_with_winner}")
    lines.append(f"Solved (any strategy):  {summary.n_solved_any}")
    lines.append("")
    lines.append("Per-strategy:")
    for strat, c in sorted(summary.per_strategy.items()):
        total = c["solved"] + c["no_solution"] + c["error"] + c["timeout"]
        if total == 0:
            continue
        lines.append(
            f"  {strat}: {c['solved']}/{total} solved"
            f" | err {c['error']} | timeout {c['timeout']}"
            f" | p50 {c['p50_ms'] or '-'}ms | p95 {c['p95_ms'] or '-'}ms"
        )
    lines.append("")
    if summary.surplus_delta_mean is not None:
        ETH = 1e18
        lines.append("Score comparison (CIP-14, vs winner):")
        lines.append(f"  Hypothetical wins (our > winner): {summary.n_positive_delta}")
        lines.append(f"  Win-rate (hypothetical):          {summary.win_rate_hypothetical:.1%}")
        lines.append(f"  Mean  delta:  {summary.surplus_delta_mean/ETH:+.6f} ETH")
        lines.append(f"  Median delta: {summary.surplus_delta_median/ETH:+.6f} ETH")  # type: ignore[operator]
    else:
        lines.append("Score comparison: no scored solutions yet (backfill_scores.py not run?).")
    return "\n".join(lines)
