"""Shared helpers for analyze_* scripts.

Underscore prefix: not invokable as `python -m scripts._analyze_common` and not
meant for external import — purely an extraction point for cross-script DRY.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from typing import Any

_ETH = 1e18


def print_winner_price_block(rows: Iterable[Any]) -> None:
    """Print the CIP-14-at-winner-prices block, or a backfill hint if empty.

    Phase 4a: scored using OUR trades but the WINNER's clearingPrices —
    isolates "trade selection" from "pricing" failure modes. Caller passes any
    iterable of rows exposing ``.score_vs_winner_prices_wei`` and
    ``.winner_score`` attributes (the columns selected by analyze_* scripts).
    """
    wp_scored = [
        (int(r.score_vs_winner_prices_wei), int(r.winner_score))
        for r in rows
        if r.score_vs_winner_prices_wei is not None and r.winner_score is not None
    ]
    if not wp_scored:
        print(
            "\nscore_vs_winner_prices_wei: not yet populated "
            "— run backfill_winner_price_scores.py"
        )
        return

    wp_ours = [s / _ETH for s, _ in wp_scored]
    wp_deltas = [(s - w) / _ETH for s, w in wp_scored]
    wp_wins = sum(1 for d in wp_deltas if d > 0)
    print("\nCIP-14 Score @ winner prices (ETH):")
    print(
        f"  Ours: mean={statistics.mean(wp_ours):+.6f}  "
        f"median={statistics.median(wp_ours):+.6f}  "
        f"max={max(wp_ours):+.6f}"
    )
    print(
        f"  Delta vs winner: mean={statistics.mean(wp_deltas):+.6f}  "
        f"median={statistics.median(wp_deltas):+.6f}"
    )
    print(
        f"Wins @ winner prices: {wp_wins}/{len(wp_scored)} "
        f"({wp_wins / len(wp_scored):.0%})"
    )
