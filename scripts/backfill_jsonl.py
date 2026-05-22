"""One-off backfill: import existing shadow.jsonl entries into Postgres.

Used after Task 1.1-1.4 deploy so the Postgres tables include auctions
that were captured before the persistence path existed. Idempotent: re-runs
skip already-present rows.

Usage (inside the cow-solver container):
    python -m scripts.backfill_jsonl --path /data/shadow.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from src.log import get_logger
from src.persistence.db import get_session_factory
from src.persistence.models import ShadowAuction, ShadowSolution, ShadowWinner, TokenOutcome
from src.shadow.token_outcomes import extract_token_outcomes

log = get_logger(__name__)


async def backfill(jsonl_path: Path) -> dict[str, int]:
    counts = {"auctions": 0, "solutions": 0, "winners": 0, "outcomes": 0, "skipped": 0}
    Session = get_session_factory()

    with jsonl_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                counts["skipped"] += 1
                continue

            auction_id = int(entry.get("auction_id") or 0)
            if not auction_id:
                counts["skipped"] += 1
                continue

            our_solution = entry.get("our_solution")
            winner_info = entry.get("winner_solution")  # has {solver, score}
            ts_str = entry.get("timestamp")

            try:
                polled_at = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except (AttributeError, ValueError):
                polled_at = datetime.now(UTC)

            async with Session() as session:
                # Skip if auction already present
                existing = await session.execute(
                    select(ShadowAuction).where(ShadowAuction.auction_id == auction_id)
                )
                if existing.scalar_one_or_none() is not None:
                    counts["skipped"] += 1
                    continue

                session.add(ShadowAuction(
                    auction_id=auction_id,
                    polled_at=polled_at,
                    n_orders=int(entry.get("orders_sampled", 0)),
                    raw_competition={"backfilled": True},
                    raw_auction={"backfilled": True},
                ))
                await session.flush()
                counts["auctions"] += 1

                # Our solution as naive attempt (JSONL didn't track per-strategy)
                if our_solution is not None:
                    session.add(ShadowSolution(
                        auction_id=auction_id,
                        strategy="backfilled",
                        status="solved",
                        latency_ms=None,
                        solution=our_solution,
                        error=None,
                    ))
                    counts["solutions"] += 1

                # Winner (minimal info from JSONL)
                if winner_info is not None and winner_info.get("solver"):
                    score_raw = winner_info.get("score")
                    try:
                        score = int(score_raw) if score_raw is not None else None
                    except (ValueError, TypeError):
                        score = None
                    session.add(ShadowWinner(
                        auction_id=auction_id,
                        winner_solver=str(winner_info["solver"]),
                        score=score,
                        raw_solution=winner_info,
                    ))
                    counts["winners"] += 1

                # Token outcomes — extract from our_solution prices (best signal we have)
                # The JSONL doesn't preserve auction.orders here, so token surface comes
                # only from solution prices. This is partial coverage.
                if our_solution is not None and our_solution.get("prices"):
                    pseudo_auction = {
                        "orders": [
                            {"sellToken": t, "buyToken": t}
                            for t in our_solution["prices"].keys()
                        ]
                    }
                    outcomes = extract_token_outcomes(pseudo_auction, None, our_solution)
                    for o in outcomes:
                        session.add(TokenOutcome(
                            auction_id=auction_id,
                            token_address=o["token_address"],
                            appeared_in_winner=False,
                            appeared_in_ours=True,
                            caused_revert=False,
                        ))
                        counts["outcomes"] += 1

                await session.commit()

    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, default=Path("/data/shadow.jsonl"))
    args = parser.parse_args()

    if not args.path.exists():
        log.error("backfill_missing_file", path=str(args.path))
        return

    counts = asyncio.run(backfill(args.path))
    log.info("backfill_done", **counts)
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
