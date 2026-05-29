"""24h data verification script — run inside the cow-solver container.

Usage:
    docker exec cow-solver python -m scripts.verify_24h
    docker exec cow-solver python -m scripts.verify_24h --hours 6
    docker exec cow-solver python -m scripts.verify_24h --sample 10

Checks 4 layers:
1. Datenfluss     — Auktionen ankommen? Regelmäßig?
2. Solver-Output  — Welche Strategien liefern? Scores plausibel?
3. Winner-Daten   — Competitor-Vergleich funktioniert?
4. Stichproben    — 5 zufällige Auktionen End-to-End verfolgen
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from src.persistence.db import get_session_factory

SEP = "=" * 60


async def run(hours: int, sample_n: int) -> None:
    since = datetime.now(UTC) - timedelta(hours=hours)
    factory = get_session_factory()

    async with factory() as sess:
        await _check_datenfluss(sess, since, hours)
        await _check_solver_output(sess, since)
        await _check_winner_data(sess, since)
        await _check_phantom_score(sess, since)
        await _check_comp_sync(sess, since)
        await _check_ghost_orders(sess)
        await _stichproben(sess, since, sample_n)


# ── Schicht 1: Datenfluss ─────────────────────────────────────────────────


async def _check_datenfluss(sess, since, hours: int) -> None:
    print(f"\n{SEP}")
    print(f"SCHICHT 1: Datenfluss (letzte {hours}h)")
    print(SEP)

    # Gesamt
    total = await sess.scalar(
        text("SELECT COUNT(*) FROM shadow_auctions WHERE polled_at > :since"),
        {"since": since},
    )
    print(f"  Auktionen gesamt:      {total or 0:>6}")

    if not total:
        print("  ⚠ KEINE DATEN — shadow-poller liefert nichts an die DB!")
        print("    Prüfe: docker logs cow-shadow-poller --since 10m")
        return

    # Neueste + älteste
    row = await sess.execute(
        text(
            "SELECT MIN(polled_at), MAX(polled_at), AVG(n_orders) "
            "FROM shadow_auctions WHERE polled_at > :since"
        ),
        {"since": since},
    )
    min_t, max_t, avg_orders = row.one()
    now = datetime.now(UTC)
    lag = (now - max_t.replace(tzinfo=UTC)).total_seconds() if max_t else 9999

    print(f"  Älteste Auktion:       {min_t.strftime('%H:%M:%S UTC') if min_t else 'n/a'}")
    print(f"  Neueste Auktion:       {max_t.strftime('%H:%M:%S UTC') if max_t else 'n/a'}")
    print(f"  Lag seit letzter:      {lag:.0f}s")
    print(f"  Ø Aufträge/Auktion:    {float(avg_orders or 0):.1f}")

    if lag > 300:
        print(f"  ⚠ Lag > 5 min ({lag:.0f}s) — shadow-poller hängt oder rate-limited?")
    else:
        print("  ✓ Daten kommen regelmäßig an")

    # Stundenweise Verteilung
    rows = await sess.execute(
        text(
            "SELECT date_trunc('hour', polled_at) AS h, COUNT(*) AS n "
            "FROM shadow_auctions WHERE polled_at > :since "
            "GROUP BY h ORDER BY h DESC LIMIT 12"
        ),
        {"since": since},
    )
    print("\n  Stundenweise (letzte 12h):")
    for h, n in rows:
        bar = "█" * min(n, 40)
        print(f"    {h.strftime('%H:00'):>6}  {n:>4}  {bar}")


# ── Schicht 2: Solver-Output ──────────────────────────────────────────────


async def _check_solver_output(sess, since) -> None:
    print(f"\n{SEP}")
    print("SCHICHT 2: Solver-Output pro Strategie")
    print(SEP)

    rows = await sess.execute(
        text(
            """
            SELECT
                strategy,
                COUNT(*) AS n_attempts,
                COUNT(*) FILTER (WHERE status = 'solved') AS n_solutions,
                COUNT(*) FILTER (WHERE our_score_wei IS NOT NULL AND our_score_wei > 0) AS n_with_score,
                AVG(our_score_wei::numeric) FILTER (WHERE our_score_wei > 0) AS avg_score,
                MAX(our_score_wei::numeric) AS max_score,
                AVG(latency_ms) AS avg_latency_ms
            FROM shadow_solutions
            WHERE created_at > :since
            GROUP BY strategy
            ORDER BY n_solutions DESC
            """
        ),
        {"since": since},
    )

    data = rows.fetchall()
    if not data:
        print("  ⚠ KEINE shadow_solutions — Solver schreibt nicht in die DB!")
        print("    Prüfe: docker logs cow-solver --since 10m | tail -50")
        return

    print(f"  {'Strategie':<30} {'Versuche':>8} {'Lösungen':>8} {'m.Score':>8} {'ØScore(ETH)':>12} {'MaxScore(ETH)':>14} {'ØLatenz':>9}")
    print("  " + "-" * 95)
    for strategy, n_att, n_sol, _n_scored, avg_score, max_score, avg_lat in data:
        avg_eth = float(avg_score or 0) / 1e18
        max_eth = float(max_score or 0) / 1e18
        lat = f"{float(avg_lat or 0):.0f}ms"
        solve_rate = f"{100*n_sol/n_att:.0f}%" if n_att else "n/a"
        print(
            f"  {strategy:<30} {n_att:>8} {n_sol:>7} {solve_rate:>8} "
            f"{avg_eth:>11.6f} {max_eth:>13.6f} {lat:>9}"
        )

    # Solve-Rate gesamt
    total_att = sum(r[1] for r in data)
    total_sol = sum(r[2] for r in data)
    if total_att:
        print(f"\n  Gesamt-Solve-Rate: {100*total_sol/total_att:.1f}% ({total_sol}/{total_att})")

    # Auffälligkeiten
    for strategy, _n_att, _n_sol, n_scored, avg_score, _max_score, _avg_lat in data:
        if strategy == "naive" and n_scored and n_scored > 0:
            print(f"  ⚠ naive hat {n_scored} Zeilen mit Score > 0 — sollte NULL sein!")
        if avg_score and float(avg_score) > 1e36:
            print(f"  ⚠ {strategy}: Score > 1e36 — Multi-Party Score-Inflation aktiv?")


# ── Schicht 3: Winner-Daten ───────────────────────────────────────────────


async def _check_winner_data(sess, since) -> None:
    print(f"\n{SEP}")
    print("SCHICHT 3: Winner-Daten & Score-Gap")
    print(SEP)

    # Winner-Coverage
    row = await sess.execute(
        text(
            """
            SELECT
                COUNT(DISTINCT sa.auction_id) AS auctions,
                COUNT(DISTINCT sw.auction_id) AS with_winner
            FROM shadow_auctions sa
            LEFT JOIN shadow_winners sw ON sa.auction_id = sw.auction_id
            WHERE sa.polled_at > :since
            """
        ),
        {"since": since},
    )
    auctions, with_winner = row.one()
    coverage = f"{100*with_winner/auctions:.1f}%" if auctions else "n/a"
    print(f"  Auktionen gesamt:      {auctions or 0:>6}")
    print(f"  Davon mit Winner:      {with_winner or 0:>6}  ({coverage})")
    if auctions and with_winner and with_winner / auctions < 0.5:
        print("  ⚠ Winner-Coverage < 50% — comp-sync könnte hängen")

    # Top-5 Gewinner-Solver
    rows = await sess.execute(
        text(
            """
            SELECT sw.winner_solver, COUNT(*) AS wins
            FROM shadow_winners sw
            JOIN shadow_auctions sa ON sa.auction_id = sw.auction_id
            WHERE sa.polled_at > :since
            GROUP BY sw.winner_solver
            ORDER BY wins DESC LIMIT 5
            """
        ),
        {"since": since},
    )
    winners = rows.fetchall()
    if winners:
        print("\n  Top-5 Gewinner-Solver:")
        for solver, wins in winners:
            pct = f"{100*wins/with_winner:.1f}%" if with_winner else ""
            print(f"    {solver:<30} {wins:>5} Gewinne  {pct:>6}")

    # Score-Gap (unser bester vs. Winner)
    row = await sess.execute(
        text(
            """
            SELECT
                AVG(sw.score::numeric) AS avg_winner_score,
                AVG(best.our_score::numeric) AS avg_our_best,
                COUNT(*) AS n
            FROM shadow_winners sw
            JOIN shadow_auctions sa ON sa.auction_id = sw.auction_id
            JOIN LATERAL (
                SELECT MAX(our_score_wei) AS our_score
                FROM shadow_solutions
                WHERE auction_id = sw.auction_id
                  AND our_score_wei > 0
            ) AS best ON TRUE
            WHERE sa.polled_at > :since
              AND sw.score IS NOT NULL
              AND best.our_score IS NOT NULL
            """
        ),
        {"since": since},
    )
    gap_row = row.one()
    if gap_row.n and gap_row.n > 0:
        avg_winner = float(gap_row.avg_winner_score or 0) / 1e18
        avg_ours = float(gap_row.avg_our_best or 0) / 1e18
        ratio = avg_ours / avg_winner if avg_winner > 0 else 0
        print(f"\n  Score-Gap (n={gap_row.n} Auktionen mit beiden Scores):")
        print(f"    Ø Winner-Score:   {avg_winner:.8f} ETH")
        print(f"    Ø Unser Score:    {avg_ours:.8f} ETH")
        print(f"    Verhältnis:       {ratio:.2%}")
        if ratio < 0.01:
            print("    ⚠ Unser Score ist < 1% des Winners — Phantom-Score-Problem?")
    else:
        print("\n  Kein Score-Vergleich möglich (keine überlappenden Daten)")


# ── Schicht 3a: Phantom-Score-Realitätscheck ──────────────────────────────


async def _check_phantom_score(sess, since) -> None:
    """Vergleicht unser_score_wei (an UNSEREN prices) vs.
    score_vs_winner_prices_wei (gleiche trades, aber an WINNER's prices).

    Das isoliert "Phantom-Score-Bug": wenn ratio our/winner_prices >> 1,
    waren unsere clearing prices oracle-inflated, nicht ausführbar.

    Echter hypothetischer Win: score_vs_winner_prices_wei > winner_score
    (apples-to-apples, beide an winner-prices bewertet).
    """
    print(f"\n{SEP}")
    print("SCHICHT 3a: PHANTOM-SCORE-REALITÄTSCHECK")
    print(SEP)

    # Coverage: wie viele Rows haben den re-evaluierten Score gefüllt?
    row = await sess.execute(
        text(
            """
            SELECT
                COUNT(*) AS total_scored,
                COUNT(*) FILTER (WHERE score_vs_winner_prices_wei IS NOT NULL)
                    AS with_reeval,
                COUNT(*) FILTER (WHERE score_vs_winner_prices_wei = 0)
                    AS zeroed_at_winner_prices
            FROM shadow_solutions ss
            JOIN shadow_auctions sa ON sa.auction_id = ss.auction_id
            WHERE sa.polled_at > :since
              AND ss.our_score_wei > 0
            """
        ),
        {"since": since},
    )
    total_scored, with_reeval, zeroed = row.one()
    print(f"  Rows mit our_score > 0:       {total_scored or 0:>6}")
    print(f"  Davon mit re-eval Score:      {with_reeval or 0:>6}  "
          f"({100*(with_reeval or 0)/(total_scored or 1):.1f}%)")

    if not total_scored:
        print("  Keine Scores im Fenster.")
        return

    if not with_reeval:
        print("  ⚠ KEINE score_vs_winner_prices_wei gefüllt!")
        print("    Backfill nötig: docker exec cow-solver python -m scripts.backfill_winner_price_scores")
        return

    print(f"  Zero an Winner-Prices:        {zeroed or 0:>6}  "
          f"(unsere trades nicht ausführbar gewesen)")

    # Pro Strategie: Inflation-Faktor our/winner_prices
    rows = await sess.execute(
        text(
            """
            SELECT
                ss.strategy,
                COUNT(*) AS n,
                COUNT(*) FILTER (WHERE ss.score_vs_winner_prices_wei = 0) AS n_zeroed,
                PERCENTILE_CONT(0.5) WITHIN GROUP (
                    ORDER BY ss.our_score_wei::numeric / NULLIF(ss.score_vs_winner_prices_wei::numeric, 0)
                ) AS median_inflation,
                PERCENTILE_CONT(0.95) WITHIN GROUP (
                    ORDER BY ss.our_score_wei::numeric / NULLIF(ss.score_vs_winner_prices_wei::numeric, 0)
                ) AS p95_inflation
            FROM shadow_solutions ss
            JOIN shadow_auctions sa ON sa.auction_id = ss.auction_id
            WHERE sa.polled_at > :since
              AND ss.our_score_wei > 0
              AND ss.score_vs_winner_prices_wei IS NOT NULL
              AND ss.score_vs_winner_prices_wei > 0
            GROUP BY ss.strategy
            ORDER BY n DESC
            """
        ),
        {"since": since},
    )
    print()
    print(f"  {'Strategie':<30} {'n':>5} {'zeroed':>7} {'medInflation':>13} {'p95Inflation':>13}")
    print("  " + "-" * 75)
    for strategy, n, n_zeroed, median_i, p95_i in rows:
        med = f"{float(median_i or 0):.2f}×"
        p95 = f"{float(p95_i or 0):.2f}×"
        zfrac = f"{100*(n_zeroed or 0)/n:.0f}%" if n else "n/a"
        flag = ""
        if median_i and float(median_i) > 1.5:
            flag = "  ⚠ inflated"
        elif median_i and float(median_i) > 1.1:
            flag = "  ~ leicht über"
        print(f"  {strategy:<30} {n:>5} {zfrac:>7} {med:>13} {p95:>13}{flag}")

    # Ehrliche Wins: re-evaluiert vs winner_score
    row = await sess.execute(
        text(
            """
            SELECT
                ss.strategy,
                COUNT(*) FILTER (WHERE ss.our_score_wei > sw.score) AS naive_wins,
                COUNT(*) FILTER (WHERE ss.score_vs_winner_prices_wei > sw.score)
                    AS honest_wins,
                COUNT(*) AS n_compared
            FROM shadow_solutions ss
            JOIN shadow_auctions sa ON sa.auction_id = ss.auction_id
            JOIN shadow_winners sw ON sw.auction_id = ss.auction_id
            WHERE sa.polled_at > :since
              AND ss.our_score_wei > 0
              AND ss.score_vs_winner_prices_wei IS NOT NULL
              AND sw.score IS NOT NULL
              AND sw.score > 0
            GROUP BY ss.strategy
            ORDER BY n_compared DESC
            """
        ),
        {"since": since},
    )
    print()
    print("  Ehrliche hypothetische Wins (score_vs_winner_prices > winner_score):")
    print(f"  {'Strategie':<30} {'verglichen':>11} {'naive_wins':>11} {'ehrlich':>9} {'Verlust':>9}")
    print("  " + "-" * 75)
    for strategy, naive_wins, honest_wins, n_compared in rows:
        nw = naive_wins or 0
        hw = honest_wins or 0
        nc = n_compared or 0
        verlust_pct = f"{100*(nw-hw)/nw:.0f}%" if nw else "n/a"
        flag = ""
        if nw and (nw - hw) / nw > 0.5:
            flag = "  ⚠ Phantom!"
        elif nw and (nw - hw) / nw > 0.2:
            flag = "  ~ etwas Phantom"
        print(f"  {strategy:<30} {nc:>11} {nw:>11} {hw:>9} {verlust_pct:>9}{flag}")


# ── Schicht 3b: Comp-Sync ─────────────────────────────────────────────────


async def _check_comp_sync(sess, since) -> None:
    print(f"\n{SEP}")
    print("SCHICHT 3b: Competitor-Sync (comp-sync)")
    print(SEP)

    row = await sess.execute(
        text(
            """
            SELECT COUNT(*) AS rows_24h, MAX(polled_at) AS last_sync
            FROM shadow_competitors
            WHERE polled_at > :since
            """
        ),
        {"since": since},
    )
    rows_24h, last_sync = row.one()
    print(f"  Competitor-Zeilen (24h): {rows_24h or 0:>6}")

    if last_sync:
        lag = (datetime.now(UTC) - last_sync.replace(tzinfo=UTC)).total_seconds()
        print(f"  Letzter Sync:            {last_sync.strftime('%H:%M:%S UTC')}")
        print(f"  Lag:                     {lag/60:.1f} min")
        if lag > 1800:
            print("  ⚠ Sync > 30 min — cow-solver-comp-sync hängt?")
            print("    Prüfe: docker logs cow-solver-comp-sync --tail 30")
        else:
            print("  ✓ comp-sync aktuell")
    else:
        print("  ⚠ KEINE Competitor-Daten — comp-sync schreibt nicht!")


# ── Schicht 3c: Ghost-Orders ──────────────────────────────────────────────


async def _check_ghost_orders(sess) -> None:
    print(f"\n{SEP}")
    print("SCHICHT 3c: Ghost-Order-Detektion")
    print(SEP)

    row = await sess.execute(
        text(
            """
            SELECT COUNT(*) AS total,
                   MAX(last_refreshed_at) AS last_refresh
            FROM ghost_orders
            """
        )
    )
    total, last_refresh = row.one()
    print(f"  Ghost-Orders in DB:    {total or 0:>6}")

    if last_refresh:
        lag = (datetime.now(UTC) - last_refresh.replace(tzinfo=UTC)).total_seconds()
        print(f"  Letzter Refresh:       {last_refresh.strftime('%H:%M:%S UTC')}")
        print(f"  Lag:                   {lag/60:.1f} min")
        if lag > 3600:
            print("  ⚠ Refresh > 60 min — ghost-refresh Container hängt?")
        else:
            print("  ✓ Ghost-Detektor läuft")
    else:
        print("  ⚠ Noch kein Ghost-Refresh gelaufen")


# ── Schicht 4: Stichproben ────────────────────────────────────────────────


async def _stichproben(sess, since, n: int) -> None:
    print(f"\n{SEP}")
    print(f"SCHICHT 4: Stichproben ({n} zufällige Auktionen)")
    print(SEP)

    rows = await sess.execute(
        text(
            """
            SELECT sa.auction_id, sa.n_orders, sa.polled_at
            FROM shadow_auctions sa
            WHERE sa.polled_at > :since
            ORDER BY RANDOM()
            LIMIT :n
            """
        ),
        {"since": since, "n": n},
    )
    auctions = rows.fetchall()

    if not auctions:
        print("  Keine Daten für Stichproben.")
        return

    for auction_id, n_orders, polled_at in auctions:
        print(f"\n  Auktion {auction_id}  |  {polled_at.strftime('%d.%m %H:%M')}  |  {n_orders} Aufträge")

        # Unsere Lösungsversuche
        sol_rows = await sess.execute(
            text(
                """
                SELECT strategy, status, our_score_wei, latency_ms, error
                FROM shadow_solutions
                WHERE auction_id = :aid
                ORDER BY created_at
                """
            ),
            {"aid": auction_id},
        )
        solutions = sol_rows.fetchall()
        if solutions:
            for strategy, status, score, latency, error in solutions:
                score_eth = f"{float(score)/1e18:.8f} ETH" if score and float(score) > 0 else "kein Score"
                lat = f"{latency}ms" if latency else "n/a"
                err = f" ← {error[:60]}" if error else ""
                print(f"    [{strategy:<28}] {status:<12} {score_eth:>22}  {lat:>7}{err}")
        else:
            print("    [keine shadow_solutions für diese Auktion]")

        # Winner
        w_row = await sess.execute(
            text(
                "SELECT winner_solver, score FROM shadow_winners WHERE auction_id = :aid"
            ),
            {"aid": auction_id},
        )
        winner = w_row.one_or_none()
        if winner:
            w_score = f"{float(winner.score)/1e18:.8f} ETH" if winner.score else "n/a"
            print(f"    [WINNER: {winner.winner_solver:<24}]              {w_score:>22}")
        else:
            print("    [kein Winner bekannt — comp-sync noch nicht gelaufen]")

    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="24h Daten-Verifikation")
    parser.add_argument("--hours", type=int, default=24, help="Zeitfenster in Stunden (default: 24)")
    parser.add_argument("--sample", type=int, default=5, help="Anzahl Stichproben-Auktionen (default: 5)")
    args = parser.parse_args()

    asyncio.run(run(hours=args.hours, sample_n=args.sample))


if __name__ == "__main__":
    main()
