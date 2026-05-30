"""One-shot diagnostic: why does backfill_winner_price_scores skip every row?

The backfill needs the *winner's* clearingPrices.  It looks in two places:
  1. shadow_winners.raw_solution  → .clearingPrices / .prices
  2. shadow_competitors (is_winner=True) → .clearing_prices   (fallback)

If both are empty for the unscored auctions, every row is skipped.  This
script reports, for the SAME batch the backfill processes, how many rows each
source can actually satisfy — so we know which assumption is wrong.

Usage:
    docker exec cow-solver python -m scripts.diag_winner_prices [--days 30] [--batch 500]
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from src.persistence.db import get_session_factory
from src.persistence.models import (
    ShadowAuction,
    ShadowCompetitor,
    ShadowSolution,
    ShadowWinner,
)


async def diag(days: int, batch_size: int) -> None:
    since = datetime.now(UTC) - timedelta(days=days)
    Session = get_session_factory()

    async with Session() as session:
        # Exact same row set the backfill selects.
        q = await session.execute(
            select(
                ShadowSolution.id,
                ShadowSolution.auction_id,
                ShadowWinner.winner_solver,
                ShadowWinner.raw_solution.label("winner_raw"),
            )
            .join(ShadowAuction, ShadowAuction.auction_id == ShadowSolution.auction_id)
            .join(ShadowWinner, ShadowWinner.auction_id == ShadowSolution.auction_id)
            .where(ShadowSolution.score_vs_winner_prices_wei.is_(None))
            .where(ShadowSolution.solution.is_not(None))
            .where(ShadowSolution.status == "solved")
            .where(ShadowAuction.polled_at >= since)
            .limit(batch_size)
        )
        rows = q.all()
        auction_ids = {r[1] for r in rows}
        print(f"Backfill batch: {len(rows)} solutions across {len(auction_ids)} auctions\n")

        # ── Source 1: shadow_winners.raw_solution ─────────────────────────
        win_has_cp = 0
        win_raw_kinds: Counter[str] = Counter()
        for _sid, _aid, _wsolver, winner_raw in rows:
            if not isinstance(winner_raw, dict):
                win_raw_kinds[type(winner_raw).__name__] += 1
                continue
            keys = set(winner_raw.keys())
            if winner_raw.get("clearingPrices"):
                win_has_cp += 1
                win_raw_kinds["has clearingPrices"] += 1
            elif winner_raw.get("prices"):
                win_has_cp += 1
                win_raw_kinds["has prices"] += 1
            else:
                # show which top-level keys it DOES have
                win_raw_kinds[f"dict keys={sorted(keys)[:6]}"] += 1
        print("── Source 1: shadow_winners.raw_solution ──")
        print(f"   rows with usable winner prices: {win_has_cp}/{len(rows)}")
        for kind, n in win_raw_kinds.most_common(8):
            print(f"     {n:>4}  {kind}")

        # ── Source 2: shadow_competitors ──────────────────────────────────
        comp_q = await session.execute(
            select(
                ShadowCompetitor.auction_id,
                ShadowCompetitor.solver_name,
                ShadowCompetitor.is_winner,
                ShadowCompetitor.ranking,
                ShadowCompetitor.clearing_prices,
            ).where(ShadowCompetitor.auction_id.in_(auction_ids))
        )
        comp_rows = comp_q.all()

        comps_per_auction: Counter[int] = Counter()
        winners_flagged = 0
        winners_with_cp = 0
        ranking1_with_cp = 0
        any_cp_in_auction: set[int] = set()
        for aid, _name, is_winner, ranking, cp in comp_rows:
            comps_per_auction[aid] += 1
            has_cp = bool(cp)
            if has_cp:
                any_cp_in_auction.add(aid)
            if is_winner:
                winners_flagged += 1
                if has_cp:
                    winners_with_cp += 1
            if ranking == 1 and has_cp:
                ranking1_with_cp += 1

        auctions_with_comps = len(comps_per_auction)
        print("\n── Source 2: shadow_competitors ──")
        print(f"   competitor rows for these auctions: {len(comp_rows)}")
        print(f"   auctions that have ANY competitor row: {auctions_with_comps}/{len(auction_ids)}")
        print(f"   rows flagged is_winner=True:           {winners_flagged}")
        print(f"     of which have non-empty clearing_prices: {winners_with_cp}")
        print(f"   rows ranking==1 with clearing_prices:  {ranking1_with_cp}")
        print(f"   auctions with ANY non-empty clearing_prices: {len(any_cp_in_auction)}/{len(auction_ids)}")

        # ── Cross-check: winner_solver match by name ──────────────────────
        # Maybe is_winner is never set but we can match shadow_winners.winner_solver
        # to shadow_competitors.solver_name and read THAT row's clearing_prices.
        winner_solver_by_auction = {r[1]: (r[2] or "") for r in rows}
        cp_by_auction_solver: dict[tuple[int, str], dict] = {}
        for aid, name, _w, _r, cp in comp_rows:
            if cp:
                cp_by_auction_solver[(aid, (name or "").lower())] = cp
        name_match_hits = 0
        for aid, wsolver in winner_solver_by_auction.items():
            if (aid, (wsolver or "").lower()) in cp_by_auction_solver:
                name_match_hits += 1
        print("\n── Cross-check: match winner_solver → competitor.solver_name ──")
        print(f"   auctions where winner_solver name has clearing_prices: {name_match_hits}/{len(auction_ids)}")

        # Sample a few solver_name values to eyeball the naming convention.
        sample_names = Counter((n or "") for _a, n, *_ in comp_rows)
        print("   sample competitor solver_name values:")
        for name, n in sample_names.most_common(6):
            print(f"     {n:>4}  {name!r}")
        sample_winners = Counter(wsolver for wsolver in winner_solver_by_auction.values())
        print("   sample shadow_winners.winner_solver values:")
        for name, n in sample_winners.most_common(6):
            print(f"     {n:>4}  {name!r}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--batch", type=int, default=500)
    args = p.parse_args()
    asyncio.run(diag(days=args.days, batch_size=args.batch))


if __name__ == "__main__":
    main()
