"""Coverage-vs-Execution breakdown for high-value auction buckets.

Answers: for Bucket 4 (0.01-0.1 ETH) and Bucket 5 (>0.1 ETH), how many
auctions did we lose because we had *no solution at all* (coverage gap) vs.
because our solution scored lower than the winner (execution gap)?

For execution-gap losses, breaks the ratio (our_score / winner_score) into
sub-buckets to separate "close miss" (routing depth) from "far miss"
(structural problem — wrong pair, different market).

Usage:
    docker exec cow-solver python -m scripts.diagnose_bucket_losses
    docker exec cow-solver python -m scripts.diagnose_bucket_losses --hours 48 --days 7
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from src.persistence.db import get_session_factory

_ETH = 1e18
_SEP = "=" * 62


async def run(hours: float, days: int) -> None:
    since = datetime.now(UTC) - timedelta(hours=hours)
    factory = get_session_factory()

    async with factory() as sess:

        rows = await sess.execute(
            text("""
                WITH scored AS (
                    SELECT
                        sw.auction_id,
                        sw.score::numeric          AS winner_score,
                        MAX(ss.our_score_wei)      AS our_best
                    FROM shadow_winners sw
                    JOIN shadow_auctions sa ON sa.auction_id = sw.auction_id
                    LEFT JOIN shadow_solutions ss
                        ON  ss.auction_id = sw.auction_id
                        AND ss.our_score_wei > 0
                    WHERE sa.polled_at > :since
                      AND sw.score IS NOT NULL
                      AND sw.score::numeric >= 1e16
                    GROUP BY sw.auction_id, sw.score
                )
                SELECT
                    CASE
                        WHEN winner_score < 1e17 THEN '4-groß (0.01-0.1 ETH)'
                        ELSE                         '5-mega (>0.1 ETH)'
                    END                              AS bucket,
                    winner_score,
                    our_best,
                    CASE
                        WHEN our_best IS NULL
                            THEN 'A: no_coverage'
                        WHEN our_best >= winner_score
                            THEN 'B: won'
                        WHEN our_best::numeric / winner_score >= 0.90
                            THEN 'C: close_miss   (≥90% of winner)'
                        WHEN our_best::numeric / winner_score >= 0.70
                            THEN 'D: near_miss    (70-90%)'
                        WHEN our_best::numeric / winner_score >= 0.40
                            THEN 'E: far_miss     (40-70%)'
                        ELSE
                            'F: distant_miss (<40%)'
                    END                              AS class
                FROM scored
                ORDER BY bucket, class
            """),
            {"since": since},
        )
        data = rows.fetchall()

    if not data:
        print("No data in the selected window.")
        return

    # ── Aggregate ─────────────────────────────────────────────────────────────
    from collections import defaultdict
    stats: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"n": 0, "sum_winner": 0.0, "sum_ours": 0.0})
    )
    bucket_totals: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "sum_winner": 0.0}
    )

    for r in data:
        b = r.bucket
        c = r.class_
        w = float(r.winner_score or 0)
        o = float(r.our_best or 0)
        stats[b][c]["n"] += 1
        stats[b][c]["sum_winner"] += w
        stats[b][c]["sum_ours"] += o
        bucket_totals[b]["n"] += 1
        bucket_totals[b]["sum_winner"] += w

    # Class labels in display order
    classes = [
        "A: no_coverage",
        "B: won",
        "C: close_miss   (≥90% of winner)",
        "D: near_miss    (70-90%)",
        "E: far_miss     (40-70%)",
        "F: distant_miss (<40%)",
    ]

    print(f"\n{_SEP}")
    print("COVERAGE vs. EXECUTION — Bucket 4+5 Loss Breakdown")
    print(_SEP)
    print(f"Window: last {hours:g}h\n")

    for bucket in sorted(stats.keys()):
        bt = bucket_totals[bucket]
        n_total = bt["n"]
        sum_winner_total = bt["sum_winner"]

        print(f"  {'─'*58}")
        print(f"  {bucket}  —  {n_total} auctions  "
              f"(Σ winner surplus: {sum_winner_total/_ETH:.4f} ETH)")
        print(f"  {'─'*58}")
        print(f"  {'Class':<38} {'n':>4} {'%auc':>6}  "
              f"{'Σwinner(ETH)':>13}  {'Avg ratio':>9}")
        print(f"  {'-'*80}")

        for cls in classes:
            if cls not in stats[bucket]:
                continue
            d = stats[bucket][cls]
            n = d["n"]
            pct_n = 100 * n / n_total if n_total else 0
            sum_w = d["sum_winner"]
            # avg ratio only meaningful for execution classes (not no_coverage)
            if cls != "A: no_coverage" and d["sum_ours"] > 0:
                avg_ratio = d["sum_ours"] / d["sum_winner"] if d["sum_winner"] else 0
                ratio_str = f"{avg_ratio:.1%}"
            else:
                ratio_str = "   —  "
            print(f"  {cls:<38} {n:>4} {pct_n:>5.0f}%  "
                  f"{sum_w/_ETH:>13.4f}  {ratio_str:>9}")

        # Summary insight line
        b_stats = stats[bucket]
        n_no_cov  = b_stats.get("A: no_coverage", {}).get("n", 0)
        n_won     = b_stats.get("B: won", {}).get("n", 0)
        n_close   = b_stats.get("C: close_miss   (≥90% of winner)", {}).get("n", 0)
        n_near    = b_stats.get("D: near_miss    (70-90%)", {}).get("n", 0)
        n_far     = b_stats.get("E: far_miss     (40-70%)", {}).get("n", 0)
        n_distant = b_stats.get("F: distant_miss (<40%)", {}).get("n", 0)
        n_exec_loss = n_close + n_near + n_far + n_distant
        cov_rate = 100 * (n_total - n_no_cov) / n_total if n_total else 0
        win_of_cov = 100 * n_won / (n_total - n_no_cov) if (n_total - n_no_cov) else 0
        print()
        print(f"  → Coverage: {cov_rate:.0f}%  |  Win-of-covered: {win_of_cov:.0f}%")
        dominant = max(
            [("no-solution", n_no_cov),
             ("close miss ≥90%", n_close),
             ("near miss 70-90%", n_near),
             ("far miss 40-70%", n_far),
             ("distant miss <40%", n_distant)],
            key=lambda x: x[1]
        )
        print(f"  → Dominant loss mode: '{dominant[0]}' ({dominant[1]} auctions)")
        print()

    # ── Cross-bucket summary ───────────────────────────────────────────────────
    print(f"\n{_SEP}")
    print("SUMMARY — Was ist der größere Hebel?")
    print(_SEP)

    total_no_cov = sum(
        stats[b].get("A: no_coverage", {}).get("n", 0) for b in stats
    )
    total_exec_loss = sum(
        stats[b].get(c, {}).get("n", 0)
        for b in stats
        for c in classes
        if c not in ("A: no_coverage", "B: won")
    )
    total_won = sum(stats[b].get("B: won", {}).get("n", 0) for b in stats)
    total_all = sum(bt["n"] for bt in bucket_totals.values())

    # Surplus left on the table by loss mode
    surplus_no_cov = sum(
        stats[b].get("A: no_coverage", {}).get("sum_winner", 0.0) for b in stats
    )
    surplus_exec_loss = sum(
        stats[b].get(c, {}).get("sum_winner", 0.0)
        for b in stats
        for c in classes
        if c not in ("A: no_coverage", "B: won")
    )

    print(f"  Auktionen gesamt (Bucket 4+5):  {total_all}")
    print(f"  Gewonnen:                        {total_won} ({100*total_won/total_all:.0f}%)")
    print(f"  Verloren — keine Solution:       {total_no_cov} ({100*total_no_cov/total_all:.0f}%)"
          f"  →  Σ {surplus_no_cov/_ETH:.3f} ETH verpasst")
    print(f"  Verloren — schlechtere Solution: {total_exec_loss} ({100*total_exec_loss/total_all:.0f}%)"
          f"  →  Σ {surplus_exec_loss/_ETH:.3f} ETH verpasst")

    if surplus_no_cov > surplus_exec_loss:
        dominant_lever = "COVERAGE  (mehr Auktionen überhaupt lösen)"
    else:
        dominant_lever = "EXECUTION (bessere Quotes / Routing-Tiefe)"
    print(f"\n  ► Dominanter Hebel: {dominant_lever}")
    print()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--days",  type=int,   default=0,
                   help="Override --hours: use N days instead")
    args = p.parse_args()
    hours = args.days * 24 if args.days else args.hours
    asyncio.run(run(hours=hours, days=args.days))


if __name__ == "__main__":
    main()
