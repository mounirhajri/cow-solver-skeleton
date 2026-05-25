"""Competitive analysis of CoW competition data vs our shadow solutions.

Three views, printed to stdout:

View 1 — Per-strategy ranking distribution
    For each shadow_solution with a non-NULL our_score_wei, count where our
    score would have ranked among all competitors in the same auction.
    Output: histogram (1st, 2nd, top-3, top-5, lower).

View 2 — Winner specialisation per token-pair
    For each token-pair seen in competitors' ``orders`` JSON, count how often
    each solver was the winner.  Top 20 pairs by auction count, top 8 solvers.

View 3 — Score gap to winner
    For completed auctions in the last 7 days, compute the difference between
    the winner's score and our best score_wei per auction.  Reports median,
    p25 and p75 for auctions where we had a score but did NOT beat the winner.

Usage
-----
    python -m scripts.analyze_competitors [--days 7]
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.log import get_logger
from src.persistence.db import get_session_factory
from src.persistence.models import ShadowAuction, ShadowCompetitor, ShadowSolution

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# View 1: ranking distribution
# ---------------------------------------------------------------------------


async def _view_ranking_distribution(
    session: AsyncSession,
    since: datetime,
) -> None:
    """Print a histogram of where our score would rank among all competitors."""
    # Fetch all (auction_id, our_score_wei) for solved solutions in the window.
    our_q = await session.execute(
        select(ShadowSolution.auction_id, ShadowSolution.our_score_wei)
        .join(ShadowAuction, ShadowAuction.auction_id == ShadowSolution.auction_id)
        .where(ShadowSolution.our_score_wei.is_not(None))
        .where(ShadowAuction.polled_at >= since)
    )
    our_rows = our_q.all()

    if not our_rows:
        print("[View 1] No scored solutions found in window — nothing to rank.")
        return

    # Group by auction_id, keep best score per auction.
    best_ours: dict[int, int] = {}
    for auction_id, score_wei in our_rows:
        score = int(score_wei)
        if auction_id not in best_ours or score > best_ours[auction_id]:
            best_ours[auction_id] = score

    # Fetch competitor scores for those auctions.
    auction_ids = list(best_ours.keys())
    comp_q = await session.execute(
        select(ShadowCompetitor.auction_id, ShadowCompetitor.score)
        .where(
            and_(
                ShadowCompetitor.auction_id.in_(auction_ids),
                ShadowCompetitor.score.is_not(None),
                ShadowCompetitor.filtered_out.is_(False),
            )
        )
    )
    comp_rows = comp_q.all()

    # Build per-auction score lists.
    comp_scores: dict[int, list[int]] = defaultdict(list)
    for auction_id, score_wei in comp_rows:
        comp_scores[auction_id].append(int(score_wei))

    # Compute rank for each auction.
    rank_counts: dict[str, int] = defaultdict(int)
    no_competitor_data = 0
    for auction_id, our_score in best_ours.items():
        competitors = comp_scores.get(auction_id)
        if not competitors:
            no_competitor_data += 1
            continue
        # Rank = number of competitors with strictly higher score + 1.
        rank = sum(1 for s in competitors if s > our_score) + 1
        if rank == 1:
            rank_counts["1st"] += 1
        elif rank == 2:
            rank_counts["2nd"] += 1
        elif rank <= 3:
            rank_counts["top-3"] += 1
        elif rank <= 5:
            rank_counts["top-5"] += 1
        else:
            rank_counts["lower"] += 1

    total = sum(rank_counts.values())
    print(f"\n{'='*60}")
    print("View 1 — Our score rank distribution (best score per auction)")
    print(f"{'='*60}")
    print(f"  Auctions scored  : {len(best_ours)}")
    print(f"  No competitor data: {no_competitor_data}")
    print(f"  Auctions ranked  : {total}")
    for label in ("1st", "2nd", "top-3", "top-5", "lower"):
        n = rank_counts.get(label, 0)
        pct = 100 * n / total if total else 0
        bar = "#" * int(pct / 2)
        print(f"  {label:<8} {n:>5}  ({pct:5.1f}%)  {bar}")


# ---------------------------------------------------------------------------
# View 2: winner specialisation per token-pair
# ---------------------------------------------------------------------------


def _extract_pairs(orders: Any) -> list[tuple[str, str]]:
    """Extract (sell_token, buy_token) pairs from a competitor's orders array.

    The competition API's ``orders`` field is a list of dicts with ``id``
    (order UID) and ``sellAmount``/``buyAmount``.  Order UIDs encode the
    token pair as bytes 0-19 (sell) and 20-39 (buy) in hex after the 0x prefix.
    UID format: 0x<32-byte-order-hash><20-byte-sell-token><20-byte-buy-token>
    That's 0x + 64 + 40 + 40 = 0x + 144 hex chars = 146 total.
    """
    if not isinstance(orders, list):
        return []
    pairs: list[tuple[str, str]] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        uid = order.get("id") or ""
        # UID: 0x + 32-byte hash (64 chars) + 20-byte sell-token + 20-byte buy-token
        # Total: 2 + 64 + 40 + 40 = 146 chars
        if isinstance(uid, str) and len(uid) == 146 and uid.startswith("0x"):
            sell = "0x" + uid[66:106].lower()
            buy = "0x" + uid[106:146].lower()
            pairs.append((sell, buy))
    return pairs


async def _view_winner_by_token_pair(
    session: AsyncSession,
    since: datetime,
    top_n_solvers: int = 8,
    top_n_pairs: int = 20,
) -> None:
    """Print winner counts per token-pair, top solvers as columns."""
    comp_q = await session.execute(
        select(
            ShadowCompetitor.auction_id,
            ShadowCompetitor.solver_name,
            ShadowCompetitor.orders,
            ShadowCompetitor.is_winner,
        )
        .join(ShadowAuction, ShadowAuction.auction_id == ShadowCompetitor.auction_id)
        .where(ShadowAuction.polled_at >= since)
        .where(ShadowCompetitor.filtered_out.is_(False))
    )
    comp_rows = comp_q.all()

    if not comp_rows:
        print("[View 2] No competitor data found — run sync_competitions first.")
        return

    # pair → solver → win_count
    pair_solver_wins: dict[tuple[str, str], defaultdict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    pair_auction_count: dict[tuple[str, str], int] = defaultdict(int)
    seen_pair_auctions: dict[tuple[str, str], set[int]] = defaultdict(set)

    for auction_id, solver_name, orders, is_winner in comp_rows:
        pairs = _extract_pairs(orders)
        for pair in pairs:
            if auction_id not in seen_pair_auctions[pair]:
                seen_pair_auctions[pair].add(auction_id)
                pair_auction_count[pair] += 1
            if is_winner:
                pair_solver_wins[pair][solver_name] += 1

    # Top solvers overall by total wins.
    solver_total_wins: dict[str, int] = defaultdict(int)
    for solver_wins in pair_solver_wins.values():
        for solver, wins in solver_wins.items():
            solver_total_wins[solver] += wins
    top_solvers = sorted(solver_total_wins, key=lambda s: -solver_total_wins[s])[:top_n_solvers]

    # Top pairs by auction count.
    top_pairs = sorted(pair_auction_count, key=lambda p: -pair_auction_count[p])[:top_n_pairs]

    print(f"\n{'='*60}")
    print("View 2 — Winner specialisation by token-pair")
    print(f"{'='*60}")
    if not top_pairs:
        print("  No order UID data found (orders array empty or missing).")
        return

    # Header row.
    col_w = 12
    header = f"  {'sell_token':>20} / {'buy_token':<20}  {'total':>6}"
    for s in top_solvers:
        short = s[:col_w]
        header += f"  {short:>{col_w}}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for pair in top_pairs:
        sell, buy = pair
        total = pair_auction_count[pair]
        wins_map: dict[str, int] = pair_solver_wins.get(pair, {})
        row = f"  {sell[-8:]:>20} / {buy[-8:]:<20}  {total:>6}"
        for s in top_solvers:
            n = wins_map.get(s, 0)
            row += f"  {n:>{col_w}}"
        print(row)

    print(f"\n  Top solvers: {', '.join(top_solvers)}")


# ---------------------------------------------------------------------------
# View 3: score gap to winner
# ---------------------------------------------------------------------------


async def _view_score_gap(
    session: AsyncSession,
    since: datetime,
) -> None:
    """Print median / p25 / p75 score gap (winner - ours) for losing auctions."""
    # For each auction, best our_score and winner score.
    our_q = await session.execute(
        select(ShadowSolution.auction_id, ShadowSolution.our_score_wei)
        .join(ShadowAuction, ShadowAuction.auction_id == ShadowSolution.auction_id)
        .where(ShadowSolution.our_score_wei.is_not(None))
        .where(ShadowAuction.polled_at >= since)
    )
    our_rows = our_q.all()

    if not our_rows:
        print("[View 3] No scored solutions found in window.")
        return

    best_ours: dict[int, int] = {}
    for auction_id, score_wei in our_rows:
        score = int(score_wei)
        if auction_id not in best_ours or score > best_ours[auction_id]:
            best_ours[auction_id] = score

    # Fetch winner scores.
    winner_q = await session.execute(
        select(ShadowCompetitor.auction_id, ShadowCompetitor.score)
        .where(
            and_(
                ShadowCompetitor.auction_id.in_(list(best_ours.keys())),
                ShadowCompetitor.is_winner.is_(True),
                ShadowCompetitor.score.is_not(None),
            )
        )
    )
    winner_rows = winner_q.all()
    winner_score: dict[int, int] = {r[0]: int(r[1]) for r in winner_rows}

    diffs: list[float] = []
    diffs_pct: list[float] = []
    we_won = 0
    no_winner_data = 0

    for auction_id, our_score in best_ours.items():
        w_score = winner_score.get(auction_id)
        if w_score is None:
            no_winner_data += 1
            continue
        if our_score >= w_score:
            we_won += 1
            continue
        diff = float(w_score - our_score)
        diffs.append(diff)
        if w_score > 0:
            diffs_pct.append(diff / w_score * 100)

    print(f"\n{'='*60}")
    print("View 3 — Score gap to winner (auctions where we LOST)")
    print(f"{'='*60}")
    print(f"  Auctions with our score  : {len(best_ours)}")
    print(f"  No competitor winner data: {no_winner_data}")
    print(f"  Auctions we WON          : {we_won}")
    print(f"  Auctions we LOST         : {len(diffs)}")

    if not diffs:
        print("  No gap data available.")
        return

    diffs_sorted = sorted(diffs)
    n = len(diffs_sorted)
    median = statistics.median(diffs_sorted)
    p25 = diffs_sorted[n // 4]
    p75 = diffs_sorted[3 * n // 4]

    diffs_pct_sorted = sorted(diffs_pct)
    median_pct = statistics.median(diffs_pct_sorted) if diffs_pct_sorted else float("nan")
    p25_pct = diffs_pct_sorted[len(diffs_pct_sorted) // 4] if diffs_pct_sorted else float("nan")
    p75_pct = (
        diffs_pct_sorted[3 * len(diffs_pct_sorted) // 4]
        if diffs_pct_sorted
        else float("nan")
    )

    print()
    print(f"  {'Metric':<12} {'wei':>20}  {'%':>8}")
    print("  " + "-" * 44)
    print(f"  {'p25':<12} {p25:>20,.0f}  {p25_pct:>7.2f}%")
    print(f"  {'median':<12} {median:>20,.0f}  {median_pct:>7.2f}%")
    print(f"  {'p75':<12} {p75:>20,.0f}  {p75_pct:>7.2f}%")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def run_analysis(
    days: int,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    factory = session_factory or get_session_factory()
    since = datetime.now(UTC) - timedelta(days=days)
    print(f"\nCoW Competition Analysis  (last {days} days, since {since:%Y-%m-%d %H:%M} UTC)")

    async with factory() as session:
        await _view_ranking_distribution(session, since)

    async with factory() as session:
        await _view_winner_by_token_pair(session, since)

    async with factory() as session:
        await _view_score_gap(session, since)

    print()


async def main_async(days: int) -> None:
    try:
        await run_analysis(days=days)
    except Exception as exc:
        log.error(
            "analyze_competitors_unhandled",
            error=str(exc),
            error_type=type(exc).__name__,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyse CoW competition data vs our shadow solutions."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Analysis window in days (default: 7).",
    )
    args = parser.parse_args()
    asyncio.run(main_async(days=args.days))


if __name__ == "__main__":
    main()
