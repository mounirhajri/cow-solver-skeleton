"""Datenintegritäts- und Score-Validierung — läuft im cow-solver Container.

Usage:
    docker exec cow-solver python -m scripts.validate_data
    docker exec cow-solver python -m scripts.validate_data --hours 8
    docker exec cow-solver python -m scripts.validate_data --hours 24 --fix-cutoff "2026-05-29 10:00:00"

Prüft 5 Schichten:
1. Score-Sanity         — Werte im realistischen ETH-Bereich?
2. JSON-Felder          — prices/trades in solution JSONB vorhanden?
3. Join-Integrität      — keine verwaisten auction_ids?
4. Score-Gap            — unser Score vs. Winner pro Strategie
5. Composer-Fix         — Composer-Lösungen >= beste Einzel-Strategie?
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from src.persistence.db import get_session_factory

SEP = "=" * 60
OK = "✓"
WARN = "⚠"


async def run(hours: float, fix_cutoff: str | None) -> None:
    since = datetime.now(UTC) - timedelta(hours=hours)
    factory = get_session_factory()

    async with factory() as sess:
        await _check_score_sanity(sess, since)
        await _check_json_fields(sess, since)
        await _check_join_integrity(sess, since)
        await _check_score_gap(sess, since)
        await _check_ebbo(sess, since)
        await _check_null_scores(sess, since)
        if fix_cutoff:
            await _check_before_after_fix(sess, since, fix_cutoff)
        await _check_composer(sess, since)
    print()


# ── 1: Score-Sanity ───────────────────────────────────────────────────────


async def _check_score_sanity(sess, since) -> None:
    print(f"\n{SEP}")
    print("PRÜFUNG 1: Score-Sanity (Wertebereich)")
    print(SEP)

    rows = await sess.execute(
        text(
            """
            SELECT
                strategy,
                COUNT(*)                                                    AS solved_n,
                COUNT(*) FILTER (WHERE our_score_wei IS NULL)               AS null_score,
                COUNT(*) FILTER (WHERE our_score_wei = 0)                   AS zero_score,
                COUNT(*) FILTER (WHERE our_score_wei < 0)                   AS negative,
                COUNT(*) FILTER (WHERE our_score_wei > 5000000000000000000) AS over_5eth,
                ROUND(MIN(our_score_wei::numeric) / 1e18::numeric, 8)          AS min_eth,
                ROUND(MAX(our_score_wei::numeric) / 1e18::numeric, 8)          AS max_eth,
                ROUND(
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY our_score_wei::numeric)::numeric
                    / 1e18::numeric, 8
                )                                                           AS median_eth
            FROM shadow_solutions
            WHERE status = 'solved'
              AND created_at > :since
            GROUP BY strategy
            ORDER BY strategy
            """
        ),
        {"since": since},
    )
    data = rows.fetchall()

    if not data:
        print(f"  {WARN} Keine solved-Zeilen im Fenster.")
        return

    print(
        f"  {'Strategie':<32} {'n':>5} {'NULL':>5} {'=0':>5} {'<0':>5} "
        f"{'>5ETH':>6} {'min_ETH':>12} {'max_ETH':>12} {'median_ETH':>12}"
    )
    print("  " + "-" * 100)
    all_ok = True
    for r in data:
        flag = ""
        if r.null_score:
            flag += f"  {WARN} {r.null_score} NULL-Scores"
        if r.zero_score:
            flag += f"  {WARN} {r.zero_score} Null-Werte"
        if r.negative:
            flag += f"  {WARN} NEGATIV!"
        if r.over_5eth:
            flag += f"  {WARN} {r.over_5eth} Rows >5ETH"
        if flag:
            all_ok = False
        print(
            f"  {r.strategy:<32} {r.solved_n:>5} {r.null_score:>5} {r.zero_score:>5} "
            f"{r.negative:>5} {r.over_5eth:>6} "
            f"{float(r.min_eth or 0):>12.8f} {float(r.max_eth or 0):>12.8f} "
            f"{float(r.median_eth or 0):>12.8f}{flag}"
        )
    if all_ok:
        print(f"\n  {OK} Alle Scores im erwarteten Bereich (>0, ≤5 ETH)")


# ── 2: JSON-Felder ────────────────────────────────────────────────────────


async def _check_json_fields(sess, since) -> None:
    print(f"\n{SEP}")
    print("PRÜFUNG 2: JSON-Felder in solution JSONB")
    print(SEP)

    rows = await sess.execute(
        text(
            """
            SELECT
                strategy,
                COUNT(*)                                              AS n,
                COUNT(*) FILTER (WHERE solution ? 'prices')           AS has_prices,
                COUNT(*) FILTER (WHERE solution ? 'trades')           AS has_trades,
                COUNT(*) FILTER (WHERE solution ? 'interactions')     AS has_interactions,
                COUNT(*) FILTER (WHERE
                    NOT (solution ? 'prices') OR NOT (solution ? 'trades')
                )                                                     AS missing_fields,
                COUNT(*) FILTER (WHERE
                    solution ? 'trades'
                    AND jsonb_array_length(solution -> 'trades') = 0
                )                                                     AS empty_trades
            FROM shadow_solutions
            WHERE status = 'solved'
              AND created_at > :since
            GROUP BY strategy
            ORDER BY strategy
            """
        ),
        {"since": since},
    )
    data = rows.fetchall()

    if not data:
        print(f"  {WARN} Keine solved-Zeilen im Fenster.")
        return

    print(
        f"  {'Strategie':<32} {'n':>5} {'prices':>7} {'trades':>7} "
        f"{'interact':>9} {'fehlt':>6} {'leer':>6}"
    )
    print("  " + "-" * 80)
    all_ok = True
    for r in data:
        flag = ""
        if r.missing_fields:
            flag = f"  {WARN} {r.missing_fields} Rows ohne prices/trades!"
            all_ok = False
        if r.empty_trades:
            flag += f"  {WARN} {r.empty_trades} leere trades-Arrays"
            all_ok = False
        print(
            f"  {r.strategy:<32} {r.n:>5} {r.has_prices:>7} {r.has_trades:>7} "
            f"{r.has_interactions:>9} {r.missing_fields:>6} {r.empty_trades:>6}{flag}"
        )
    if all_ok:
        print(f"\n  {OK} Alle solved-Lösungen haben prices + trades im JSON")

    # Stichprobe: ein fehlerhafter Trade-Eintrag
    bad = await sess.execute(
        text(
            """
            SELECT id, auction_id, strategy,
                   jsonb_array_length(solution -> 'trades') AS n_trades,
                   solution -> 'trades' -> 0                AS first_trade
            FROM shadow_solutions
            WHERE status = 'solved'
              AND created_at > :since
              AND (NOT (solution ? 'prices') OR NOT (solution ? 'trades'))
            LIMIT 3
            """
        ),
        {"since": since},
    )
    bad_rows = bad.fetchall()
    if bad_rows:
        print(f"\n  {WARN} Stichprobe fehlerhafter Zeilen:")
        for r in bad_rows:
            print(f"    id={r.id} auction={r.auction_id} strat={r.strategy} "
                  f"n_trades={r.n_trades} first_trade={r.first_trade}")


# ── 3: Join-Integrität ────────────────────────────────────────────────────


async def _check_join_integrity(sess, since) -> None:
    print(f"\n{SEP}")
    print("PRÜFUNG 3: Cross-Table Join-Integrität")
    print(SEP)

    # solutions → auctions
    row = await sess.execute(
        text(
            """
            SELECT
                COUNT(DISTINCT s.auction_id)                                  AS sol_ids,
                COUNT(DISTINCT a.auction_id)                                  AS matched,
                COUNT(DISTINCT s.auction_id) FILTER (WHERE a.auction_id IS NULL) AS orphaned
            FROM shadow_solutions s
            LEFT JOIN shadow_auctions a USING (auction_id)
            WHERE s.created_at > :since
            """
        ),
        {"since": since},
    )
    r = row.one()
    flag = f"  {WARN} {r.orphaned} verwaiste solution.auction_ids!" if r.orphaned else f"  {OK}"
    print(f"  shadow_solutions → shadow_auctions:  {r.sol_ids} IDs, {r.orphaned} verwaist{flag}")

    # winners → auctions
    row = await sess.execute(
        text(
            """
            SELECT
                COUNT(DISTINCT w.auction_id)                                  AS win_ids,
                COUNT(DISTINCT w.auction_id) FILTER (WHERE a.auction_id IS NULL) AS orphaned
            FROM shadow_winners w
            LEFT JOIN shadow_auctions a USING (auction_id)
            WHERE w.polled_at > :since
            """
        ),
        {"since": since},
    )
    r = row.one()
    flag = f"  {WARN} {r.orphaned} verwaiste winner.auction_ids!" if r.orphaned else f"  {OK}"
    print(f"  shadow_winners  → shadow_auctions:  {r.win_ids} IDs, {r.orphaned} verwaist{flag}")

    # Doppelte solution pro (auction_id, strategy)
    row = await sess.execute(
        text(
            """
            SELECT COUNT(*) AS dupes
            FROM (
                SELECT auction_id, strategy, COUNT(*) AS c
                FROM shadow_solutions
                WHERE created_at > :since
                GROUP BY auction_id, strategy
                HAVING COUNT(*) > 1
            ) sub
            """
        ),
        {"since": since},
    )
    dupes = row.scalar()
    if dupes:
        print(f"  {WARN} {dupes} (auction_id, strategy)-Paare mit >1 Zeile (Duplikate)")
    else:
        print(f"  {OK} Keine doppelten (auction_id, strategy)-Einträge")


# ── 4: Score-Gap ─────────────────────────────────────────────────────────


async def _check_score_gap(sess, since) -> None:
    print(f"\n{SEP}")
    print("PRÜFUNG 4: Score-Gap pro Strategie (CIP-14 Konformität)")
    print(SEP)

    rows = await sess.execute(
        text(
            """
            SELECT
                s.strategy,
                COUNT(DISTINCT s.auction_id)                                          AS n,
                ROUND(AVG(s.our_score_wei::numeric) / 1e18::numeric, 6)              AS avg_our_eth,
                ROUND(AVG(w.score::numeric) / 1e18::numeric, 6)                    AS avg_winner_eth,
                ROUND(
                    100 * AVG(s.our_score_wei::numeric)
                    / NULLIF(AVG(w.score::numeric), 0), 1
                )                                                                     AS gap_pct,
                COUNT(*) FILTER (WHERE s.our_score_wei::numeric >= w.score::numeric)  AS we_win
            FROM shadow_solutions s
            JOIN shadow_winners w USING (auction_id)
            WHERE s.status = 'solved'
              AND s.our_score_wei IS NOT NULL
              AND w.score IS NOT NULL
              AND s.created_at > :since
            GROUP BY s.strategy
            ORDER BY gap_pct DESC NULLS LAST
            """
        ),
        {"since": since},
    )
    data = rows.fetchall()

    if not data:
        print(f"  {WARN} Keine Daten für Score-Vergleich (keine überlappenden auction_ids).")
        return

    print(
        f"  {'Strategie':<32} {'n':>5} {'ØUnser(ETH)':>12} {'ØWinner(ETH)':>13} "
        f"{'Gap%':>7} {'Wins':>6}"
    )
    print("  " + "-" * 85)
    for r in data:
        gap = float(r.gap_pct or 0)
        flag = ""
        if gap < 50:
            flag = f"  {WARN} Score <50% des Winners"
        elif gap >= 100:
            flag = f"  ★ würden gewinnen"
        print(
            f"  {r.strategy:<32} {r.n:>5} {float(r.avg_our_eth or 0):>12.6f} "
            f"{float(r.avg_winner_eth or 0):>13.6f} {gap:>6.1f}% {r.we_win:>6}{flag}"
        )

    # score_vs_winner_prices_wei: Preiseffizienz
    rows2 = await sess.execute(
        text(
            """
            SELECT
                strategy,
                COUNT(*) AS n,
                ROUND(AVG(our_score_wei::numeric) / 1e18::numeric, 6)        AS avg_own,
                ROUND(AVG(score_vs_winner_prices_wei::numeric) / 1e18::numeric, 6) AS avg_at_winner,
                ROUND(
                    100 * AVG(score_vs_winner_prices_wei::numeric)
                    / NULLIF(AVG(our_score_wei::numeric), 0), 1
                )                                                              AS price_eff_pct
            FROM shadow_solutions
            WHERE status = 'solved'
              AND our_score_wei IS NOT NULL
              AND score_vs_winner_prices_wei IS NOT NULL
              AND created_at > :since
            GROUP BY strategy
            ORDER BY strategy
            """
        ),
        {"since": since},
    )
    data2 = rows2.fetchall()

    if data2:
        print()
        print("  Preiseffizienz (score_vs_winner_prices_wei / our_score_wei):")
        print(f"  {'Strategie':<32} {'n':>5} {'ØEigen(ETH)':>12} {'ØBeiWinner(ETH)':>15} {'PrEff%':>8}")
        print("  " + "-" * 78)
        for r in data2:
            eff = float(r.price_eff_pct or 0)
            flag = ""
            if eff > 150:
                flag = f"  {WARN} Phantom-Score?"
            elif eff < 50:
                flag = f"  {WARN} Schlechte Preise"
            print(
                f"  {r.strategy:<32} {r.n:>5} {float(r.avg_own or 0):>12.6f} "
                f"{float(r.avg_at_winner or 0):>15.6f} {eff:>7.1f}%{flag}"
            )
    else:
        print(f"\n  (score_vs_winner_prices_wei nicht befüllt — Backfill optional)")


# ── 5: EBBO ───────────────────────────────────────────────────────────────


async def _check_ebbo(sess, since) -> None:
    print(f"\n{SEP}")
    print("PRÜFUNG 5: EBBO-Ablehnungen")
    print(SEP)

    rows = await sess.execute(
        text(
            """
            SELECT
                strategy,
                COUNT(*)                AS n,
                ROUND(AVG(latency_ms)::numeric)  AS avg_lat_ms,
                MIN(created_at)         AS first,
                MAX(created_at)         AS last
            FROM shadow_solutions
            WHERE status = 'ebbo_rejected'
              AND created_at > :since
            GROUP BY strategy
            ORDER BY n DESC
            """
        ),
        {"since": since},
    )
    data = rows.fetchall()

    if not data:
        print(f"  {OK} Keine EBBO-Ablehnungen im Fenster")
        return

    total_solved = await sess.scalar(
        text(
            "SELECT COUNT(*) FROM shadow_solutions "
            "WHERE status IN ('solved','ebbo_rejected') AND created_at > :since"
        ),
        {"since": since},
    )
    total_ebbo = sum(r.n for r in data)
    ebbo_pct = 100 * total_ebbo / total_solved if total_solved else 0

    print(f"  EBBO-Ablehnungen gesamt: {total_ebbo} ({ebbo_pct:.1f}% aller solved+ebbo)")
    print()
    print(f"  {'Strategie':<32} {'n':>5} {'ØLatenz':>9} {'Erste':>20} {'Letzte':>20}")
    print("  " + "-" * 90)
    for r in data:
        first = r.first.strftime("%d.%m %H:%M") if r.first else "n/a"
        last = r.last.strftime("%d.%m %H:%M") if r.last else "n/a"
        print(
            f"  {r.strategy:<32} {r.n:>5} {float(r.avg_lat_ms or 0):>8.0f}ms "
            f"{first:>20} {last:>20}"
        )


# ── 6: NULL-Score Diagnose ────────────────────────────────────────────────


async def _check_null_scores(sess, since) -> None:
    print(f"\n{SEP}")
    print("PRÜFUNG 6: NULL-Scores bei solved-Zeilen")
    print(SEP)

    total_null = await sess.scalar(
        text(
            "SELECT COUNT(*) FROM shadow_solutions "
            "WHERE status = 'solved' AND our_score_wei IS NULL AND created_at > :since"
        ),
        {"since": since},
    )

    if not total_null:
        print(f"  {OK} Keine NULL-Scores bei solved-Lösungen")
        return

    print(f"  {WARN} {total_null} solved-Zeilen ohne our_score_wei")

    rows = await sess.execute(
        text(
            """
            SELECT
                id, auction_id, strategy, created_at, error,
                solution ? 'prices' AS has_prices,
                solution ? 'trades' AS has_trades,
                CASE
                    WHEN solution ? 'trades'
                    THEN jsonb_array_length(solution -> 'trades')
                    ELSE NULL
                END AS n_trades
            FROM shadow_solutions
            WHERE status = 'solved'
              AND our_score_wei IS NULL
              AND created_at > :since
            ORDER BY created_at DESC
            LIMIT 10
            """
        ),
        {"since": since},
    )
    data = rows.fetchall()

    print()
    print("  Letzte 10 NULL-Score-Rows:")
    print(f"  {'id':>8} {'auction_id':>12} {'strategie':<28} {'Zeit':>17} "
          f"{'prices':>7} {'trades':>7} {'n_t':>4}  Fehler")
    print("  " + "-" * 110)
    for r in data:
        zeit = r.created_at.strftime("%d.%m %H:%M") if r.created_at else "n/a"
        err = (r.error or "")[:50]
        print(
            f"  {r.id:>8} {r.auction_id:>12} {r.strategy:<28} {zeit:>17} "
            f"{str(r.has_prices):>7} {str(r.has_trades):>7} "
            f"{str(r.n_trades or ''):>4}  {err}"
        )


# ── 7: Vorher/Nachher Router-Fix ─────────────────────────────────────────


async def _check_before_after_fix(sess, since, fix_cutoff_str: str) -> None:
    print(f"\n{SEP}")
    print(f"PRÜFUNG 7: Vorher/Nachher Router-Fix ({fix_cutoff_str})")
    print(SEP)

    try:
        fix_cutoff = datetime.fromisoformat(fix_cutoff_str).replace(tzinfo=UTC)
    except ValueError:
        print(f"  {WARN} Ungültiges --fix-cutoff Format. Erwartet: 'YYYY-MM-DD HH:MM:SS'")
        return

    rows = await sess.execute(
        text(
            """
            SELECT
                CASE WHEN created_at < :cutoff THEN 'before' ELSE 'after' END AS period,
                status,
                COUNT(*) AS n
            FROM shadow_solutions
            WHERE strategy = 'router_v2'
              AND created_at > :since
            GROUP BY 1, 2
            ORDER BY 1, 3 DESC
            """
        ),
        {"since": since, "cutoff": fix_cutoff},
    )
    data = rows.fetchall()

    if not data:
        print(f"  Keine router_v2-Daten im Fenster.")
        return

    by_period: dict[str, dict[str, int]] = {"before": {}, "after": {}}
    for r in data:
        by_period[r.period][r.status] = r.n

    for period, label in [("before", f"Vor Fix  (<{fix_cutoff_str})"),
                           ("after",  f"Nach Fix (≥{fix_cutoff_str})")]:
        d = by_period[period]
        total = sum(d.values())
        if not total:
            continue
        print(f"\n  {label}:")
        for status, n in sorted(d.items(), key=lambda x: -x[1]):
            pct = 100 * n / total
            print(f"    {status:<20} {n:>5}  ({pct:.1f}%)")


# ── 8: Composer-Fix ───────────────────────────────────────────────────────


async def _check_composer(sess, since) -> None:
    print(f"\n{SEP}")
    print("PRÜFUNG 8: Composer-Fix (CIP-14 surplus_estimate)")
    print(SEP)

    total_composer = await sess.scalar(
        text(
            "SELECT COUNT(*) FROM shadow_solutions "
            "WHERE strategy = 'composer' AND status = 'solved' AND created_at > :since"
        ),
        {"since": since},
    )

    if not total_composer:
        print("  Keine Composer-Lösungen im Fenster (composer läuft nur wenn 2+ Strategien solved).")
        return

    print(f"  Composer-Lösungen (solved): {total_composer}")

    rows = await sess.execute(
        text(
            """
            SELECT
                c.auction_id,
                c.our_score_wei                      AS comp_score,
                b.our_score_wei                      AS bipartite_score,
                r.our_score_wei                      AS router_score,
                GREATEST(
                    COALESCE(b.our_score_wei::numeric, 0),
                    COALESCE(r.our_score_wei::numeric, 0)
                )                                    AS best_single,
                CASE
                    WHEN c.our_score_wei::numeric >= GREATEST(
                        COALESCE(b.our_score_wei::numeric, 0),
                        COALESCE(r.our_score_wei::numeric, 0)
                    ) THEN 'OK'
                    ELSE 'LOWER_THAN_SINGLE'
                END                                  AS sanity,
                c.created_at
            FROM shadow_solutions c
            LEFT JOIN shadow_solutions b
                ON b.auction_id = c.auction_id AND b.strategy = 'bipartite'
            LEFT JOIN shadow_solutions r
                ON r.auction_id = c.auction_id AND r.strategy = 'router_v2'
            WHERE c.strategy = 'composer'
              AND c.status = 'solved'
              AND c.created_at > :since
            ORDER BY c.created_at DESC
            LIMIT 20
            """
        ),
        {"since": since},
    )
    data = rows.fetchall()

    n_ok = sum(1 for r in data if r.sanity == "OK")
    n_bad = len(data) - n_ok

    if n_bad == 0:
        print(f"  {OK} Alle {len(data)} Composer-Lösungen >= beste Einzel-Strategie")
    else:
        print(f"  {WARN} {n_bad}/{len(data)} Composer-Lösungen UNTER Einzel-Strategie-Score")

    print()
    print(f"  {'Auktion':>12} {'Composer(ETH)':>14} {'Bipartite':>12} "
          f"{'RouterV2':>12} {'Beste-Einzel':>13} {'Status':>18}")
    print("  " + "-" * 90)
    for r in data:
        def fmt(v):
            return f"{float(v) / 1e18:.8f}" if v is not None else "        -"

        flag = "" if r.sanity == "OK" else f"  {WARN}"
        print(
            f"  {r.auction_id:>12} {fmt(r.comp_score):>14} {fmt(r.bipartite_score):>12} "
            f"{fmt(r.router_score):>12} {fmt(r.best_single):>13} {r.sanity:>18}{flag}"
        )


# ── Main ──────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Datenintegritäts- und Score-Validierung")
    parser.add_argument(
        "--hours", type=float, default=24,
        help="Zeitfenster in Stunden (default: 24)",
    )
    parser.add_argument(
        "--fix-cutoff",
        default=None,
        metavar="YYYY-MM-DD HH:MM:SS",
        help="Timestamp des Router-Fixes für Vorher/Nachher-Vergleich (UTC)",
    )
    args = parser.parse_args()
    asyncio.run(run(hours=args.hours, fix_cutoff=args.fix_cutoff))


if __name__ == "__main__":
    main()
