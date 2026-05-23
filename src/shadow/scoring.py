"""CIP-14 solution scoring — exact formula from cowprotocol/services driver source.

Reference: crates/driver/src/domain/competition/solution/scoring.rs
    Score = Σ per-trade user_surplus converted to ETH via native prices.

Units
-----
- clearing_prices (solution.prices):  token → arbitrary unit, ratio matters
- native_prices  (auction.prices):    token → wei per 1 token, scaled by 1e18
  i.e.  price_in_eth = token_amount * native_price // 10**18
- score:  wei  (ETH-denominated surplus)

Protocol fees are not applied in shadow mode (we don't charge fees), so the
score computed here is slightly higher than the driver would produce for fee-
taking solvers.  For a fair comparison the winner's score (from the competition
API) must also be read from shadow_winners.score — do NOT try to recompute it.
"""

from __future__ import annotations

import contextlib
from typing import Any


def _ceil_div(a: int, b: int) -> int:
    """Ceiling integer division, matches Rust `checked_ceil_div`."""
    if b == 0:
        raise ZeroDivisionError
    return (a + b - 1) // b


def _score_sell_trade(
    executed: int,
    signed_sell: int,
    signed_buy: int,
    cp_sell: int,
    cp_buy: int,
    native_price_buy: int,
) -> int:
    """Surplus for a *sell* order (user sells sell-token, receives buy-token).

    surplus_buy = ceil(executed * cp_sell / cp_buy) - ceil(executed * signed_buy / signed_sell)
    score       = surplus_buy * native_price_buy // 1e18
    """
    limit_buy = _ceil_div(executed * signed_buy, signed_sell)
    bought = _ceil_div(executed * cp_sell, cp_buy)
    surplus = bought - limit_buy
    if surplus <= 0:
        return 0
    return surplus * native_price_buy // (10**18)


def _score_buy_trade(
    executed: int,
    signed_sell: int,
    signed_buy: int,
    cp_sell: int,
    cp_buy: int,
    native_price_buy: int,
) -> int:
    """Surplus for a *buy* order (user buys buy-token, pays sell-token).

    surplus_sell = executed * signed_sell // signed_buy - executed * cp_buy // cp_sell
    score        = (surplus_sell * signed_buy // signed_sell) * native_price_buy // 1e18

    (Buy-order surplus is in sell-token; convert to buy-token before ETH conversion.)
    """
    limit_sell = executed * signed_sell // signed_buy
    sold = executed * cp_buy // cp_sell
    surplus_sell = limit_sell - sold
    if surplus_sell <= 0:
        return 0
    # Convert sell-token surplus → buy-token equivalent
    surplus_buy = surplus_sell * signed_buy // signed_sell
    return surplus_buy * native_price_buy // (10**18)


