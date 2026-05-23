"""Router-v2 strategy: UniV2/Camelot multi-hop routing.

Workhorse strategy for Phase 1. For each sell order, finds the best
1- or 2-hop UniV2 path. If amount_out exceeds the order's buy_amount
(limit), emits a Trade.

Performance:
  Arbitrum auctions carry ~1200 orders. Quoting them sequentially exhausts
  the per-strategy timeout. Two mitigations are applied:
    1. Order cap — only the top `max_orders` sell orders (by sell_amount)
       are quoted. Largest orders carry the most surplus; quoting them first
       maximises CIP-14 score within the time budget.
    2. asyncio.gather + semaphore — up to `max_concurrent` quotes run in
       parallel. Each quote makes ~3 sequential RPC round-trips (~200 ms
       each); with 20 concurrent slots, 100 orders finish in ~3 s, comfortably
       inside the 5 s per-strategy timeout.

Clearing prices: for each filled order, register the implied ratio.
The composer will harmonize these in Phase 2; for Phase 1 we emit
order-by-order trades with their individual implied prices, which the
naive composer accepts.
"""

from __future__ import annotations

import asyncio

from src.log import get_logger
from src.models.auction import Auction
from src.models.solution import Solution, Trade
from src.routing.multicall import Multicall3
from src.routing.multihop import HopQuote, quote_best_path
from src.solver.base import NoSolution

log = get_logger(__name__)

_DEFAULT_MAX_ORDERS = 50
_DEFAULT_MAX_CONCURRENT = 20
_DEFAULT_STRATEGY_TIMEOUT = 11.0


class RouterSolver:
    name = "router-v2"

    def __init__(
        self,
        multicall: Multicall3,
        intermediates: list[str],
        max_orders: int = _DEFAULT_MAX_ORDERS,
        max_concurrent: int = _DEFAULT_MAX_CONCURRENT,
        strategy_timeout: float = _DEFAULT_STRATEGY_TIMEOUT,
    ) -> None:
        self._multicall = multicall
        self._intermediates = intermediates
        self._max_orders = max_orders
        self._max_concurrent = max_concurrent
        # Advertise a custom timeout so the orchestrator gives us more headroom
        # than the default 5 s per-strategy limit. On-chain quoting needs ~3 serial
        # RPC round-trips per order; 50 orders / 20 concurrent ≈ 3 waves × ~1 s = ~3 s,
        # comfortably inside 9 s even on a slow public Arbitrum endpoint.
        self.timeout: float = strategy_timeout

    async def solve(self, auction: Auction) -> Solution | NoSolution:
        # Only sell orders; cap to the largest N by sell_amount so we stay
        # within the per-strategy timeout even on 1200-order Arbitrum auctions.
        sell_orders = sorted(
            [o for o in auction.orders if o.kind == "sell"],
            key=lambda o: o.sell_amount,
            reverse=True,
        )[: self._max_orders]

        if not sell_orders:
            return NoSolution()

        sem = asyncio.Semaphore(self._max_concurrent)

        async def _quote_one(order: object) -> tuple[object, list[HopQuote] | None]:
            async with sem:
                try:
                    path = await quote_best_path(
                        self._multicall,
                        order.sell_token,  # type: ignore[attr-defined]
                        order.buy_token,   # type: ignore[attr-defined]
                        order.sell_amount, # type: ignore[attr-defined]
                        self._intermediates,
                    )
                    return order, path
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "router_quote_failed",
                        uid=order.uid,          # type: ignore[attr-defined]
                        error=str(exc),
                    )
                    return order, None

        results = await asyncio.gather(*[_quote_one(o) for o in sell_orders])

        trades: list[Trade] = []
        prices: dict[str, int] = {}

        for order, path in results:
            if path is None:
                continue
            executed_buy = path[-1].amount_out
            if executed_buy < order.buy_amount:  # type: ignore[attr-defined]
                continue
            trades.append(
                Trade(
                    kind="fulfillment",
                    order_uid=order.uid,          # type: ignore[attr-defined]
                    executed_amount=order.sell_amount,  # type: ignore[attr-defined]
                )
            )
            # Clearing prices must be ETH-denominated (wei per token unit, scaled
            # 1e18) so CIP-14 scoring produces correct ETH surplus values.
            # Prefer the auction's reference prices (oracle-backed, consistent
            # across all orders); fall back to the on-chain execution ratio only
            # when reference prices are missing.
            sell_info = auction.tokens.get(order.sell_token)  # type: ignore[attr-defined]
            buy_info = auction.tokens.get(order.buy_token)    # type: ignore[attr-defined]
            if (
                sell_info and sell_info.reference_price
                and buy_info and buy_info.reference_price
            ):
                prices.setdefault(order.sell_token, sell_info.reference_price)  # type: ignore[attr-defined]
                prices.setdefault(order.buy_token, buy_info.reference_price)    # type: ignore[attr-defined]
            else:
                # Execution-ratio fallback: ratio cp_sell/cp_buy = executed_buy/sell_amount
                # is correct but values aren't ETH-denominated, so cross-pair
                # scoring will be approximate.
                prices.setdefault(order.sell_token, executed_buy)               # type: ignore[attr-defined]
                prices.setdefault(order.buy_token, order.sell_amount)            # type: ignore[attr-defined]

        if not trades:
            return NoSolution()

        log.info(
            "router_solved",
            auction_id=auction.id,
            n_quoted=len(sell_orders),
            n_filled=len(trades),
        )
        return Solution(
            id=int(auction.id),
            prices=prices,
            trades=trades,
            interactions=[],
        )
