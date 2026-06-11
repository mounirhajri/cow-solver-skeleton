"""Slim aged shadow_auctions rows down to their solution-referenced orders.

``shadow_auctions.raw_auction`` stores per-auction payloads with an
``orders`` array (~955 slim orders ≈ 174 KB/row at ~470 rows/hour from live
driver traffic ≈ 2 GB/day) — the host disk filled twice in the week of
2026-06-08 despite the per-order field diet in ``_slim_raw_auction``.

The stored payloads have exactly two consumers:

1. Ghost detection (``scripts/refresh_ghost_set.py`` DETECTION_SQL) reads
   ``raw_auction->'orders'`` — but only for rows from the last 24 h.
2. Offline probes do ``Auction.model_validate(raw_auction)`` — but only for
   auctions that a ``shadow_solutions`` row actually references.

Therefore a row older than 48 h only needs the orders that one of its own
solutions traded (``shadow_solutions.solution.trades[].order``); the other
~950 orders per row are dead weight. This sidecar rewrites such rows in
place: ``orders`` is filtered to the solution-referenced UIDs and a
top-level ``"_retained": true`` marker is added so the row is never
reprocessed. Everything else (``tokens``, prices, …) stays untouched so the
probes keep round-tripping.

Already-retained rows are skipped in Python rather than in SQL — the marker
lives inside a plain ``json`` column (not ``jsonb``), and a JSON-operator
WHERE clause would also break the sqlite test backend. To avoid rescanning
the same skipped rows forever, the loop keeps the last processed
``polled_at`` in memory and paginates by ``polled_at > watermark``. Rows age
into the 48 h window in ``polled_at`` order, so the watermark never needs
resetting within a run; after a restart the scan starts over, but skip-only
pages are cheap (no per-row solution lookups, no updates).

Usage
-----
    python -m scripts.retention_loop            # one-shot, exits
    python -m scripts.retention_loop --loop     # sidecar, runs every
                                                # RETENTION_INTERVAL_SECONDS
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from scripts.liveness import touch_liveness
from src.config import settings
from src.log import configure_logging, get_logger
from src.persistence.db import get_session_factory
from src.persistence.models import ShadowAuction, ShadowSolution

log = get_logger(__name__)

LIVENESS_PATH = Path("/data/retention.alive")

# Rows younger than this are never touched: ghost detection needs the full
# orders array for its 24 h observation window, and 48 h leaves a 2× margin
# for clock skew / detection-window tuning before retention bites.
RETENTION_AGE_HOURS = 48

# Top-level marker key written into raw_auction after slimming. Its presence
# means "already processed — skip".
RETAINED_MARKER = "_retained"

# Rows rewritten per cycle. 200 × ~174 KB ≈ 35 MB of JSON churn per cycle,
# and at the default 15-min cadence that's 800 rows/h — comfortably above
# the ~470 rows/h aging into the window, so the backlog drains.
DEFAULT_BATCH = 200

DEFAULT_INTERVAL_SECONDS = int(os.environ.get("RETENTION_INTERVAL_SECONDS", "900"))

# After a restart the watermark is gone and the scan restarts at the oldest
# rows, which are typically all retained already. Skip-only pages are cheap,
# so a cycle may page past up to MAX_PAGES_PER_CYCLE × batch skipped rows
# (10 000 at defaults) before yielding. Rewrite work stays capped at
# ``batch`` rows per cycle either way.
MAX_PAGES_PER_CYCLE = 50


@dataclass
class CycleStats:
    """Per-cycle counters, logged verbatim as ``retention_cycle`` fields."""

    n_processed: int = 0
    n_skipped_retained: int = 0
    n_malformed: int = 0
    n_orders_before: int = 0
    n_orders_after: int = 0
    bytes_saved_estimate: int = 0


def _parse_raw(raw: Any) -> dict[str, Any] | None:
    """``raw_auction`` as a dict, or None if unparseable.

    The JSON column type hands back a dict on asyncpg and sqlite alike, but
    be liberal: rows written through text-SQL paths can arrive as a string.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def extract_referenced_uids(solutions: Iterable[Any]) -> set[str]:
    """Lowercase order UIDs referenced by any solution's trades.

    Solution dicts carry the UID under ``order`` (driver schema),
    ``orderUid`` (legacy poller schema) or ``order_uid`` — same fallback
    chain as ``src.shadow.scoring``. Malformed entries are ignored: a UID we
    fail to extract means its order gets dropped from a >48 h-old row, which
    only degrades an offline probe, never the live path.
    """
    uids: set[str] = set()
    for solution in solutions:
        sol = _parse_raw(solution)
        if sol is None:
            continue
        trades = sol.get("trades")
        if not isinstance(trades, list):
            continue
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            uid = trade.get("order") or trade.get("orderUid") or trade.get("order_uid")
            if isinstance(uid, str) and uid:
                uids.add(uid.lower())
    return uids


