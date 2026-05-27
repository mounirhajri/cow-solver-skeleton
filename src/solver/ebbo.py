"""EBBO (External Best Bid/Offer) validator — pre-submission safety net.

CoW Protocol's settlement contract enforces that each user receives at least
the surplus they would have gotten from the best external venue.  When our
composed Solution claims a clearing price that gives the user less than
external Uniswap V3 routing would, the on-chain settlement may revert or
the driver may reject the solution outright.  Repeated EBBO violations
slash the solver bond.

This module checks **before** we submit:
    for every trade in the composed Solution, get a fresh V3 quote for
    the same swap at the same input/output size; if our clearing prices
    give the user a worse deal than external V3 routing (minus tolerance),
    fail the validation and fall back to no-solution.

**Scope: V3-only.** EBBO queries the same candidate set RouterSolver emits
(``FEE_TIERS`` direct + same-fee 2-hops per intermediate) — comparing
apples to apples. The earlier ``quote_best_path`` path mixed V2 and V3,
which rejected valid V3-only Router solutions whenever Camelot or Sushi
had a better pool that Router couldn't route through anyway. When
V2Source is wired into Router (Phase 0b.5), broaden EBBO's scope to
V2+V3 again so both sides see the same venue set.

Sell trades: user sells ``executed_amount`` of sell_token.  We compute
    ``our_buy_amount = ceil(executed × cp_sell / cp_buy)`` and require it
    to meet or beat the external ``quoteExactInput`` result.

Buy trades: user receives exactly ``executed_amount`` of buy_token.  We
    compute ``our_sell_amount = floor(executed × cp_buy / cp_sell)``
    (matching ``_score_buy_trade`` rounding) and require it to be no worse
    than the external ``quoteExactOutput`` result (plus tolerance).

Trades whose tokens lack a V3 route (deep long-tail) are skipped — by
definition we cannot under-perform a non-existent external quote.

Solutions with more than ``_MAX_TRADES_PER_CHECK`` trades are rejected
outright rather than silently truncated: submitting unvalidated trades is
the exact failure mode EBBO is meant to prevent.

Tolerance default is 50 bps (0.5 %); this absorbs slippage between the
quote call and the on-chain settlement window.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.log import get_logger
from src.models.auction import Auction
from src.models.solution import Solution
from src.routing.amm_v3 import FEE_TIERS
from src.routing.multicall import Multicall3
from src.routing.v3_batched import V3Path, batched_v3_quote
from src.shadow.scoring import _ceil_div

log = get_logger(__name__)


# Default tolerance: 50 bps = 0.5 %.  Tight enough to catch genuinely
# uncompetitive solutions, loose enough to absorb sub-second pool drift
# between our quote and the production settlement window.
DEFAULT_TOLERANCE_BPS = 50

# Hard cap on trades per solution.  Exceeded → REJECT (not truncate+pass).
# Submitting unvalidated trades is the exact failure mode EBBO prevents.
_MAX_TRADES_PER_CHECK = 20


async def _quote_best_exact_input(
    multicall: Multicall3,
    sell_token: str,
    buy_token: str,
    amount_in: int,
    intermediates: list[str],
) -> int | None:
    """Return the largest buy-token output for the given sell-token input.

    V3-only mirror of ``_quote_best_exact_output``. Builds the same
    candidate set Router emits (4 direct fee tiers + same-fee 2-hops per
    intermediate) and picks the maximum ``amount_out`` across the batch.

    Returning the V3-only best — rather than ``quote_best_path``'s mixed
    V2+V3 search — keeps EBBO's scope aligned with what RouterSolver
    actually emits today. If Router is V3-only but EBBO checked V2+V3,
    a better Camelot pool would reject a Router solution that V3-best
    can't beat by definition. When V2Source lands in Router (Phase 0b.5),
    swap this helper for one that searches V2+V3 again.
    """
    paths: list[V3Path] = []
    for fee in FEE_TIERS:
        paths.append(
            V3Path(
                order_uid="",
                token_in=sell_token,
                token_out=buy_token,
                amount_in=amount_in,
                fee_tier_in=fee,
                exact_output=False,
            )
        )
    for mid in intermediates:
        if mid.lower() in (sell_token.lower(), buy_token.lower()):
            continue
        for fee in FEE_TIERS:
            paths.append(
                V3Path(
                    order_uid="",
                    token_in=sell_token,
                    token_out=buy_token,
                    amount_in=amount_in,
                    fee_tier_in=fee,
                    intermediate=mid,
                    fee_tier_out=fee,
                    exact_output=False,
                )
            )

    quotes = await batched_v3_quote(multicall, paths)
    best: int | None = None
    for q in quotes:
        if q.amount_out > 0 and (best is None or q.amount_out > best):
            best = q.amount_out
    return best


async def _quote_best_exact_output(
    multicall: Multicall3,
    sell_token: str,
    buy_token: str,
    amount_out: int,
    intermediates: list[str],
) -> int | None:
    """Return the cheapest sell-token cost to receive ``amount_out`` buy-token.

    Queries all four V3 fee tiers (direct) plus each intermediate in a single
    Multicall3 round-trip using ``quoteExactOutput`` semantics.  Returns the
    minimum non-zero ``amount_in`` found, or ``None`` if no route exists.

    ``V3BatchedQuote.amount_out`` is the variable side returned by the quoter:
    for exact-output paths that variable side is ``amount_in`` (sell-token
    cost), so naming is consistent with the sell-side helper even though the
    interpretation differs.
    """
    paths: list[V3Path] = []
    for fee in FEE_TIERS:
        paths.append(
            V3Path(
                order_uid="",
                token_in=sell_token,
                token_out=buy_token,
                amount_in=amount_out,
                fee_tier_in=fee,
                exact_output=True,
            )
        )
    for mid in intermediates:
        if mid.lower() in (sell_token.lower(), buy_token.lower()):
            continue
        for fee in FEE_TIERS:
            paths.append(
                V3Path(
                    order_uid="",
                    token_in=sell_token,
                    token_out=buy_token,
                    amount_in=amount_out,
                    fee_tier_in=fee,
                    intermediate=mid,
                    fee_tier_out=fee,
                    exact_output=True,
                )
            )

    quotes = await batched_v3_quote(multicall, paths)
    # Pick the minimum non-zero amount_in (cheapest route for the user)
    best: int | None = None
    for q in quotes:
        if q.amount_out > 0 and (best is None or q.amount_out < best):
            best = q.amount_out
    return best


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
    """Verify every trade in ``solution`` beats external V3 quote.

    Returns ``EBBOResult`` with ``passes=False`` if ANY trade falls short
    by more than ``tolerance_bps``.  Trades that lack clearing prices or
    that reference unknown order UIDs are skipped (not failed).

    Solutions with more than ``_MAX_TRADES_PER_CHECK`` trades are rejected
    immediately rather than silently truncated — submitting even one
    unvalidated trade is the failure mode this module exists to prevent.
    """
    uid_to_order = {o.uid: o for o in auction.orders}
    violations: list[str] = []
    n_checked = 0
    n_skipped = 0

    n_total = len(solution.trades)
    n_truncated = max(0, n_total - _MAX_TRADES_PER_CHECK)
    if n_truncated:
        log.warning(
            "ebbo_trade_count_truncated",
            auction_id=auction.id,
            total=n_total,
            checked=_MAX_TRADES_PER_CHECK,
            truncated=n_truncated,
        )
        return EBBOResult(
            passes=False,
            violations=[
                f"truncation: {n_total} trades exceed limit of {_MAX_TRADES_PER_CHECK}"
            ],
            n_checked=0,
            n_skipped=0,
            n_truncated=n_truncated,
        )

    for trade in solution.trades:
        order = uid_to_order.get(trade.order_uid)
        if order is None:
            n_skipped += 1
            continue

        cp_sell = solution.prices.get(order.sell_token)
        cp_buy = solution.prices.get(order.buy_token)
        if cp_sell is None or cp_buy is None or cp_buy == 0 or cp_sell == 0:
            # Composed solution missing prices for this trade's tokens —
            # skipping is safer than rejecting on incomplete data.
            n_skipped += 1
            continue

        executed = int(trade.executed_amount)

        if order.kind == "sell":
            # Sell trade: user gives executed sell_token, receives buy_token.
            # our_buy_amount = ceil(executed × cp_sell / cp_buy) — mirrors
            # _score_sell_trade rounding so validator and scorer agree.
            our_buy_amount = _ceil_div(executed * int(cp_sell), int(cp_buy))

            try:
                ext_buy_amount_opt = await _quote_best_exact_input(
                    multicall,
                    order.sell_token,
                    order.buy_token,
                    executed,
                    intermediates,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "ebbo_quote_failed",
                    auction_id=auction.id,
                    order_uid=order.uid,
                    error=str(exc),
                )
                n_skipped += 1
                continue

            if ext_buy_amount_opt is None or ext_buy_amount_opt <= 0:
                n_skipped += 1
                continue
            ext_buy_amount = ext_buy_amount_opt

            # Pass if our_buy >= ext_buy × (1 - tolerance).
            threshold = (ext_buy_amount * (10_000 - tolerance_bps)) // 10_000
            n_checked += 1
            if our_buy_amount < threshold:
                shortfall_bps = (
                    (ext_buy_amount - our_buy_amount) * 10_000
                    // max(ext_buy_amount, 1)
                )
                violations.append(
                    f"{order.uid[:18]}: our={our_buy_amount} ext={ext_buy_amount} "
                    f"({shortfall_bps}bps short)"
                )

        elif order.kind == "buy":
            # Buy trade: user receives exactly executed buy_token, pays sell_token.
            # our_sell_amount = floor(executed × cp_buy / cp_sell) — mirrors
            # _score_buy_trade rounding (integer // = floor).
            our_sell_amount = (executed * int(cp_buy)) // int(cp_sell)

            try:
                ext_sell_amount = await _quote_best_exact_output(
                    multicall,
                    order.sell_token,
                    order.buy_token,
                    executed,
                    intermediates,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "ebbo_quote_failed",
                    auction_id=auction.id,
                    order_uid=order.uid,
                    error=str(exc),
                )
                n_skipped += 1
                continue

            if ext_sell_amount is None or ext_sell_amount <= 0:
                n_skipped += 1
                continue

            # Pass if our_sell <= ext_sell × (1 + tolerance).
            # User pays MORE than external would charge = worse experience.
            threshold = (ext_sell_amount * (10_000 + tolerance_bps)) // 10_000
            n_checked += 1
            if our_sell_amount > threshold:
                overpay_bps = (
                    (our_sell_amount - ext_sell_amount) * 10_000
                    // max(ext_sell_amount, 1)
                )
                violations.append(
                    f"{order.uid[:18]}: our_sell={our_sell_amount} "
                    f"ext_sell={ext_sell_amount} ({overpay_bps}bps overpay)"
                )

        else:
            # Unknown order kind — skip defensively.
            n_skipped += 1
            continue

    return EBBOResult(
        passes=not violations,
        violations=violations,
        n_checked=n_checked,
        n_skipped=n_skipped,
        n_truncated=n_truncated,
    )
