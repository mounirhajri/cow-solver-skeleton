"""Router-v2 strategy: UniV2/Camelot multi-hop routing.

Workhorse strategy for Phase 1. For each sell order, finds the best
1- or 2-hop UniV2 path. If amount_out exceeds the order's buy_amount
(limit), emits a Trade.

Clearing prices: for each filled order, register the implied ratio.
The composer will harmonize these in Phase 2; for Phase 1 we emit
order-by-order trades with their individual implied prices, which the
naive composer accepts.
"""

from __future__ import annotations

from src.log import get_logger
from src.models.auction import Auction
from src.models.solution import Solution, Trade
from src.routing.multicall import Multicall3
from src.routing.multihop import quote_best_path
from src.solver.base import NoSolution

log = get_logger(__name__)


class RouterSolver:
    name = "router-v2"

    def __init__(self, multicall: Multicall3, intermediates: list[str]) -> None:
        self._multicall = multicall
        self._intermediates = intermediates

    async def solve(self, auction: Auction) -> Solution | NoSolution:
        trades: list[Trade] = []
        prices: dict[str, int] = {}

        for order in auction.orders:
            if order.kind != "sell":
                continue
            try:
                path = await quote_best_path(
                    self._multicall,
                    order.sell_token,
                    order.buy_token,
                    order.sell_amount,
                    self._intermediates,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "router_quote_failed",
                    uid=order.uid,
                    error=str(exc),
                )
                continue
            if path is None:
                continue
            executed_buy = path[-1].amount_out
            if executed_buy < order.buy_amount:
                continue
            trades.append(
                Trade(
                    kind="fulfillment",
                    order_uid=order.uid,
                    executed_amount=order.sell_amount,
                )
            )
            # Clearing prices must be ETH-denominated (wei per token unit, scaled
            # 1e18) so CIP-14 scoring produces correct ETH surplus values.
            # Prefer the auction's reference prices (oracle-backed, consistent
            # across all orders); fall back to the on-chain execution ratio only
            # when reference prices are missing.
            sell_info = auction.tokens.get(order.sell_token)
            buy_info = auction.tokens.get(order.buy_token)
            if (
                sell_info and sell_info.reference_price
                and buy_info and buy_info.reference_price
            ):
                prices.setdefault(order.sell_token, sell_info.reference_price)
                prices.setdefault(order.buy_token, buy_info.reference_price)
            else:
                # Execution-ratio fallback: ratio cp_sell/cp_buy = executed_buy/sell_amount
                # is correct but values aren't ETH-denominated, so cross-pair
                # scoring will be approximate.
                prices.setdefault(order.sell_token, executed_buy)
                prices.setdefault(order.buy_token, order.sell_amount)

        if not trades:
            return NoSolution()

        return Solution(
            id=int(auction.id),
            prices=prices,
            trades=trades,
            interactions=[],
        )
