"""Project net monthly economics from shadow data.

Phase 4a / G6 break-even gate. Queries hypothetical wins per revenue
strategy in the window, projects to monthly EUR using current ETH/COW
prices, applies bonding-pool fee, subtracts server cost. Prints PASS/FAIL
on the G6 gate from docs/superpowers/specs/2026-05-23-solver-revenue-strategy-design.md §6.

Assumptions baked in (override via flags):
- Arbitrum minimum performance reward per won batch: 0.00024 ETH (docs.cow.fi)
- CIP-48 bonding-pool service fee: 15 % on COW-denominated rewards
- Solver surplus (kept by us per win): the row's our_score_wei

Usage:
    python -m scripts.estimate_economics [--days 7]
        [--hours 6]
        [--eth-price-eur 3000] [--cow-price-eur 0.15]
        [--server-cost-eur 60] [--bonding-fee-pct 15]
        [--win-rate-confidence 0.2]

    --hours überschreibt --days wenn gesetzt (z. B. --hours 6 = 0.25 Tage).
    Wichtig für frische Daten nach Config-Switches: erst 6-8 h nach einem
    Tuning-Change ergibt --days 1 eine homogene Datenbasis.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from src.persistence.db import get_session_factory
from src.persistence.models import ShadowAuction, ShadowSolution, ShadowWinner

# docs.cow.fi/cow-protocol/tutorials/solvers/onboard — Arbitrum min reward per won batch.
# Floor that applies regardless of competition margin.
_ARBITRUM_MIN_REWARD_ETH = 0.00024

# Strategies that produce revenue when they win. naive is excluded — its scores
# are oracle-price-inflated and would not survive on-chain execution.
_REVENUE_STRATEGIES = (
    "cow-matching-bipartite",
    "cow-matching-multi-party",
    "router-v2",
)

# Strategies whose wins are subject to UID-set dedup.  A long-lived limit
# order (e.g. a TWAP, a 22 %-slack partial-fillable BUY, or a slow-fill SELL)
# produces the SAME trade-UID set across consecutive auctions until it's
# fully settled.  Without dedup the raw count overstates achievable
# settlements by 10–400×.
#
# Initially only multi-party was deduped on the grounds that "1- and 2-trade
# rings rarely repeat" (old comment).  That was wrong: live 2026-05-24 data
# showed router-v2 hitting the SAME 1-WBTC BUY in 134 of 24 h auctions
# (10–15 distinct UIDs, dozens of repeats each).  Bipartite has the same
# exposure when both sides of a pair are persistent (e.g. two TWAPs).
#
# Treatment is uniform via _ring_signature: dedup by sorted-uid hash, take
# the median score per unique signature.  Single-uid 'rings' (router-v2)
# collapse cleanly because the sig is just the one uid.
_DEDUP_STRATEGIES = frozenset({
    "cow-matching-multi-party",
    "cow-matching-bipartite",
    "router-v2",
})

_DAYS_PER_MONTH = 30.44

# Rows persisted before this cutoff used a buggy clearing-price convention in
# RouterSolver (oracle reference_price instead of AMM execution rate), which
# overstated CIP-14 surplus systematically — median 6.6 ETH per fill at the
# height of the contamination. Fix landed in PR #26 (merged 2026-05-24 20:34
# UTC, deploy completed 20:37 UTC). Cutoff is set 3 minutes later as a safety
# margin so any in-flight auctions caught mid-deploy are excluded. Rows from
# router-v2 before this timestamp are dropped to stop the estimator from
# republishing phantom numbers from historical data. Bipartite/multi-party
# rows were unaffected by the bug (they don't use _register_prices).
_ROUTER_PRICE_BUG_CUTOFF = datetime(2026, 5, 24, 20, 40, tzinfo=UTC)


@dataclass(frozen=True)
class Projection:
    window_days: float
    wins_total: int
    wins_per_strategy: dict[str, int]
    wins_per_month_point: float
    wins_per_month_low: float
    wins_per_month_high: float
    # `median_surplus_eth` drives projections (robust to outliers).  `mean_*`
    # is shown alongside for transparency so a hidden inflation isn't hiding.
    median_surplus_eth: float
    mean_surplus_eth: float
    reward_eur_month: float
    surplus_eur_month: float
    reward_after_fee_eur: float
    server_cost_eur: float
    net_eur_month_point: float
    net_eur_month_low: float
    net_eur_month_high: float


def _ring_signature(solution: dict | None) -> str | None:
    """Stable hash of a ring's order-UID set.

    Same orders in any order → same sig; missing trades → None.  Falls back
    cleanly on malformed solutions so a single bad row doesn't poison the
    whole projection.
    """
    if not isinstance(solution, dict):
        return None
    trades = solution.get("trades") or []
    uids = sorted(
        t.get("orderUid") for t in trades if isinstance(t, dict) and t.get("orderUid")
    )
    if not uids:
        return None
    return hashlib.md5(",".join(uids).encode("utf-8")).hexdigest()


def _dedupe_by_ring(
    rows: list[tuple[int, str | None]],
) -> list[int]:
    """Collapse rows with the same ring signature into one representative.

    Each unique signature contributes ONE win whose surplus is the median
    surplus observed for that ring across the window.  Rows with sig=None
    (no extractable trades) pass through untouched — better to leave them
    than silently drop revenue.
    """
    by_sig: dict[str, list[int]] = defaultdict(list)
    out: list[int] = []
    for score, sig in rows:
        if sig is None:
            out.append(score)
        else:
            by_sig[sig].append(score)
    for scores in by_sig.values():
        out.append(int(statistics.median(scores)))
    return out


def _cap_outliers(values: list[int], percentile: float) -> list[int]:
    """Clip each value at the (window-local) percentile.

    Replaces a tail of inflated rows (e.g. an old code-path bug that scored
    a route at 50× the typical value) with the percentile cap, rather than
    deleting them — keeps the win count honest while neutralising the inflation.

    Uses a hard "nearest-rank" percentile rather than statistics.quantiles
    (which linearly interpolates between samples and would not hard-clip a
    lone outlier — the cap drifts toward the outlier value).

    No-op when percentile >= 100 or sample size < 10.
    """
    if percentile >= 100.0 or len(values) < 10:
        return values
    sorted_vals = sorted(values)
    # Nearest-rank: cap = sorted[ceil(N * p/100) - 1], 1-indexed.
    # For N=100, p=99: cap = sorted[98] = second-largest. A single outlier
    # at position 100 gets clipped down to that.
    n = len(sorted_vals)
    rank = max(1, -(-int(n * percentile) // 100))  # ceil(n*p/100), 1-indexed
    cap = sorted_vals[rank - 1]
    return [min(v, cap) for v in values]


def project_monthly(
    wins_by_strategy: dict[str, list[int]],
    window_days: float,
    eth_price_eur: float,
    server_cost_eur: float,
    bonding_fee_pct: float,
    win_rate_confidence: float,
) -> Projection:
    """Pure math — separated so it is unit-testable without a DB.

    Uses MEDIAN surplus per win, not mean.  Median is robust against the
    kind of outliers shadow data is prone to: an old-code bug logging an
    impossible-to-execute 0.498 ETH route inflates the mean by ~70× while
    barely moving the median.  Mean is still reported alongside.
    """
    months_per_window = window_days / _DAYS_PER_MONTH

    all_surplus_wei = [s for wins in wins_by_strategy.values() for s in wins]
    total_wins = len(all_surplus_wei)
    if all_surplus_wei:
        median_surplus_eth = statistics.median(all_surplus_wei) / 1e18
        mean_surplus_eth = statistics.mean(all_surplus_wei) / 1e18
    else:
        median_surplus_eth = 0.0
        mean_surplus_eth = 0.0

    wins_per_month = total_wins / months_per_window if months_per_window > 0 else 0.0
    wins_low = wins_per_month * (1 - win_rate_confidence)
    wins_high = wins_per_month * (1 + win_rate_confidence)

    def _net_at(wins_pm: float) -> tuple[float, float, float]:
        reward_eth = wins_pm * _ARBITRUM_MIN_REWARD_ETH
        surplus_eth = wins_pm * median_surplus_eth
        reward_eur = reward_eth * eth_price_eur
        surplus_eur = surplus_eth * eth_price_eur
        # CIP-48 service fee applies to COW-denominated performance rewards.
        # Solver surplus stays with us un-feed (it's collected via execution,
        # not via the protocol payout pipeline).
        reward_after_fee = reward_eur * (1 - bonding_fee_pct / 100)
        net = reward_after_fee + surplus_eur - server_cost_eur
        return reward_eur, surplus_eur, net

    reward_eur, surplus_eur, net_point = _net_at(wins_per_month)
    _, _, net_low = _net_at(wins_low)
    _, _, net_high = _net_at(wins_high)
    reward_after_fee = reward_eur * (1 - bonding_fee_pct / 100)

    return Projection(
        window_days=window_days,
        wins_total=total_wins,
        wins_per_strategy={k: len(v) for k, v in wins_by_strategy.items()},
        wins_per_month_point=wins_per_month,
        wins_per_month_low=wins_low,
        wins_per_month_high=wins_high,
        median_surplus_eth=median_surplus_eth,
        mean_surplus_eth=mean_surplus_eth,
        reward_eur_month=reward_eur,
        surplus_eur_month=surplus_eur,
        reward_after_fee_eur=reward_after_fee,
        server_cost_eur=server_cost_eur,
        net_eur_month_point=net_point,
        net_eur_month_low=net_low,
        net_eur_month_high=net_high,
    )


async def collect_wins_per_strategy(
    days: float,
    *,
    dedup_rings: bool = True,
    outlier_cap_percentile: float = 99.0,
) -> dict[str, list[int]]:
    """Per-strategy list of our_score_wei for auctions we'd have won.

    Two debiasing steps applied before returning:

    1.  ``dedup_rings=True`` (default):  for every strategy in
        ``_DEDUP_STRATEGIES`` (multi-party, bipartite, router-v2), rows
        with the same sorted-UID signature collapse to a single
        representative whose surplus is the median of the group.  Without
        this, a long-lived order produces the same trade-UID set in every
        auction it appears in → raw counts overstate achievable settlements
        by 10–400×.  Single-trade strategies (router-v2) dedup by single uid.
    2.  ``outlier_cap_percentile=99`` (default):  values above the p99 of
        each per-strategy distribution are clipped to that cap.  Neutralises
        old-code-bug rows (e.g. pre-PR-#26 router-v2 at 6 ETH where realistic
        margin was milliETH) without dropping the win itself.

    Set ``dedup_rings=False`` or ``outlier_cap_percentile=100`` to disable
    each step independently; useful when validating raw shadow numbers.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    Session = get_session_factory()

    async with Session() as session:
        q = await session.execute(
            select(
                ShadowSolution.strategy,
                ShadowSolution.our_score_wei,
                ShadowSolution.solution,
                ShadowSolution.created_at,
                ShadowWinner.score.label("winner_score"),
            )
            .join(ShadowAuction, ShadowAuction.auction_id == ShadowSolution.auction_id)
            .outerjoin(ShadowWinner, ShadowWinner.auction_id == ShadowSolution.auction_id)
            .where(ShadowAuction.polled_at >= since)
            .where(ShadowSolution.strategy.in_(_REVENUE_STRATEGIES))
            .where(ShadowSolution.status == "solved")
            .where(ShadowSolution.our_score_wei.is_not(None))
            .where(ShadowWinner.score.is_not(None))
        )
        rows = q.all()

    # Bucket (score, sig) per strategy first — sig only needed for dedup
    # but cheap to compute up-front and lets _dedupe_by_ring stay pure.
    n_contaminated = 0
    wins_with_sigs: dict[str, list[tuple[int, str | None]]] = defaultdict(list)
    for r in rows:
        if (
            r.strategy == "router-v2"
            and r.created_at is not None
            and r.created_at < _ROUTER_PRICE_BUG_CUTOFF
        ):
            n_contaminated += 1
            continue
        if int(r.our_score_wei) > int(r.winner_score):
            sig = _ring_signature(r.solution) if r.strategy in _DEDUP_STRATEGIES else None
            wins_with_sigs[r.strategy].append((int(r.our_score_wei), sig))

    if n_contaminated:
        print(
            f"  [data-quality] dropped {n_contaminated} router-v2 row(s) "
            f"persisted before {_ROUTER_PRICE_BUG_CUTOFF.isoformat()} "
            f"(phantom clearing-price era — see scripts/estimate_economics.py)"
        )

    out: dict[str, list[int]] = {}
    for strategy, paired in wins_with_sigs.items():
        if dedup_rings and strategy in _DEDUP_STRATEGIES:
            raw_n = len(paired)
            values = _dedupe_by_ring(paired)
            if len(values) < raw_n:
                print(
                    f"  [dedup] {strategy}: {raw_n} raw rows → {len(values)} "
                    f"distinct UID-sets ({raw_n - len(values)} repeats collapsed)"
                )
        else:
            values = [s for s, _ in paired]
        values = _cap_outliers(values, outlier_cap_percentile)
        out[strategy] = values
    return out


