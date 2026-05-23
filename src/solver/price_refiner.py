"""Replace oracle clearing prices with real on-chain DEX prices.

After NaiveSolver finds fillable trades using oracle reference prices,
this module re-quotes only the specific token pairs those trades touch
and substitutes real AMM prices. Trades whose real execution price falls
below the order's limit are dropped.

Why this approach:
- NaiveSolver typically fills 5-20 trades touching 3-10 unique token pairs.
- Quoting 3-10 pairs is ~10x cheaper than RouterSolver's top-50 approach.
- Clearing prices derived from real DEX quotes produce accurate CIP-14
  scores that can be meaningfully compared to the winner's score.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from src.log import get_logger
from src.models.auction import Auction
from src.models.solution import Solution, Trade
from src.routing.multihop import quote_best_path

if TYPE_CHECKING:
    from src.routing.multicall import Multicall3

log = get_logger(__name__)


async def refine_solution_prices(
    solution: Solution,
    auction: Auction,
    multicall: Multicall3,
    intermediates: list[str],
    timeout: float = 3.0,
) -> Solution:
    """Re-quote the token pairs in *solution* and return a price-refined copy.

    For each unique (sell_token, buy_token) pair in the solution's trades,
    fetches the best available DEX path (V2 + V3, 1- and 2-hop). Trades
    whose pair cannot be quoted or whose real amount_out < order.buy_amount
    are dropped. If no trades survive, the original oracle-priced solution
    is returned unchanged (safe fallback — never returns empty).

    Args:
        solution:      Naive solution with oracle clearing prices.
        auction:       Current auction (needed for order buy_amount limits).
        multicall:     Multicall3 client for batched RPC calls.
        intermediates: Token addresses to use as 2-hop bridges.
        timeout:       Per-pair quote timeout in seconds (default 3 s).
                       Total time ≤ timeout (all pairs quoted in parallel).

    Returns:
        A new Solution with real DEX clearing prices, or *solution* unchanged
        if quoting fails entirely.
    """
    # Build lookup: uid → order (for buy_amount limit check)
    orders_by_uid = {o.uid: o for o in auction.orders}

    # Collect unique pairs from the trades that are fulfillments
    pairs: dict[tuple[str, str], list[Trade]] = {}
    for trade in solution.trades:
        if trade.kind != "fulfillment":
            continue
        order = orders_by_uid.get(trade.order_uid)
        if order is None:
            continue
        key = (order.sell_token.lower(), order.buy_token.lower())
        pairs.setdefault(key, []).append(trade)

    if not pairs:
        return solution

    # Quote all pairs in parallel
    async def _quote_pair(
        sell_token: str, buy_token: str, sell_amount: int
    ) -> tuple[str, str, int, int] | None:
        """Returns (sell_token, buy_token, price_sell, price_buy) or None."""
        try:
            path = await asyncio.wait_for(
                quote_best_path(multicall, sell_token, buy_token, sell_amount, intermediates),
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "price_refiner_quote_failed",
                pair=f"{sell_token[:8]}->{buy_token[:8]}",
                error=str(exc),
            )
            return None
        if path is None:
            return None
        # Use execution ratio as clearing price:
        # price_sell / price_buy = amount_out / sell_amount
        # Represent as: price_sell = amount_out, price_buy = sell_amount
        amount_out = path[-1].amount_out
        if amount_out <= 0:
            return None
        return (sell_token, buy_token, amount_out, sell_amount)

    # Use the largest sell_amount among all trades for a given pair as the
    # representative quote — largest order gives the most realistic price.
    pair_sell_amounts: dict[tuple[str, str], int] = {}
    for (sell_tok, buy_tok), trades_for_pair in pairs.items():
        max_sell = max(
            orders_by_uid[t.order_uid].sell_amount
            for t in trades_for_pair
            if t.order_uid in orders_by_uid
        )
        pair_sell_amounts[(sell_tok, buy_tok)] = max_sell

    quote_tasks = [
        _quote_pair(sell_tok, buy_tok, sell_amt)
        for (sell_tok, buy_tok), sell_amt in pair_sell_amounts.items()
    ]
    quote_results = await asyncio.gather(*quote_tasks)

    # Build real price map: (sell_token, buy_token) → (cp_sell, cp_buy)
    real_prices: dict[tuple[str, str], tuple[int, int]] = {}
    for result in quote_results:
        if result is None:
            continue
        sell_tok, buy_tok, cp_sell, cp_buy = result
        real_prices[(sell_tok, buy_tok)] = (cp_sell, cp_buy)

    if not real_prices:
        log.debug("price_refiner_no_quotes", auction_id=auction.id)
        return solution

    # Filter trades: only keep those whose pair was quoted AND real price
    # still beats the order's buy_amount limit.
    refined_trades: list[Trade] = []
    refined_prices: dict[str, int] = {}

    for trade in solution.trades:
        if trade.kind != "fulfillment":
            refined_trades.append(trade)
            continue
        order = orders_by_uid.get(trade.order_uid)
        if order is None:
            continue
        key = (order.sell_token.lower(), order.buy_token.lower())
        price_pair = real_prices.get(key)
        if price_pair is None:
            # No quote for this pair — fall back to oracle price for this trade
            sell_info = auction.tokens.get(order.sell_token)
            buy_info = auction.tokens.get(order.buy_token)
            if sell_info and sell_info.reference_price and buy_info and buy_info.reference_price:
                refined_trades.append(trade)
                refined_prices.setdefault(order.sell_token, int(sell_info.reference_price))
                refined_prices.setdefault(order.buy_token, int(buy_info.reference_price))
            continue

        cp_sell, cp_buy = price_pair
        # Check: at real DEX price, does executed_sell_amount yield >= buy_amount?
        real_buy = trade.executed_amount * cp_sell // cp_buy
        if real_buy < order.buy_amount:
            log.debug(
                "price_refiner_trade_dropped",
                uid=trade.order_uid[:16],
                real_buy=real_buy,
                required=order.buy_amount,
            )
            continue

        refined_trades.append(trade)
        refined_prices.setdefault(order.sell_token, cp_sell)
        refined_prices.setdefault(order.buy_token, cp_buy)

    if not refined_trades:
        log.debug("price_refiner_all_dropped", auction_id=auction.id)
        return solution

    n_oracle = len(solution.trades) - len(refined_trades)
    log.info(
        "price_refiner_done",
        auction_id=auction.id,
        n_trades_in=len(solution.trades),
        n_trades_out=len(refined_trades),
        n_dropped=n_oracle,
        n_pairs_quoted=len(real_prices),
    )

    return Solution(
        id=solution.id,
        prices=refined_prices,
        trades=refined_trades,
        interactions=solution.interactions,
    )
