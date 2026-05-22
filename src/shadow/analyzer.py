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


def _compute_solution_surplus(
    solution: dict[str, Any] | None, auction: dict[str, Any] | None
) -> int | None:
    """Compute aggregate surplus across a solution's trades.

    surplus = sum over trades of (executed_amount * sell_price / buy_price - required_buy_amount).

    Returns None if solution or auction is missing or malformed.
    """
    if solution is None or auction is None:
        return None
    trades = solution.get("trades") or []
    prices = solution.get("prices") or {}
    orders = {o.get("uid"): o for o in auction.get("orders", [])}
    total = 0
    for trade in trades:
        uid = trade.get("orderUid") or trade.get("order_uid")
        order = orders.get(uid)
        if order is None:
            continue
        executed = int(trade.get("executedAmount") or trade.get("executed_amount") or 0)
        sell_token = (order.get("sellToken") or order.get("sell_token") or "").lower()
        buy_token = (order.get("buyToken") or order.get("buy_token") or "").lower()
        sell_price_raw = prices.get(sell_token) or prices.get(sell_token.lower())
        buy_price_raw = prices.get(buy_token) or prices.get(buy_token.lower())
        # Prices in Solution can be strings or ints
        try:
            sell_price = int(sell_price_raw) if sell_price_raw is not None else None
            buy_price = int(buy_price_raw) if buy_price_raw is not None else None
        except (ValueError, TypeError):
            continue
        if sell_price is None or buy_price is None or buy_price == 0:
            continue
        required_buy = int(order.get("buyAmount") or order.get("buy_amount") or 0)
        # Approximate: executed_buy = executed * sell_price / buy_price
        executed_buy = (executed * sell_price) // buy_price
        total += executed_buy - required_buy
    return total


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

        # Surplus delta — needs per-auction join
        delta_q = await session.execute(
            select(
                ShadowAuction.raw_auction,
                ShadowSolution.solution,
                ShadowWinner.raw_solution,
            )
            .select_from(ShadowAuction)
            .join(ShadowSolution, ShadowSolution.auction_id == ShadowAuction.auction_id)
            .join(ShadowWinner, ShadowWinner.auction_id == ShadowAuction.auction_id)
            .where(ShadowAuction.polled_at >= window.since)
            .where(ShadowAuction.polled_at <= window.until)
            .where(ShadowSolution.status == "solved")
        )
        deltas: list[float] = []
        n_positive = 0
        for raw_auction, our_solution, winner_solution in delta_q.all():
            ours = _compute_solution_surplus(our_solution, raw_auction)
            theirs = _compute_solution_surplus(winner_solution, raw_auction)
            if ours is None or theirs is None:
                continue
            d = float(ours - theirs)
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
        lines.append(f"Hypothetical wins (delta>0):  {summary.n_positive_delta}")
        lines.append(f"Win-rate (hypothetical):      {summary.win_rate_hypothetical:.1%}")
        lines.append(f"Mean surplus delta:           {summary.surplus_delta_mean:+.2e}")
        lines.append(f"Median surplus delta:         {summary.surplus_delta_median:+.2e}")
    else:
        lines.append("Surplus delta: no comparable batches yet.")
    return "\n".join(lines)
