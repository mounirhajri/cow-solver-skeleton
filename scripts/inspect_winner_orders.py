"""One-shot: inspect the structure of winner solution orders to find executed amounts.

Goal: understand whether we can reconstruct clearingPrices from the winner's
executed order data (executedSellAmount / executedBuyAmount fields).

Run on Hetzner:
    docker exec cow-solver python -m scripts.inspect_winner_orders
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from src.persistence.db import get_session_factory
from src.persistence.models import ShadowAuction, ShadowCompetitor, ShadowWinner


async def main() -> None:
    S = get_session_factory()
    async with S() as s:
        # Recent winner rows
        q = await s.execute(
            select(ShadowWinner.auction_id, ShadowWinner.winner_solver, ShadowWinner.raw_solution)
            .order_by(ShadowWinner.auction_id.desc())
            .limit(8)
        )
        rows = q.all()

        for aid, wsolver, rs in rows:
            if not isinstance(rs, dict):
                print(f"aid={aid}: raw_solution is {type(rs).__name__}")
                continue
            cp = rs.get("clearingPrices")
            orders = rs.get("orders") or []
            print(f"\n=== aid={aid}  winner={wsolver} ===")
            print(f"  clearingPrices value: {cp!r}")
            print(f"  orders count: {len(orders)}")
            if orders:
                o0 = orders[0]
                print(f"  orders[0] keys: {sorted(o0.keys())}")
                print(f"  orders[0]: {json.dumps(o0)[:600]}")

        # Also look at the raw_competition winner solution from shadow_auctions
        # in case that has different data than what the poller stored.
        if rows:
            aid0 = rows[0][0]
            aq = await s.execute(
                select(ShadowAuction.raw_competition)
                .where(ShadowAuction.auction_id == aid0)
            )
            rc = aq.scalar_one_or_none()
            if isinstance(rc, dict):
                sols = rc.get("solutions") or []
                winner_comp_sol = next(
                    (sol for sol in sols if sol.get("isWinner")), None
                )
                if winner_comp_sol:
                    print(f"\n=== raw_competition winner sol (aid={aid0}) ===")
                    print(f"  keys: {sorted(winner_comp_sol.keys())}")
                    cp2 = winner_comp_sol.get("clearingPrices")
                    print(f"  clearingPrices: {cp2!r}")
                    ords2 = winner_comp_sol.get("orders") or []
                    print(f"  orders count: {len(ords2)}")
                    if ords2:
                        print(f"  orders[0] keys: {sorted(ords2[0].keys())}")
                        print(f"  orders[0]: {json.dumps(ords2[0])[:600]}")
            else:
                print(f"\n  (no raw_competition for aid={aid0} type={type(rc).__name__})")

        # Check shadow_competitors for the same auction — same API endpoint,
        # but sync_competitions.py may have stored a different slice of the response.
        if rows:
            aid0 = rows[0][0]
            cq = await s.execute(
                select(ShadowCompetitor.solver_name, ShadowCompetitor.is_winner,
                       ShadowCompetitor.clearing_prices, ShadowCompetitor.orders)
                .where(ShadowCompetitor.auction_id == aid0)
                .order_by(ShadowCompetitor.ranking)
            )
            crows = cq.all()
            print(f"\n=== shadow_competitors for aid={aid0} ===")
            for cname, ciw, ccp, cords in crows:
                ords = cords or []
                print(f"  {cname:30s}  is_winner={ciw}  cp={ccp!r}  orders={len(ords)}")
                if ords and ciw:
                    print(f"    orders[0] keys: {sorted(ords[0].keys())}")
                    print(f"    orders[0]: {json.dumps(ords[0])[:400]}")


if __name__ == "__main__":
    asyncio.run(main())
