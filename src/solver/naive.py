from __future__ import annotations

from typing import TYPE_CHECKING

from src.log import get_logger
from src.models.auction import Auction
from src.models.solution import Solution, Trade
from src.solver.base import NoSolution

if TYPE_CHECKING:
    from src.routing.multicall import Multicall3

log = get_logger(__name__)


class NaiveSolver:
    """Uses CoW Protocol reference prices to find fillable sell orders.

    No external API needed — reference prices come from the auction payload
    itself (Chainlink/oracle-based). This is a valid Phase 1 baseline.

    When *multicall* is provided, oracle clearing prices are replaced with
    real on-chain DEX quotes via price_refiner.refine_solution_prices().
    This produces accurate CIP-14 scores for shadow analytics.
    """

    name = "naive"

    def __init__(
        self,
        multicall: Multicall3 | None = None,
        intermediates: list[str] | None = None,
        refine_timeout: float = 3.0,
    ) -> None:
        self._multicall = multicall
        self._intermediates = intermediates or []
        self._refine_timeout = refine_timeout
        # Declare a custom per-strategy timeout so the orchestrator gives the
        # price refiner enough room.  The budget-divided default (13 s / 5
        # strategies ≈ 2.6 s) is shorter than refine_timeout (3 s), so naive
        # would be cancelled before the refiner finishes without this override.
        # A global asyncio.wait_for(solve_timeout_seconds=13 s) in main.py caps
        # the entire solve call, so individual strategy overrides cannot cause
        # the server to miss CoW Protocol's 15 s response deadline.
        self.timeout: float = refine_timeout + 1.0

    async def solve(self, auction: Auction) -> Solution | NoSolution:
        # /solve gates quote-only (id=None) requests before invoking any
        # strategy, so by the time we run here id is guaranteed non-None.
        # Assert narrows the type for mypy and would fail loudly if the
        # gate ever regresses.
        assert auction.id is not None, "naive must not run for quote-only auctions"
        trades: list[Trade] = []
        prices: dict[str, int] = {}

        for order in auction.orders:
            if order.kind != "sell":
                log.debug("skip_non_sell_order", uid=order.uid, kind=order.kind)
                continue

            sell_info = auction.tokens.get(order.sell_token)
            buy_info = auction.tokens.get(order.buy_token)

            if not sell_info or not sell_info.reference_price:
                log.debug("no_reference_price", uid=order.uid, token=order.sell_token)
                continue
            if not buy_info or not buy_info.reference_price:
                log.debug("no_reference_price", uid=order.uid, token=order.buy_token)
                continue

            # At reference prices: how much buy_token does sell_amount yield?
            buy_at_ref = order.sell_amount * sell_info.reference_price // buy_info.reference_price

            if buy_at_ref < order.buy_amount:
                log.debug(
                    "below_limit",
                    uid=order.uid,
                    buy_at_ref=buy_at_ref,
                    required=order.buy_amount,
                )
                continue

            trades.append(
                Trade(kind="fulfillment", order_uid=order.uid, executed_amount=order.sell_amount)
            )
            prices[order.sell_token] = sell_info.reference_price
            prices[order.buy_token] = buy_info.reference_price

        if not trades:
            return NoSolution()

        solution = Solution(id=int(auction.id), prices=prices, trades=trades, interactions=[])

        if self._multicall:
            from src.solver.price_refiner import refine_solution_prices
            solution = await refine_solution_prices(
                solution,
                auction,
                self._multicall,
                self._intermediates,
                timeout=self._refine_timeout,
            )

        return solution
