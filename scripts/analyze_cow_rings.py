"""Analyze CoW ring performance in shadow data.

Answers:
  - How many 2-party (bipartite) and 3-4 party rings per auction?
  - What CIP-14 surplus do they generate?
  - How do CoW matching solutions compare to winners?

Usage:
    python -m scripts.analyze_cow_rings [--days 7]
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from scripts._analyze_common import print_winner_price_block
from src.persistence.db import get_session_factory
from src.persistence.models import ShadowAuction, ShadowSolution, ShadowWinner


def _count_trades(solution: dict[str, Any] | None) -> int:
    if not solution:
        return 0
    return sum(1 for t in (solution.get("trades") or []) if t.get("kind") == "fulfillment")


def _detect_rings(solution: dict[str, Any] | None, orders_by_uid: dict[str, Any]) -> list[int]:
    """Return list of ring sizes found in the solution.

    A ring is a cycle: A→B→...→A in sell/buy token graph.
    For a 2-party ring: A sells X→Y, B sells Y→X.
    This returns the trade-count of each ring (2, 3, 4...).
    """
    if not solution or not orders_by_uid:
        return []

    trades = [t for t in (solution.get("trades") or []) if t.get("kind") == "fulfillment"]
    if not trades:
        return []

    # Build directed graph: sell_token → buy_token per trade
    edges: list[tuple[str, str]] = []
    for t in trades:
        uid = (t.get("orderUid") or t.get("order_uid") or "").lower()
        order = orders_by_uid.get(uid)
        if not order:
            continue
        sell = (order.get("sellToken") or order.get("sell_token") or "").lower()
        buy = (order.get("buyToken") or order.get("buy_token") or "").lower()
        if sell and buy:
            edges.append((sell, buy))

    if not edges:
        return [len(trades)]  # fallback: count all trades as one ring

    # Simple cycle detection via DFS
    # Build adjacency: for each node, outgoing edges
    from collections import defaultdict
    graph: dict[str, list[str]] = defaultdict(list)
    for s, b in edges:
        graph[s].append(b)

    visited: set[str] = set()
    rings: list[int] = []

    def find_cycle(start: str, current: str, path: list[str], depth: int) -> None:
        if depth > 6:  # max 6-hop rings
            return
        for nxt in graph.get(current, []):
            if nxt == start and depth >= 2:
                rings.append(len(path))
                return
            if nxt not in visited:
                visited.add(nxt)
                find_cycle(start, nxt, path + [nxt], depth + 1)
                visited.discard(nxt)

    tokens = list({s for s, _ in edges} | {b for _, b in edges})
    for tok in tokens:
        visited = {tok}
        find_cycle(tok, tok, [tok], 1)

    return rings if rings else [len(trades)]


async def analyze_cow_rings(days: int = 7) -> None:
    since = datetime.now(UTC) - timedelta(days=days)
    Session = get_session_factory()

    async with Session() as session:
        # Fetch CoW matching solutions with score data
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
                ShadowAuction.raw_auction,
            )
            .join(ShadowAuction, ShadowAuction.auction_id == ShadowSolution.auction_id)
            .outerjoin(ShadowWinner, ShadowWinner.auction_id == ShadowSolution.auction_id)
            .where(ShadowAuction.polled_at >= since)
            .where(
                ShadowSolution.strategy.in_(
                    ["cow-matching-bipartite", "cow-matching-multi-party"]
                )
            )
            .where(ShadowSolution.status == "solved")
            .order_by(ShadowSolution.auction_id)
        )
        rows = q.all()

    print(f"\n{'='*60}")
    print(f"CoW Ring Analysis — last {days} day(s)")
    print(f"{'='*60}")
    print(f"Total CoW solutions found: {len(rows)}")

    if not rows:
        print("\nNo CoW matching solutions in this window.")
        print("Possible causes:")
        print("  - cow-matching-multi-party: bug or no 3+ party rings in orderbook")
        print("  - cow-matching-bipartite: no matching order pairs")
        return

    bipartite_rows = [r for r in rows if r.strategy == "cow-matching-bipartite"]
    multiparty_rows = [r for r in rows if r.strategy == "cow-matching-multi-party"]

    print(f"  bipartite (2-party): {len(bipartite_rows)}")
    print(f"  multi-party (3-4+):  {len(multiparty_rows)}")

    for label, group in [("BIPARTITE (2-party rings)", bipartite_rows),
                         ("MULTI-PARTY (3-4+ rings)", multiparty_rows)]:
        if not group:
            continue

        print(f"\n{'─'*50}")
        print(f"{label}")
        print(f"{'─'*50}")

        trade_counts = [_count_trades(r.solution) for r in group]
        print(f"Auctions solved:    {len(group)}")
        print(f"Trades per auction: min={min(trade_counts)}, "
              f"median={statistics.median(trade_counts):.0f}, "
              f"max={max(trade_counts)}")

        # Score analysis
        scored = [(r, int(r.our_score_wei), int(r.winner_score))
                  for r in group
                  if r.our_score_wei is not None and r.winner_score is not None]
        unscored = len(group) - len(scored)

        print(f"Scored rows:        {len(scored)} (unscored: {unscored})")

        if scored:
            ETH = 1e18
            our_scores = [s / ETH for _, s, _ in scored]
            win_scores = [w / ETH for _, _, w in scored]
            deltas = [o - w for o, w in zip(our_scores, win_scores, strict=True)]

            print("\nCIP-14 Score (ETH):")
            print(f"  Ours:   mean={statistics.mean(our_scores):+.6f}  "
                  f"median={statistics.median(our_scores):+.6f}  "
                  f"max={max(our_scores):+.6f}")
            print(f"  Winner: mean={statistics.mean(win_scores):+.6f}  "
                  f"median={statistics.median(win_scores):+.6f}  "
                  f"max={max(win_scores):+.6f}")
            print(f"  Delta:  mean={statistics.mean(deltas):+.6f}  "
                  f"median={statistics.median(deltas):+.6f}")

            wins = sum(1 for d in deltas if d > 0)
            print(f"\nHypothetical wins:  {wins}/{len(scored)} ({wins/len(scored):.0%})")

            print_winner_price_block(group)

            # Winner breakdown — who wins when we don't?
            losses = [(r, o, w) for r, o, w in scored if o < w]
            if losses:
                from collections import Counter
                winner_names = Counter(r.winner_solver for r, _, _ in losses)
                print(f"\nWho beats us (in {len(losses)} losses):")
                for solver, cnt in winner_names.most_common(5):
                    short = (solver or "unknown")[:40]
                    print(f"  {cnt:3d}x  {short}")

        # Sample: show a few actual solutions
        print("\nSample solutions (up to 3):")
        for r in group[:3]:
            trades = _count_trades(r.solution)
            our = f"{int(r.our_score_wei)/1e18:.6f} ETH" if r.our_score_wei else "unscored"
            win = f"{int(r.winner_score)/1e18:.6f} ETH" if r.winner_score else "no winner"
            print(f"  auction={r.auction_id}  trades={trades}  ours={our}  winner={win}")

    # Key insight: CoW solutions vs naive on the same auctions
    print(f"\n{'='*60}")
    print("KEY QUESTION: Do CoW rings beat the winner?")
    print(f"{'='*60}")
    all_scored = [(r, int(r.our_score_wei), int(r.winner_score))
                  for r in rows
                  if r.our_score_wei is not None and r.winner_score is not None]
    if all_scored:
        ETH = 1e18
        wins = sum(1 for r, o, w in all_scored if o > w)
        print(f"  {wins}/{len(all_scored)} auctions: CoW solution > winner score")
        if wins > 0:
            win_deltas = [(o - w) / ETH for r, o, w in all_scored if o > w]
            print(f"  When winning: mean surplus delta = {statistics.mean(win_deltas):+.6f} ETH")
    else:
        print("  No scored CoW solutions yet — run backfill_scores.py first.")
        print("  Then re-run this script.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze CoW ring performance")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    asyncio.run(analyze_cow_rings(days=args.days))


if __name__ == "__main__":
    main()