def compute_solution_score(
    solution: dict[str, Any],
    orders_by_uid: dict[str, dict[str, Any]],
    native_prices: dict[str, int],
) -> int:
    """Return the CIP-14 quality score (in wei) for a solution.

    Args:
        solution:      solver solution dict — must have ``prices`` (token→str/int)
                       and ``trades`` (list of {kind, orderUid, executedAmount}).
        orders_by_uid: map  uid (lower) → order dict with
                       sellToken, buyToken, sellAmount, buyAmount, kind.
        native_prices: map  token (lower) → int  — ETH price scaled by 1e18.
                       Comes from ``raw_competition["auction"]["prices"]``.

    Returns:
        Score in wei; 0 if inputs are missing or no trades produce positive surplus.
    """
    if not solution or not orders_by_uid or not native_prices:
        return 0

    # Build clearing price map (lower-case keys, integer values)
    cp: dict[str, int] = {}
    for tok, price in (solution.get("prices") or {}).items():
        with contextlib.suppress(ValueError, TypeError):
            cp[tok.lower()] = int(price)

    # Normalise native prices to lower-case int map
    np_map: dict[str, int] = {}
    for tok, price in native_prices.items():
        with contextlib.suppress(ValueError, TypeError):
            np_map[tok.lower()] = int(price)

    total = 0
    for trade in solution.get("trades") or []:
        if trade.get("kind") != "fulfillment":
            continue

        uid = (trade.get("orderUid") or trade.get("order_uid") or "").lower()
        order = orders_by_uid.get(uid)
        if order is None:
            continue

        try:
            executed = int(trade.get("executedAmount") or 0)
            if executed <= 0:
                continue

            sell_tok = (order.get("sellToken") or order.get("sell_token") or "").lower()
            buy_tok = (order.get("buyToken") or order.get("buy_token") or "").lower()
            signed_sell = int(order.get("sellAmount") or order.get("sell_amount") or 0)
            signed_buy = int(order.get("buyAmount") or order.get("buy_amount") or 0)
            side = (order.get("kind") or "sell").lower()

            cp_sell = cp.get(sell_tok, 0)
            cp_buy = cp.get(buy_tok, 0)
            native_buy = np_map.get(buy_tok, 0)

            if not all([signed_sell, signed_buy, cp_sell, cp_buy, native_buy]):
                continue

            if side == "sell":
                total += _score_sell_trade(
                    executed, signed_sell, signed_buy, cp_sell, cp_buy, native_buy
                )
            else:
                total += _score_buy_trade(
                    executed, signed_sell, signed_buy, cp_sell, cp_buy, native_buy
                )

        except (ValueError, TypeError, ZeroDivisionError):
            continue

    return total


def score_at_external_prices(
    solution: dict[str, Any],
    orders_by_uid: dict[str, dict[str, Any]],
    native_prices: dict[str, int],
    clearing_prices: dict[str, Any],
) -> int:
    """Score *our* trades but using externally supplied clearing prices.

    Used for the winner-price comparison column (Phase 4a): re-evaluates our
    fulfillments at the winner's clearingPrices to isolate "wrong trades
    chosen" from "our prices were off".

    Lower-cases ``clearing_prices`` keys to match the convention in
    :func:`compute_solution_score`.  Does not mutate ``solution``.
    """
    if not solution:
        return 0
    cp_lower: dict[str, Any] = {}
    for tok, price in (clearing_prices or {}).items():
        if isinstance(tok, str):
            cp_lower[tok.lower()] = price
    substituted = {**solution, "prices": cp_lower}
    return compute_solution_score(substituted, orders_by_uid, native_prices)


def extract_native_prices(raw_competition: dict[str, Any]) -> dict[str, int]:
    """Pull ``auction.prices`` from a competition response dict.

    Returns a lower-case token → int map, empty dict if not present.
    """
    comp_auction = raw_competition.get("auction") or {}
    prices_raw = comp_auction.get("prices") or {}
    result: dict[str, int] = {}
    for tok, val in prices_raw.items():
        with contextlib.suppress(ValueError, TypeError):
            result[tok.lower()] = int(val)
    return result


def orders_by_uid_from_auction(auction: Any) -> dict[str, dict[str, Any]]:
    """Build uid → order dict from a Pydantic Auction model or a raw dict.

    Accepts either the Pydantic ``Auction`` model (from /solve path) or a raw
    dict (from poller path).  Always returns lower-case uid keys.
    """
    result: dict[str, dict[str, Any]] = {}

    # Pydantic model path
    orders = getattr(auction, "orders", None)
    if orders is not None:
        for o in orders:
            uid = getattr(o, "uid", None)
            if uid:
                result[uid.lower()] = {
                    "sellToken": getattr(o, "sell_token", ""),
                    "buyToken": getattr(o, "buy_token", ""),
                    "sellAmount": getattr(o, "sell_amount", 0),
                    "buyAmount": getattr(o, "buy_amount", 0),
                    "kind": getattr(o, "kind", "sell"),
                }
        return result

    # Raw dict path
    if isinstance(auction, dict):
        for o in auction.get("orders") or []:
            uid = (o.get("uid") or o.get("orderUid") or "").lower()
            if uid:
                result[uid] = o
    return result
