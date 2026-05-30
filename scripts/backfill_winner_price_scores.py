"""Backfill ``score_vs_winner_prices_wei`` for shadow_solutions rows that are NULL.

For each solved solution we have stored, recompute its CIP-14 score using the
*winner's* clearingPrices (from shadow_winners.raw_solution) instead of our
own.  This isolates two failure modes (Phase 4a):
  - We picked the wrong trades  →  new score ≈ our_score_wei, both low vs winner
  - Our clearing prices are off →  new score ≪ our_score_wei

For "backfilled" solutions whose raw_auction is a placeholder dict, we fetch
order details from the CoW Protocol API by UID (same fallback path as
backfill_scores.py).

Usage:
    python -m scripts.backfill_winner_price_scores [--days 30] [--dry-run] [--batch 500]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from scripts.backfill_scores import _extract_order_uids, fetch_orders_by_uid
from src.persistence.db import get_session_factory
from src.persistence.models import (
    ShadowAuction,
    ShadowCompetitor,
    ShadowSolution,
    ShadowWinner,
)
from src.shadow.scoring import (
    extract_native_prices,
    orders_by_uid_from_auction,
    score_at_external_prices,
)


def _winner_clearing_prices(raw_solution: dict[str, Any] | None) -> dict[str, Any]:
    """Pull clearingPrices (CoW API) or prices (our solver) from a winner row."""
    if not isinstance(raw_solution, dict):
        return {}
    return raw_solution.get("clearingPrices") or raw_solution.get("prices") or {}


async def backfill(days: int = 30, dry_run: bool = False, batch_size: int = 500) -> None:
    since = datetime.now(UTC) - timedelta(days=days)
    Session = get_session_factory()

    async with Session() as session:
        q = await session.execute(
            select(
                ShadowSolution.id,
                ShadowSolution.auction_id,
                ShadowSolution.solution,
                ShadowAuction.raw_auction,
                ShadowAuction.raw_competition,
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

        # Winner clearing prices live in TWO places and only one is reliable:
        #   1. shadow_winners.raw_solution — the poller's competition snapshot.
        #      Observed 2026-05-30: this frequently lacks clearingPrices, so
        #      _winner_clearing_prices() returns {} and every row gets skipped.
        #   2. shadow_competitors.clearing_prices — populated by
        #      sync_competitions.py from the CoW competition API, which DOES
        #      carry the winner's full clearingPrices.
        # Pre-fetch source #2 (one query) as a fallback keyed by auction_id so
        # the per-row loop below can fill the gap without N extra round-trips.
        auction_ids = {r[1] for r in rows}
        winner_cp_by_auction: dict[int, dict[str, Any]] = {}
        if auction_ids:
            comp_q = await session.execute(
                select(
                    ShadowCompetitor.auction_id,
                    ShadowCompetitor.clearing_prices,
                )
                .where(ShadowCompetitor.is_winner.is_(True))
                .where(ShadowCompetitor.auction_id.in_(auction_ids))
            )
            for aid, cp in comp_q.all():
                if cp:
                    winner_cp_by_auction[aid] = cp

    print(f"Found {len(rows)} unscored solved solutions (batch cap {batch_size})")

    updates: list[dict[str, object]] = []
    zero_count = 0
    error_count = 0
    api_fetch_count = 0
    skipped_no_prices = 0

    for sol_id, auction_id, solution, raw_auction, raw_competition, winner_raw in rows:
        try:
            if not isinstance(solution, dict):
                zero_count += 1
                updates.append({"id": sol_id, "score_vs_winner_prices_wei": None})
                continue

            # Prefer the poller snapshot; fall back to the competition-API
            # clearing prices stored in shadow_competitors (see pre-fetch above).
            clearing_prices = _winner_clearing_prices(winner_raw)
            if not clearing_prices:
                clearing_prices = winner_cp_by_auction.get(auction_id, {})
            if not clearing_prices:
                skipped_no_prices += 1
                continue

            native_prices = extract_native_prices(raw_competition or {})
            # Same fallback as backfill_scores.py: raw_auction.tokens[*].referencePrice
            if not native_prices:
                for addr, tok in (raw_auction or {}).get("tokens", {}).items():
                    ref = tok.get("referencePrice") or tok.get("reference_price")
                    if ref:
                        with contextlib.suppress(ValueError, TypeError):
                            native_prices[addr.lower()] = int(ref)

            uid_map = orders_by_uid_from_auction(raw_auction or {})

            # Placeholder raw_auction ({"backfilled": true}) → fetch from API
            if not uid_map or list(raw_auction or {}) == ["backfilled"]:
                uids = _extract_order_uids(solution)
                if uids:
                    api_fetch_count += len(uids)
                    uid_map = await fetch_orders_by_uid(uids)
                    if not uid_map:
                        print(
                            f"  WARN aid={auction_id} sid={sol_id}: API returned no orders"
                        )

            score = score_at_external_prices(
                solution, uid_map, native_prices, clearing_prices
            )

        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR aid={auction_id} sid={sol_id}: {exc}")
            error_count += 1
            continue

        if score == 0:
            zero_count += 1
        updates.append(
            {
                "id": sol_id,
                "score_vs_winner_prices_wei": score if score > 0 else None,
            }
        )

    print(
        f"  Scored: {len(updates)} | zero/null: {zero_count} | errors: {error_count}"
        f" | skipped (no winner prices): {skipped_no_prices}"
        f" | API fetches: {api_fetch_count}"
    )

    if dry_run:
        for u in updates[:5]:
            score_eth = int(str(u["score_vs_winner_prices_wei"] or 0)) / 1e18
            print(
                f"  [dry-run] sid={u['id']} score={u['score_vs_winner_prices_wei']}"
                f" ({score_eth:.6f} ETH)"
            )
        print("Dry-run mode — nothing written.")
        return

    async with Session() as session:
        for u in updates:
            await session.execute(
                update(ShadowSolution)
                .where(ShadowSolution.id == u["id"])
                .where(ShadowSolution.score_vs_winner_prices_wei.is_(None))
                .values(score_vs_winner_prices_wei=u["score_vs_winner_prices_wei"])
            )
        await session.commit()

    print(f"Updated {len(updates)} rows.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill CIP-14 scores evaluated at winner's clearing prices"
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch", type=int, default=500)
    args = parser.parse_args()
    asyncio.run(backfill(days=args.days, dry_run=args.dry_run, batch_size=args.batch))


if __name__ == "__main__":
    main()
