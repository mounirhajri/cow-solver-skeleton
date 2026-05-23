"""Backfill CIP-14 our_score_wei for shadow_solutions rows that are NULL.

Run once after the d86057a796cb migration to score historical data.

Usage:
    python -m scripts.backfill_scores [--dry-run] [--batch 500]
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select, update

from src.persistence.db import get_session_factory
from src.persistence.models import ShadowAuction, ShadowSolution
from src.shadow.scoring import (
    compute_solution_score,
    extract_native_prices,
    orders_by_uid_from_auction,
)


async def backfill(dry_run: bool = False, batch_size: int = 500) -> None:
    Session = get_session_factory()

    async with Session() as session:
        # Load all unscored solutions that have a solution JSON
        q = await session.execute(
            select(
                ShadowSolution.id,
                ShadowSolution.auction_id,
                ShadowSolution.solution,
                ShadowAuction.raw_auction,
                ShadowAuction.raw_competition,
            )
            .join(ShadowAuction, ShadowAuction.auction_id == ShadowSolution.auction_id)
            .where(ShadowSolution.our_score_wei.is_(None))
            .where(ShadowSolution.solution.is_not(None))
            .limit(batch_size)
        )
        rows = q.all()

    print(f"Found {len(rows)} unscored solutions (batch cap {batch_size})")

    updates: list[dict[str, object]] = []
    zero_count = 0
    error_count = 0

    for sol_id, auction_id, solution, raw_auction, raw_competition in rows:
        try:
            uid_map = orders_by_uid_from_auction(raw_auction or {})
            native_prices = extract_native_prices(raw_competition or {})
            score = compute_solution_score(solution, uid_map, native_prices)
        except Exception as exc:
            print(f"  ERROR aid={auction_id} sid={sol_id}: {exc}")
            error_count += 1
            continue

        if score == 0:
            zero_count += 1
        updates.append({"id": sol_id, "our_score_wei": score if score > 0 else None})

    print(f"  Scored: {len(updates)} | zero/null: {zero_count} | errors: {error_count}")

    if dry_run:
        # Show a few samples
        for u in updates[:5]:
            print(f"  [dry-run] sid={u['id']} score={u['our_score_wei']}")
        print("Dry-run mode — nothing written.")
        return

    # Write in one batch
    async with Session() as session:
        for u in updates:
            await session.execute(
                update(ShadowSolution)
                .where(ShadowSolution.id == u["id"])
                .values(our_score_wei=u["our_score_wei"])
            )
        await session.commit()

    print(f"Updated {len(updates)} rows.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill CIP-14 scores")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch", type=int, default=500)
    args = parser.parse_args()
    asyncio.run(backfill(dry_run=args.dry_run, batch_size=args.batch))


if __name__ == "__main__":
    main()
