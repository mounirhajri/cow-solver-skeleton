"""On-chain feasibility-verdict analysis for shadow solutions.

Read the ``feasible`` / ``revert_reason`` columns populated going-forward by the
persist-time validator (re-encode ``GPv2Settlement.settle()`` → ``eth_call`` at
``latest``).  Two views, printed to stdout:

View A — Feasibility distribution per strategy
    Among solutions *eligible* for validation (non-naive, solved, scored), how
    many came back feasible=True (settle-able), False (phantom revert) or NULL
    (UNKNOWN — validation infra gap: order fetch / encode / RPC failure /
    rate-limit exhaustion).  Two ratios matter:

      check-rate = (True+False) / eligible   → how often we could even decide.
                   A low check-rate means infra is blind, NOT that all is well.
      feasible%  = True / (True+False)        → of the decidable ones, how many
                   actually settle.  This is the phantom-score truth signal.

View B — Top revert reasons
    For feasible=False rows, the most common ``revert_reason`` prefixes.  These
    are genuine on-chain reverts (token-conservation violation, bad signature,
    AMM-interaction revert).  CAVEAT: validation runs at ``latest``, so for
    AMM-heavy strategies a reason may reflect pool-state drift between the
    auction block and now rather than a true phantom — read those with care.

Usage
-----
    python -m scripts.analyze_feasibility [--days 7] [--strategy STRATEGY]

``--strategy`` restricts both views to a single strategy (e.g.
``cow-matching-bipartite``, ``router-v2``).  Without it, all non-naive
strategies are tallied separately in View A and pooled in View B.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.log import get_logger
from src.persistence.db import get_session_factory
from src.persistence.models import ShadowSolution

log = get_logger(__name__)

# A solution is "eligible" for a feasibility verdict iff the persist-time hook
# would have run it: a non-naive strategy that produced a scored solution.
# Restricting the denominator to eligible rows makes a NULL feasible value mean
# "infra couldn't decide" rather than "we never tried".


def _eligible(query: Select[Any]) -> Select[Any]:
    return (
        query.where(ShadowSolution.strategy != "naive")
        .where(ShadowSolution.solution.is_not(None))
        .where(ShadowSolution.our_score_wei.is_not(None))
    )


# ---------------------------------------------------------------------------
# View A: feasibility distribution per strategy
# ---------------------------------------------------------------------------


async def _view_feasibility_by_strategy(
    session: AsyncSession,
    since: datetime,
    strategy: str | None = None,
) -> None:
    q = _eligible(
        select(ShadowSolution.strategy, ShadowSolution.feasible).where(
            ShadowSolution.created_at >= since
        )
    )
    if strategy is not None:
        q = q.where(ShadowSolution.strategy == strategy)
    rows = (await session.execute(q)).all()

    print(f"\n{'='*72}")
    print("View A — Feasibility verdict per strategy")
    print("         (eligible = non-naive, solved, scored)")
    print(f"{'='*72}")

    if not rows:
        print("  No eligible solutions in window — nothing validated yet.")
        return

    # strategy → {"feasible": n, "phantom": n, "unknown": n}
    tally: dict[str, dict[str, int]] = defaultdict(
        lambda: {"feasible": 0, "phantom": 0, "unknown": 0}
    )
    for strat, feasible in rows:
        if feasible is None:
            tally[strat]["unknown"] += 1
        elif feasible:
            tally[strat]["feasible"] += 1
        else:
            tally[strat]["phantom"] += 1

    header = (
        f"  {'strategy':<28} {'eligible':>8} {'feas':>7} {'phantom':>8} "
        f"{'unknown':>8} {'check%':>7} {'feas%':>7}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    totals = {"feasible": 0, "phantom": 0, "unknown": 0}
    for strat in sorted(tally):
        t = tally[strat]
        eligible = t["feasible"] + t["phantom"] + t["unknown"]
        decided = t["feasible"] + t["phantom"]
        check_pct = 100 * decided / eligible if eligible else 0.0
        feas_pct = 100 * t["feasible"] / decided if decided else float("nan")
        print(
            f"  {strat:<28} {eligible:>8} {t['feasible']:>7} {t['phantom']:>8} "
            f"{t['unknown']:>8} {check_pct:>6.1f}% {feas_pct:>6.1f}%"
        )
        for k in totals:
            totals[k] += t[k]

    grand_eligible = sum(totals.values())
    grand_decided = totals["feasible"] + totals["phantom"]
    grand_check = 100 * grand_decided / grand_eligible if grand_eligible else 0.0
    grand_feas = 100 * totals["feasible"] / grand_decided if grand_decided else float("nan")
    print("  " + "-" * (len(header) - 2))
    print(
        f"  {'ALL':<28} {grand_eligible:>8} {totals['feasible']:>7} "
        f"{totals['phantom']:>8} {totals['unknown']:>8} "
        f"{grand_check:>6.1f}% {grand_feas:>6.1f}%"
    )
    print(
        "\n  check% = decided / eligible (infra health);  "
        "feas% = feasible / decided (phantom-score truth)."
    )


# ---------------------------------------------------------------------------
# View B: top revert reasons
# ---------------------------------------------------------------------------


async def _view_revert_reasons(
    session: AsyncSession,
    since: datetime,
    strategy: str | None = None,
    top_n: int = 15,
    prefix_len: int = 80,
) -> None:
    q = (
        select(ShadowSolution.revert_reason)
        .where(ShadowSolution.created_at >= since)
        .where(ShadowSolution.feasible.is_(False))
    )
    if strategy is not None:
        q = q.where(ShadowSolution.strategy == strategy)
    rows = (await session.execute(q)).all()

    print(f"\n{'='*72}")
    print("View B — Top revert reasons (feasible=False → genuine phantom)")
    print(f"{'='*72}")

    if not rows:
        print("  No phantom (feasible=False) solutions in window.")
        return

    # Collapse to a stable prefix so near-identical reverts group together.
    counter: Counter[str] = Counter()
    for (reason,) in rows:
        key = (reason or "(no reason recorded)").strip()[:prefix_len]
        counter[key] += 1

    total = sum(counter.values())
    print(f"  {total} phantom solution(s); top {min(top_n, len(counter))} reasons:\n")
    for reason, n in counter.most_common(top_n):
        pct = 100 * n / total if total else 0.0
        print(f"  {n:>5}  ({pct:5.1f}%)  {reason}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def run_analysis(
    days: int,
    strategy: str | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    hours: int | None = None,
) -> None:
    factory = session_factory or get_session_factory()
    # --hours overrides --days for tight post-deploy windows (e.g. confirming a
    # config change took effect without a day of stale rows drowning it out).
    if hours is not None:
        window = timedelta(hours=hours)
        label = f"last {hours} hours"
    else:
        window = timedelta(days=days)
        label = f"last {days} days"
    since = datetime.now(UTC) - window
    scope = f" [strategy={strategy}]" if strategy else ""
    print(
        f"\nFeasibility Verdict Analysis  "
        f"({label}, since {since:%Y-%m-%d %H:%M} UTC){scope}"
    )

    async with factory() as session:
        await _view_feasibility_by_strategy(session, since, strategy=strategy)

    async with factory() as session:
        await _view_revert_reasons(session, since, strategy=strategy)

    print()


async def main_async(
    days: int, strategy: str | None = None, hours: int | None = None
) -> None:
    try:
        await run_analysis(days=days, strategy=strategy, hours=hours)
    except Exception as exc:
        log.error(
            "analyze_feasibility_unhandled",
            error=str(exc),
            error_type=type(exc).__name__,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyse on-chain feasibility verdicts of shadow solutions."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Analysis window in days (default: 7).",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=None,
        help="Analysis window in hours; overrides --days when set.",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        default=None,
        help=(
            "Restrict both views to a single strategy "
            "(e.g. cow-matching-bipartite, router-v2)."
        ),
    )
    args = parser.parse_args()
    asyncio.run(main_async(days=args.days, strategy=args.strategy, hours=args.hours))


if __name__ == "__main__":
    main()