def print_report(p: Projection, eth_price_eur: float, bonding_fee_pct: float) -> None:
    bar = "=" * 60
    print(f"\n{bar}")
    # "1 day(s)" für ganze Tage, "6h" für Sub-Tag-Fenster — sonst "0.25 day(s)"
    # was niemand lesen will.
    if p.window_days < 1:
        window_str = f"{p.window_days * 24:g}h"
    else:
        window_str = f"{p.window_days:g} day(s)"
    print(f"Economics Projection — last {window_str}")
    print(bar)
    print(f"Hypothetical wins observed: {p.wins_total} (after dedup + outlier-cap)")
    for strat, n in sorted(p.wins_per_strategy.items()):
        print(f"  {strat:30s} {n:4d}")
    print(f"\nSurplus per win (drives projection): median {p.median_surplus_eth:+.6f} ETH")
    print(f"                                     mean   {p.mean_surplus_eth:+.6f} ETH")
    if p.mean_surplus_eth > 2 * max(p.median_surplus_eth, 1e-9):
        # >2× spread is the canary for a stray outlier still hiding in the tail.
        print("  (mean ≫ median → outlier tail; check raw distribution before trusting)")
    print(
        f"Projected wins / month:        {p.wins_per_month_point:5.1f} "
        f"(±{(p.wins_per_month_high - p.wins_per_month_point):4.1f})"
    )

    print(f"\nMonthly revenue projection at ETH=€{eth_price_eur:.0f}:")
    print(f"  Performance reward (gross):  €{p.reward_eur_month:7.2f}")
    print(f"  Solver surplus (gross):      €{p.surplus_eur_month:7.2f}")
    print(
        f"  Performance reward after {bonding_fee_pct:.0f}% bonding fee: "
        f"€{p.reward_after_fee_eur:7.2f}"
    )
    print(f"  Server + RPC cost:           €{-p.server_cost_eur:7.2f}")
    print(f"  {'─' * 50}")
    print(f"  Net monthly (point):         €{p.net_eur_month_point:+7.2f}")
    print(
        f"  Net monthly (low / high):    "
        f"€{p.net_eur_month_low:+7.2f} / €{p.net_eur_month_high:+7.2f}"
    )

    print(f"\n{bar}")
    print("G6 BREAK-EVEN GATE")
    print(bar)
    pass_point = p.net_eur_month_point >= 0
    pass_band = p.net_eur_month_low >= -20.0
    verdict = "PASS" if (pass_point and pass_band) else "FAIL"
    print(f"  Net point ≥ €0:                {pass_point}  ({p.net_eur_month_point:+.2f})")
    print(f"  Net low-band ≥ -€20:           {pass_band}  ({p.net_eur_month_low:+.2f})")
    print(f"\n  Verdict: G6 {verdict}")
    if verdict == "FAIL":
        print("\n  Implication: do NOT proceed to KYC. Identify which lever can")
        print("  move us above break-even (volume via Phase 3 / margin via long-tail")
        print("  pool indexer / quote rewards) before re-running the gate.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=float, default=7.0,
                        help="Zeitfenster in Tagen (default: 7). Float zulässig.")
    parser.add_argument("--hours", type=float, default=None,
                        help="Zeitfenster in Stunden — überschreibt --days wenn gesetzt.")
    parser.add_argument("--eth-price-eur", type=float, default=3000.0)
    parser.add_argument("--cow-price-eur", type=float, default=0.15)
    parser.add_argument("--server-cost-eur", type=float, default=60.0)
    parser.add_argument("--bonding-fee-pct", type=float, default=15.0)
    parser.add_argument("--win-rate-confidence", type=float, default=0.2)
    parser.add_argument(
        "--no-dedup-rings", action="store_true",
        help="Disable multi-party ring-signature dedup (shows raw inflated wins).",
    )
    parser.add_argument(
        "--outlier-cap-percentile", type=float, default=99.0,
        help="Clip surplus values above this percentile per strategy (default 99). "
             "Set to 100 to disable.",
    )
    args = parser.parse_args()

    window_days = args.hours / 24 if args.hours is not None else args.days

    wins = asyncio.run(collect_wins_per_strategy(
        days=window_days,
        dedup_rings=not args.no_dedup_rings,
        outlier_cap_percentile=args.outlier_cap_percentile,
    ))
    projection = project_monthly(
        wins_by_strategy=wins,
        window_days=window_days,
        eth_price_eur=args.eth_price_eur,
        server_cost_eur=args.server_cost_eur,
        bonding_fee_pct=args.bonding_fee_pct,
        win_rate_confidence=args.win_rate_confidence,
    )
    print_report(projection, args.eth_price_eur, args.bonding_fee_pct)


if __name__ == "__main__":
    main()