def retain_row(raw_auction: dict[str, Any], referenced_uids: set[str]) -> dict[str, Any]:
    """Pure transform: drop unreferenced orders, set the retained marker.

    Only ``orders`` is filtered (case-insensitive UID compare); every other
    top-level key — ``tokens`` in particular — passes through untouched so
    ``Auction.model_validate`` keeps working for the offline probes. A
    payload without a list-shaped ``orders`` key still gets the marker so it
    is never rescanned.
    """
    slim = dict(raw_auction)
    orders = slim.get("orders")
    if isinstance(orders, list):
        slim["orders"] = [
            o
            for o in orders
            if isinstance(o, dict) and str(o.get("uid") or "").lower() in referenced_uids
        ]
    slim[RETAINED_MARKER] = True
    return slim


async def run_cycle(
    batch: int = DEFAULT_BATCH,
    watermark: datetime | None = None,
) -> tuple[CycleStats, datetime | None]:
    """Slim up to ``batch`` aged rows; returns stats + advanced watermark.

    The cutoff is computed in Python and bound as a parameter (no
    ``now() - interval`` SQL) so the query runs on both Postgres and the
    sqlite test backend. Pagination is strict ``polled_at > watermark``; in
    the astronomically rare case of identical-microsecond ties straddling a
    page boundary the tied row is picked up after the next restart.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=RETENTION_AGE_HOURS)
    stats = CycleStats()

    Session = get_session_factory()
    async with Session() as session:
        for _ in range(MAX_PAGES_PER_CYCLE):
            stmt = (
                select(
                    ShadowAuction.auction_id,
                    ShadowAuction.raw_auction,
                    ShadowAuction.polled_at,
                )
                .where(ShadowAuction.polled_at < cutoff)
                .order_by(ShadowAuction.polled_at.asc())
                .limit(batch)
            )
            if watermark is not None:
                stmt = stmt.where(ShadowAuction.polled_at > watermark)
            rows = (await session.execute(stmt)).all()

            for auction_id, raw, polled_at in rows:
                watermark = polled_at
                parsed = _parse_raw(raw)
                if parsed is None:
                    stats.n_malformed += 1
                    continue
                if parsed.get(RETAINED_MARKER):
                    stats.n_skipped_retained += 1
                    continue

                sol_rows = (
                    await session.execute(
                        select(ShadowSolution.solution)
                        .where(ShadowSolution.auction_id == auction_id)
                        .where(ShadowSolution.solution.is_not(None))
                    )
                ).scalars()
                referenced = extract_referenced_uids(sol_rows)
                slim = retain_row(parsed, referenced)

                orders_before = parsed.get("orders")
                orders_after = slim.get("orders")
                stats.n_orders_before += (
                    len(orders_before) if isinstance(orders_before, list) else 0
                )
                stats.n_orders_after += (
                    len(orders_after) if isinstance(orders_after, list) else 0
                )
                # Serialized-JSON delta ≈ on-disk savings (json column stores text).
                stats.bytes_saved_estimate += max(
                    0, len(json.dumps(parsed)) - len(json.dumps(slim))
                )

                await session.execute(
                    update(ShadowAuction)
                    .where(ShadowAuction.auction_id == auction_id)
                    .values(raw_auction=slim)
                )
                stats.n_processed += 1

            # Commit per page so a mid-cycle crash keeps completed work and
            # the retained markers make the cycle safely re-runnable.
            await session.commit()

            if len(rows) < batch or stats.n_processed >= batch:
                break

    return stats, watermark


async def loop(interval_seconds: int, batch: int) -> None:
    watermark: datetime | None = None
    while True:
        try:
            stats, watermark = await run_cycle(batch=batch, watermark=watermark)
            log.info("retention_cycle", **asdict(stats))
            touch_liveness(LIVENESS_PATH)
        except Exception as e:  # noqa: BLE001
            log.error("retention_cycle_failed", error=str(e))
        await asyncio.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously (sidecar mode) instead of one-shot.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"Cycle interval in seconds for loop mode (default {DEFAULT_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=DEFAULT_BATCH,
        help=f"Max rows rewritten per cycle (default {DEFAULT_BATCH}).",
    )
    args = parser.parse_args()

    configure_logging(level=settings.log_level)
    if args.loop:
        asyncio.run(loop(args.interval, args.batch))
    else:
        stats, _ = asyncio.run(run_cycle(batch=args.batch))
        log.info("retention_oneshot", **asdict(stats))


if __name__ == "__main__":
    main()
