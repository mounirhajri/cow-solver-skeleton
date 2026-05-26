"""Refresh the ghost_orders table.

A ghost-order is an order UID that appears in many auctions but no live
solver ever settles.  Empirically these are EIP-1271 contracts that reject
signature validation at settle-time, abandoned bots, or addresses with
broken approval/balance state.  They pollute bipartite matching because
they look like fillable orders on paper but no real solver bids on them
(verified 2026-05-26: 99.4% of our bipartite solutions involved ghost UIDs,
inflating reported surplus by ~1000x vs ghost-free baseline).

Detection rule:
    seen in >= MIN_AUCTIONS_SEEN distinct auctions in the last OBSERVATION_HOURS,
    AND 0 winner-settlements anywhere in the last SETTLEMENT_LOOKBACK_DAYS.

The settlement lookback is intentionally longer than the observation window:
a slow-fill order may appear in many auctions and still legitimately settle
days later.  Requiring a 7-day settlement-free track-record is conservative
against false positives.

Usage
-----
    python -m scripts.refresh_ghost_set            # one-shot, exits
    python -m scripts.refresh_ghost_set --loop     # sidecar, refreshes every
                                                   # REFRESH_INTERVAL_SECONDS
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from scripts.liveness import touch_liveness
from src.config import settings
from src.log import configure_logging, get_logger
from src.persistence.db import get_session_factory
from src.persistence.models import GhostOrder

log = get_logger(__name__)

LIVENESS_PATH = Path("/data/ghost_refresh.alive")

# Detection thresholds.  Tuned from 2026-05-26 baseline: at MIN_AUCTIONS_SEEN=20
# the rule identified 1245 ghost UIDs in 24h covering 99.4% of polluting
# bipartite trades.  Lower threshold => higher recall but starts catching
# legit-but-slow-fill orders; higher => misses fresh ghost bots.
MIN_AUCTIONS_SEEN = 20
OBSERVATION_HOURS = 24
SETTLEMENT_LOOKBACK_DAYS = 7

# Stale-row cleanup: drop ghost entries that haven't been observed in any
# auction for STALE_AFTER_DAYS.  A previously-blacklisted UID that the
# operator has cancelled is no longer worth filtering against.
STALE_AFTER_DAYS = 14

# Sidecar refresh cadence.  30 min strikes a balance between freshness for
# detecting new ghost-bots and DB load.  Override via --interval if needed.
REFRESH_INTERVAL_SECONDS = 30 * 60


DETECTION_SQL = text(f"""
    WITH visibility AS (
      SELECT
        ord->>'uid' AS uid,
        lower(ord->>'owner') AS owner,
        lower(ord->>'sellToken') AS sell_token,
        lower(ord->>'buyToken') AS buy_token,
        COUNT(DISTINCT sa.auction_id) AS n_auctions_seen,
        MIN(sa.polled_at) AS first_seen_at,
        MAX(sa.polled_at) AS last_seen_at
      FROM shadow_auctions sa,
           LATERAL jsonb_array_elements(sa.raw_auction::jsonb->'orders') AS ord
      WHERE sa.polled_at >= NOW() - INTERVAL '{OBSERVATION_HOURS} hours'
      GROUP BY 1, 2, 3, 4
    ),
    settlements AS (
      SELECT o->>'id' AS uid, COUNT(*) AS c
      FROM shadow_competitors sc,
           LATERAL jsonb_array_elements(sc.orders::jsonb) AS o
      WHERE sc.is_winner = true
        AND sc.polled_at >= NOW() - INTERVAL '{SETTLEMENT_LOOKBACK_DAYS} days'
      GROUP BY 1
    )
    SELECT
      v.uid,
      v.owner,
      v.sell_token,
      v.buy_token,
      v.n_auctions_seen,
      v.first_seen_at,
      v.last_seen_at
    FROM visibility v
    LEFT JOIN settlements s ON s.uid = v.uid
    WHERE v.n_auctions_seen >= {MIN_AUCTIONS_SEEN}
      AND COALESCE(s.c, 0) = 0
