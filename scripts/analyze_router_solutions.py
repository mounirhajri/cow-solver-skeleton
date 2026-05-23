"""Analyze router-v2 shadow performance.

Mirror of analyze_cow_rings.py, filtered to the router-v2 strategy. Used to
gauge whether the workhorse strategy is winning enough hypothetical auctions
to justify the next bonding-pool / KYC step.

Usage:
    python -m scripts.analyze_router_solutions [--days 7]
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from src.persistence.db import get_session_factory
from src.persistence.models import ShadowAuction, ShadowSolution, ShadowWinner
from src.solver.router import RouterSolver

_STRATEGY = RouterSolver.name


def _count_trades(solution: dict[str, Any] | None) -> int:
    if not solution:
        return 0
    return sum(1 for t in (solution.get("trades") or []) if t.get("kind") == "fulfillment")


async def analyze_router_solutions(days: int = 7) -> None:
    since = datetime.now(UTC) - timedelta(days=days)
    Session = get_session_factory()

    async with Session() as session:
        q = await session.execute(
            select(
                ShadowSolution.auction_id,
                ShadowSolution.strategy,
                ShadowSolution.status,
                ShadowSolution.solution,
                ShadowSolution.our_score_wei,
                ShadowSolution.score_vs_winner_prices_wei,
                ShadowWinner.score.label("winner_score"),
                ShadowWinner.winner_solver,
            )
            .join(ShadowAuction, ShadowAuction.auction_id == ShadowSolution.auction_id)
            .outerjoin(ShadowWinner, ShadowWinner.auction_id == ShadowSolution.auction_id)
            .where(ShadowAuction.polled_at >= since)
            .where(ShadowSolution.strategy == _STRATEGY)
            .where(ShadowSolution.status == "solved")
            .order_by(ShadowSolution.auction_id)
        )
        rows = q.all()

    print(f"\n{'=' * 60}")
    print(f"Router-v2 Shadow Analysis — last {days} day(s)")
    print(f"{'=' * 60}")
    print(f"Total router-v2 solutions: {len(rows)}")

    if not rows:
        print("\nNo router-v2 solutions in this window.")
        print("Possible causes:")
        print("  - shadow runner disabled or paused")
        print("  - strategy_timeout too aggressive (every order returns None)")
        print("  - RPC endpoint outage / rate-limit")
        return

    trade_counts = [_count_trades(r.solution) for r in rows]
    print(
        f"Trades per auction: min={min(trade_counts)}, "
        f"median={statistics.median(trade_counts):.0f}, "
        f"max={max(trade_counts)}"
    )

    scored = [
        (r, int(r.our_score_wei), int(r.winner_score))
        for r in rows
        if r.our_score_wei is not None and r.winner_score is not None
    ]
    unscored = len(rows) - len(scored)
    print(f"Scored rows: {len(scored)} (unscored: {unscored})")

    if not scored:
        print("\nNo scored router-v2 solutions yet - run backfill_scores.py first.")
        return

    ETH = 1e18
    our_scores = [s / ETH for _, s, _ in scored]
    win_scores = [w / ETH for _, _, w in scored]
    deltas = [o - w for o, w in zip(our_scores, win_scores, strict=True)]

    print("\nCIP-14 Score (ETH):")
    print(
        f"  Ours:   mean={statistics.mean(our_scores):+.6f}  "
        f"median={statistics.median(our_scores):+.6f}  "
        f"max={max(our_scores):+.6f}"
    )
    print(
        f"  Winner: mean={statistics.mean(win_scores):+.6f}  "
        f"median={statistics.median(win_scores):+.6f}  "
        f"max={max(win_scores):+.6f}"
    )
    print(
        f"  Delta:  mean={statistics.mean(deltas):+.6f}  "
        f"median={statistics.median(deltas):+.6f}"
    )

    wins = sum(1 for d in deltas if d > 0)
    print(f"\nHypothetical wins: {wins}/{len(scored)} ({wins / len(scored):.0%})")

    # Phase 4a — winner-price comparison block
    wp_scored = [
        (int(r.score_vs_winner_prices_wei), int(r.winner_score))
        for r in rows
        if r.score_vs_winner_prices_wei is not None and r.winner_score is not None
    ]
    if not wp_scored:
        print(
            "\nscore_vs_winner_prices_wei: not yet populated "
            "— run backfill_winner_price_scores.py"
        )
    else:
        wp_ours = [s / ETH for s, _ in wp_scored]
        wp_deltas = [(s - w) / ETH for s, w in wp_scored]
        wp_wins = sum(1 for d in wp_deltas if d > 0)
        print("\nCIP-14 Score @ winner prices (ETH):")
        print(
            f"  Ours: mean={statistics.mean(wp_ours):+.6f}  "
            f"median={statistics.median(wp_ours):+.6f}  "
            f"max={max(wp_ours):+.6f}"
        )
        print(
            f"  Delta vs winner: mean={statistics.mean(wp_deltas):+.6f}  "
            f"median={statistics.median(wp_deltas):+.6f}"
        )
        print(
            f"Wins @ winner prices: {wp_wins}/{len(wp_scored)} "
            f"({wp_wins / len(wp_scored):.0%})"
        )

    losses = [(r, o, w) for r, o, w in scored if o < w]
    if losses:
        winner_names: Counter[str] = Counter(r.winner_solver for r, _, _ in losses)
        print(f"\nWho beats us (in {len(losses)} losses):")
        for solver, cnt in winner_names.most_common(5):
            short = (solver or "unknown")[:40]
            print(f"  {cnt:3d}x  {short}")

    print("\nSample solutions (up to 3):")
    for r in rows[:3]:
        trades = _count_trades(r.solution)
        our = f"{int(r.our_score_wei) / 1e18:.6f} ETH" if r.our_score_wei else "unscored"
        win = f"{int(r.winner_score) / 1e18:.6f} ETH" if r.winner_score else "no winner"
        print(f"  auction={r.auction_id}  trades={trades}  ours={our}  winner={win}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze router-v2 shadow performance")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    asyncio.run(analyze_router_solutions(days=args.days))


if __name__ == "__main__":
    main()
