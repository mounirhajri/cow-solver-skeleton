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
        [--eth-price-eur 3000] [--cow-price-eur 0.15]
        [--server-cost-eur 60] [--bonding-fee-pct 15]
        [--win-rate-confidence 0.2]
"""

from __future__ import annotations

import argparse
import asyncio
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

_DAYS_PER_MONTH = 30.44


@dataclass(frozen=True)
class Projection:
    window_days: int
    wins_total: int
    wins_per_strategy: dict[str, int]
    wins_per_month_point: float
    wins_per_month_low: float
    wins_per_month_high: float
    avg_surplus_eth: float
    reward_eur_month: float
    surplus_eur_month: float
    reward_after_fee_eur: float
    server_cost_eur: float
    net_eur_month_point: float
    net_eur_month_low: float
    net_eur_month_high: float


def project_monthly(
    wins_by_strategy: dict[str, list[int]],
    window_days: int,
    eth_price_eur: float,
    server_cost_eur: float,
    bonding_fee_pct: float,
    win_rate_confidence: float,
) -> Projection:
    """Pure math — separated so it is unit-testable without a DB."""
    months_per_window = window_days / _DAYS_PER_MONTH

    all_surplus_wei = [s for wins in wins_by_strategy.values() for s in wins]
    total_wins = len(all_surplus_wei)
    avg_surplus_eth = (
        (statistics.mean(all_surplus_wei) / 1e18) if all_surplus_wei else 0.0
    )

    wins_per_month = total_wins / months_per_window if months_per_window > 0 else 0.0
    wins_low = wins_per_month * (1 - win_rate_confidence)
    wins_high = wins_per_month * (1 + win_rate_confidence)

    def _net_at(wins_pm: float) -> tuple[float, float, float]:
        reward_eth = wins_pm * _ARBITRUM_MIN_REWARD_ETH
        surplus_eth = wins_pm * avg_surplus_eth
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
        avg_surplus_eth=avg_surplus_eth,
        reward_eur_month=reward_eur,
        surplus_eur_month=surplus_eur,
        reward_after_fee_eur=reward_after_fee,
        server_cost_eur=server_cost_eur,
        net_eur_month_point=net_point,
        net_eur_month_low=net_low,
        net_eur_month_high=net_high,
    )


async def collect_wins_per_strategy(days: int) -> dict[str, list[int]]:
    """Per-strategy list of our_score_wei for auctions we'd have won."""
    since = datetime.now(UTC) - timedelta(days=days)
    Session = get_session_factory()

    async with Session() as session:
        q = await session.execute(
            select(
                ShadowSolution.strategy,
                ShadowSolution.our_score_wei,
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

    wins: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        if int(r.our_score_wei) > int(r.winner_score):
            wins[r.strategy].append(int(r.our_score_wei))
    return dict(wins)


def print_report(p: Projection, eth_price_eur: float, bonding_fee_pct: float) -> None:
    bar = "=" * 60
    print(f"\n{bar}")
    print(f"Economics Projection — last {p.window_days} day(s)")
    print(bar)
    print(f"Hypothetical wins observed: {p.wins_total}")
    for strat, n in sorted(p.wins_per_strategy.items()):
        print(f"  {strat:30s} {n:4d}")
    print(f"\nAvg solver surplus per win:    {p.avg_surplus_eth:+.6f} ETH")
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
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--eth-price-eur", type=float, default=3000.0)
    parser.add_argument("--cow-price-eur", type=float, default=0.15)
    parser.add_argument("--server-cost-eur", type=float, default=60.0)
    parser.add_argument("--bonding-fee-pct", type=float, default=15.0)
    parser.add_argument("--win-rate-confidence", type=float, default=0.2)
    args = parser.parse_args()

    wins = asyncio.run(collect_wins_per_strategy(days=args.days))
    projection = project_monthly(
        wins_by_strategy=wins,
        window_days=args.days,
        eth_price_eur=args.eth_price_eur,
        server_cost_eur=args.server_cost_eur,
        bonding_fee_pct=args.bonding_fee_pct,
        win_rate_confidence=args.win_rate_confidence,
    )
    print_report(projection, args.eth_price_eur, args.bonding_fee_pct)


if __name__ == "__main__":
    main()
