from src.log import get_logger
from src.models.auction import Auction
from src.models.solution import Solution, Trade
from src.solver.base import NoSolution

log = get_logger(__name__)


class NaiveSolver:
    """Uses CoW Protocol reference prices to find fillable sell orders.

    No external API needed — reference prices come from the auction payload
    itself (Chainlink/oracle-based). This is a valid Phase 1 baseline.
    """

    name = "naive"

    async def solve(self, auction: Auction) -> Solution | NoSolution:
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

        return Solution(id=int(auction.id), prices=prices, trades=trades, interactions=[])
