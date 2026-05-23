"""Backfill CIP-14 our_score_wei for shadow_solutions rows that are NULL.

For "backfilled" solutions whose raw_auction has no order data, fetches
order details from the CoW Protocol API by order UID.

Usage:
    python -m scripts.backfill_scores [--dry-run] [--batch 500]
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import httpx
from sqlalchemy import select, update

from src.persistence.db import get_session_factory
from src.persistence.models import ShadowAuction, ShadowSolution
from src.shadow.scoring import (
    compute_solution_score,
    extract_native_prices,
    orders_by_uid_from_auction,
)

COW_API = "https://api.cow.fi/arbitrum/api/v1"


async def fetch_orders_by_uid(
    uids: list[str], client: httpx.AsyncClient
) -> dict[str, dict[str, Any]]:
    """Fetch full order objects from CoW API for a list of UIDs.

    Returns uid (lower) → order dict.  Silently skips failed fetches.
    """
    result: dict[str, dict[str, Any]] = {}
    for uid in uids:
        try:
            resp = await client.get(f"{COW_API}/orders/{uid}", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                result[uid.lower()] = data
        except Exception:  # noqa: BLE001
            pass
    return result


def _extract_order_uids(solution: dict[str, Any]) -> list[str]:
    """Pull orderUid from all fulfillment trades in a solution."""
    return [
        t["orderUid"]
        for t in (solution.get("trades") or [])
        if t.get("kind") == "fulfillment" and t.get("orderUid")
    ]


async def backfill(dry_run: bool = False, batch_size: int = 500) -> None:
    Session = get_session_factory()

    async with Session() as session:
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
    api_fetch_count = 0

    async with httpx.AsyncClient() as client:
        for sol_id, auction_id, solution, raw_auction, raw_competition in rows:
            try:
                # Guard: JSON null deserialises to Python None even when
                # the SQL column is NOT NULL – skip non-dict solutions.
                if not isinstance(solution, dict):
                    zero_count += 1
                    updates.append({"id": sol_id, "our_score_wei": None})
                    continue

                native_prices = extract_native_prices(raw_competition or {})

                # Try orders from raw_auction first (fast path)
                uid_map = orders_by_uid_from_auction(raw_auction or {})

                # Fallback: raw_auction is a placeholder ({"backfilled": true}) —
                # fetch order details from CoW API using trade UIDs
                if not uid_map or list(raw_auction or {}) == ["backfilled"]:
                    uids = _extract_order_uids(solution)
                    if uids:
                        api_fetch_count += len(uids)
                        uid_map = await fetch_orders_by_uid(uids, client)
                        if not uid_map:
                            print(f"  WARN aid={auction_id} sid={sol_id}: API returned no orders")

                score = compute_solution_score(solution, uid_map, native_prices)

            except Exception as exc:  # noqa: BLE001
                print(f"  ERROR aid={auction_id} sid={sol_id}: {exc}")
                error_count += 1
                continue

            if score == 0:
                zero_count += 1
            updates.append({"id": sol_id, "our_score_wei": score if score > 0 else None})

    print(
        f"  Scored: {len(updates)} | zero/null: {zero_count} | errors: {error_count}"
        f" | API fetches: {api_fetch_count}"
    )

    if dry_run:
        for u in updates[:5]:
            score_eth = int(str(u["our_score_wei"] or 0)) / 1e18
            print(f"  [dry-run] sid={u['id']} score={u['our_score_wei']} ({score_eth:.6f} ETH)")
        print("Dry-run mode — nothing written.")
        return

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