""")


async def refresh_once() -> dict[str, int]:
    """Detect ghosts + upsert into ghost_orders.  Returns counts for logging."""
    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(days=STALE_AFTER_DAYS)

    Session = get_session_factory()
    async with Session() as session:
        # Detect
        result = await session.execute(DETECTION_SQL)
        rows = result.fetchall()
        detected_uids = {row[0] for row in rows}

        # Upsert.  ON CONFLICT preserves detected_at (first detection time)
        # and refreshes the volatile columns.
        for row in rows:
            uid, owner, sell_token, buy_token, n_seen, first_seen, last_seen = row
            stmt = pg_insert(GhostOrder).values(
                uid=uid,
                owner=owner,
                sell_token=sell_token,
                buy_token=buy_token,
                n_auctions_seen=n_seen,
                first_seen_at=first_seen,
                last_seen_at=last_seen,
                detected_at=now,
                last_refreshed_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["uid"],
                set_={
                    "n_auctions_seen": stmt.excluded.n_auctions_seen,
                    "last_seen_at": stmt.excluded.last_seen_at,
                    "last_refreshed_at": stmt.excluded.last_refreshed_at,
                    # NOTE: owner/sell_token/buy_token/first_seen_at/detected_at
                    # are intentionally NOT updated — they're stable per UID.
                },
            )
            await session.execute(stmt)

        # Self-correction for slow-fill false positives: drop rows whose UID
        # is no longer detected as ghost (either because the order settled or
        # dropped out of the observation window).  Without this, a UID that
        # gets flagged once and then settles would stay in the blacklist for
        # STALE_AFTER_DAYS days before stale-cleanup catches it.  This shrinks
        # the FP window to ~one refresh-cycle.
        #
        # Implemented via temp table because the detected set can be 1000+ UIDs
        # which is awkward for an inline IN clause.
        await session.execute(
            text(
                "CREATE TEMP TABLE _ghost_refresh_detected (uid VARCHAR(114) PRIMARY KEY) "
                "ON COMMIT DROP"
            )
        )
        if detected_uids:
            await session.execute(
                text("INSERT INTO _ghost_refresh_detected (uid) VALUES (:uid)"),
                [{"uid": u} for u in detected_uids],
            )
        no_longer_ghost = await session.execute(
            text(
                "DELETE FROM ghost_orders "
                "WHERE uid NOT IN (SELECT uid FROM _ghost_refresh_detected) "
                "  AND last_refreshed_at >= :recent_cutoff"
            ),
            {"recent_cutoff": now - timedelta(seconds=REFRESH_INTERVAL_SECONDS * 2)},
        )
        n_self_corrected = no_longer_ghost.rowcount or 0

        # Stale cleanup: rows that have not been observed in any auction
        # for STALE_AFTER_DAYS (operator stopped emitting this UID entirely).
        deleted = await session.execute(
            text("DELETE FROM ghost_orders WHERE last_refreshed_at < :cutoff"),
            {"cutoff": stale_cutoff},
        )
        await session.commit()

        return {
            "detected": len(rows),
            "deleted_stale": deleted.rowcount or 0,
            "self_corrected": n_self_corrected,
        }


async def loop(interval_seconds: int) -> None:
    while True:
        try:
            counts = await refresh_once()
            log.info("ghost_refresh_cycle", **counts)
            touch_liveness(LIVENESS_PATH)
        except Exception as e:  # noqa: BLE001
            log.error("ghost_refresh_failed", error=str(e))
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
        default=REFRESH_INTERVAL_SECONDS,
        help=f"Refresh interval in seconds for loop mode (default {REFRESH_INTERVAL_SECONDS}).",
    )
    args = parser.parse_args()

    configure_logging(level=settings.log_level)
    if args.loop:
        asyncio.run(loop(args.interval))
    else:
        counts = asyncio.run(refresh_once())
        log.info("ghost_refresh_oneshot", **counts)


if __name__ == "__main__":
    main()
