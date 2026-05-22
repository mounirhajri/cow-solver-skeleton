from src.log import get_logger
from src.models.auction import Auction
from src.models.solution import Solution, Trade
from src.routing.oneinch import OneInchClient
from src.solver.base import NoSolution

log = get_logger(__name__)


class NaiveSolver:
    """Wraps 1inch to quote each sell order independently.

    This is a baseline only. It does NO CoW-matching and treats each order
    as if it's the only one in the batch. Its purpose is to give us a working
    fallback while we develop the real edge in the private submodule.
    """

    name = "naive"

    def __init__(self, oneinch: OneInchClient) -> None:
        self._oneinch = oneinch

    async def solve(self, auction: Auction) -> Solution | NoSolution:
        trades: list[Trade] = []
        prices: dict[str, int] = {}

        for order in auction.orders:
            if order.kind != "sell":
                log.debug("skip_non_sell_order", uid=order.uid, kind=order.kind)
                continue

            try:
                quote = await self._oneinch.quote(
                    src=order.sell_token,
                    dst=order.buy_token,
                    amount=order.sell_amount,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("oneinch_quote_failed", uid=order.uid, error=str(e))
                continue

            if quote.dst_amount < order.buy_amount:
                log.debug(
                    "below_limit",
                    uid=order.uid,
                    quoted=quote.dst_amount,
                    required=order.buy_amount,
                )
                continue

            trades.append(
                Trade(kind="fulfillment", order_uid=order.uid, executed_amount=order.sell_amount)
            )
            # Uniform clearing price per directed pair (sell/buy ratio)
            prices[order.sell_token] = quote.dst_amount
            prices[order.buy_token] = order.sell_amount

        if not trades:
            return NoSolution()

        return Solution(id=int(auction.id), prices=prices, trades=trades, interactions=[])
