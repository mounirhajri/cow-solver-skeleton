"""EBBO (External Best Bid/Offer) validator — pre-submission safety net.

CoW Protocol's settlement contract enforces that each user receives at least
the surplus they would have gotten from the best external venue.  When our
composed Solution claims a clearing price that gives the user less than
external Uniswap V3 routing would, the on-chain settlement may revert or
the driver may reject the solution outright.  Repeated EBBO violations
slash the solver bond.

This module checks **before** we submit:
    for every sell trade in the composed Solution, get a fresh V3 quote
    for the same swap at the same input size; if our effective output
    (executed_sell × clearing_price[sell] / clearing_price[buy]) is below
    the external quote minus ``tolerance_bps``, fail the validation and
    fall back to no-solution.

We intentionally check only **sell** trades here — buy trades require
``quoteExactOutput`` plumbing.  router-v2 already has it (PR #20), but
wiring it into EBBO is follow-up work; until then buy trades skip the
check (counted as ``n_skipped`` so we can monitor coverage).  Trades
whose tokens lack a V3 route (deep long-tail) are skipped — by definition
we cannot under-perform a non-existent external quote.

Tolerance default is 50 bps (0.5 %); this absorbs slippage between the
quote call and the on-chain settlement window.  CoW's documented EBBO
slack is tighter than this and the production check uses median ref
prices — we err generous in the validator to avoid rejecting solutions
that would actually settle, accepting more false-positives in the
trade-off.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.log import get_logger
from src.models.auction import Auction
from src.models.solution import Solution
from src.routing.multicall import Multicall3
from src.routing.multihop import quote_best_path
from src.shadow.scoring import _ceil_div

log = get_logger(__name__)


# Default tolerance: 50 bps = 0.5 %.  Tight enough to catch genuinely
# uncompetitive solutions, loose enough to absorb sub-second pool drift
# between our quote and the production settlement window.
DEFAULT_TOLERANCE_BPS = 50

# Cap on how many trades a single solution might contain.  Composer-level
# greed today never produces more than ~6 trades; this guard exists to
# bound the EBBO RPC budget in the pathological case of a misconfigured
# composer that emits dozens of trades.
_MAX_TRADES_PER_CHECK = 20


@dataclass(frozen=True)
class EBBOResult:
    passes: bool
    violations: list[str]
    n_checked: int
    n_skipped: int
    # Trades that exceeded _MAX_TRADES_PER_CHECK and were dropped from the
    # iteration entirely.  Distinct from n_skipped (which is "checked-and-
    # waived") so callers can monitor the truncation rate separately and
    # tune _MAX_TRADES_PER_CHECK if it starts firing.
    n_truncated: int = 0


async def validate_solution_ebbo(
    solution: Solution,
    auction: Auction,
    multicall: Multicall3,
    intermediates: list[str],
    tolerance_bps: int = DEFAULT_TOLERANCE_BPS,
) -> EBBOResult:
    """Verify every sell trade in ``solution`` beats external V3 quote.

    Returns ``EBBOResult`` with ``passes=False`` if ANY trade falls short
    by more than ``tolerance_bps`` of its external-quote output.  Trades
    that lack clearing prices, that reference unknown order UIDs, or that
    are buy-kind orders are skipped (not failed).
    """
    uid_to_order = {o.uid: o for o in auction.orders}
    violations: list[str] = []
    n_checked = 0
    n_skipped = 0

    trades = solution.trades[:_MAX_TRADES_PER_CHECK]
    n_truncated = max(0, len(solution.trades) - _MAX_TRADES_PER_CHECK)
    if n_truncated:
        log.warning(
            "ebbo_trade_count_truncated",
            auction_id=auction.id,
            total=len(solution.trades),
            checked=_MAX_TRADES_PER_CHECK,
            truncated=n_truncated,
        )

    for trade in trades:
        order = uid_to_order.get(trade.order_uid)
        if order is None:
            n_skipped += 1
            continue

        # Buy-side EBBO requires quoteExactOutput plumbing in this module;
        # router-v2 already has it but threading the same through
        # quote_best_path is a separate piece of work.  Skip buys for now
        # rather than block-list them — they're 8 % of order volume so
        # skipping costs little, and the EBBO check on sell trades covers
        # the production-blocking failure mode (multi-party ring prices
        # underwater vs. external V3).
        if order.kind != "sell":
            n_skipped += 1
            continue

        cp_sell = solution.prices.get(order.sell_token)
        cp_buy = solution.prices.get(order.buy_token)
        if cp_sell is None or cp_buy is None or cp_buy == 0:
            # Composed solution missing prices for this trade's tokens —
            # we shouldn't have emitted it, but skipping is safer than
            # rejecting on incomplete data.
            n_skipped += 1
            continue

        # What the user actually receives at our clearing prices.
        # Use _ceil_div to mirror src/shadow/scoring._score_sell_trade — the
        # CIP-14 canonical rounding direction.  Without this the validator
        # could reject a solution by 1 wei that the scoring path would
        # accept, producing inconsistent rejection counts.
        our_buy_amount = _ceil_div(
            int(trade.executed_amount) * int(cp_sell), int(cp_buy)
        )

        # External quote for the same swap, same size.  None means "no V3
        # route at all" — in which case our internal trade is by definition
        # not violating EBBO (there's no external to beat).
        try:
            ext_path = await quote_best_path(
                multicall,
                order.sell_token,
                order.buy_token,
                int(trade.executed_amount),
                intermediates,
            )
        except Exception as exc:  # noqa: BLE001
            # RPC hiccup — log and skip rather than fail-closed.  A failed
            # EBBO check on a transient network blip would deny revenue.
            log.warning(
                "ebbo_quote_failed",
                auction_id=auction.id,
                order_uid=order.uid,
                error=str(exc),
            )
            n_skipped += 1
            continue

        if ext_path is None or not ext_path:
            n_skipped += 1
            continue

        ext_buy_amount = int(ext_path[-1].amount_out)
        if ext_buy_amount <= 0:
            n_skipped += 1
            continue

        # Pass if our_buy >= ext_buy × (1 - tolerance).
        # Use integer math so we don't introduce float-precision drift.
        threshold = (ext_buy_amount * (10_000 - tolerance_bps)) // 10_000
        n_checked += 1
        if our_buy_amount < threshold:
            shortfall_bps = (
                (ext_buy_amount - our_buy_amount) * 10_000 // max(ext_buy_amount, 1)
            )
            violations.append(
                f"{order.uid[:18]}: our={our_buy_amount} ext={ext_buy_amount} "
                f"({shortfall_bps}bps short)"
            )

    return EBBOResult(
        passes=not violations,
        violations=violations,
        n_checked=n_checked,
        n_skipped=n_skipped,
        n_truncated=n_truncated,
    )
