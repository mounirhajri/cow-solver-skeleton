"""Router strategy: multi-hop AMM routing.

Two modes:
  * ``v3_only_batched=True`` (default): every candidate path (direct + 2-hop
    via each intermediate, across all V3 fee tiers) for every selected order
    is collapsed into ONE Multicall3 round-trip. Drops V2 entirely. Reduces
    RPC load from ~80 round-trips to 1-2 per auction.
  * ``v3_only_batched=False``: legacy path, per-order asyncio.gather over
    `quote_best_path` (V2 + V3, all hops). Kept for tests and as escape hatch.

Performance (legacy mode, kept for context):
  Arbitrum auctions carry ~1200 orders. Quoting them sequentially exhausts
  the per-strategy timeout. Two mitigations are applied:
    1. Order cap — only the top `max_orders` sell orders (by ETH value)
       are quoted.
    2. asyncio.gather + semaphore — up to `max_concurrent` quotes in
       parallel.
"""

from __future__ import annotations

import asyncio

from src.config import settings
from src.log import get_logger
from src.models.auction import Auction
from src.models.order import Order
from src.models.solution import Solution, Trade
from src.routing.amm_v3 import FEE_TIERS
from src.routing.multicall import Multicall3
from src.routing.multihop import HopQuote, quote_best_path
from src.routing.v3_batched import V3BatchedQuote, V3Path, batched_v3_quote
from src.solver.base import NoSolution

log = get_logger(__name__)

_DEFAULT_MAX_ORDERS = 50
_DEFAULT_MAX_CONCURRENT = 20
_DEFAULT_STRATEGY_TIMEOUT = 11.0


def _eth_value_sort_key(order: Order, auction: Auction) -> int:
    """ETH-equivalent value of a sell order, used for cross-token sort.

    Auction `Token.reference_price` is ETH-denominated (wei per token unit,
    scaled 1e18 per CoW convention), so ETH value is
    ``sell_amount * reference_price // 10**18``.

    When the reference price is missing (None or 0), falls back to the raw
    ``sell_amount`` so callers with empty ``tokens={}`` retain legacy
    largest-amount-first ordering.
    """
    token_info = auction.tokens.get(order.sell_token)
    if token_info is None or not token_info.reference_price:
        return order.sell_amount
    return order.sell_amount * token_info.reference_price // 10**18


class RouterSolver:
    name = "router-v2"

    def __init__(
        self,
        multicall: Multicall3,
        intermediates: list[str],
        max_orders: int = _DEFAULT_MAX_ORDERS,
        max_concurrent: int = _DEFAULT_MAX_CONCURRENT,
        strategy_timeout: float = _DEFAULT_STRATEGY_TIMEOUT,
        v3_only_batched: bool | None = None,
    ) -> None:
        self._multicall = multicall
        self._intermediates = intermediates
        self._max_orders = max_orders
        self._max_concurrent = max_concurrent
        # When None, defer to settings so callers don't have to thread the flag
        # through every construction site; explicit values (notably tests)
        # short-circuit the global.
        self._v3_only_batched = (
            settings.router_v3_only_batched if v3_only_batched is None else v3_only_batched
        )
        # Advertise a custom timeout so the orchestrator gives us more headroom
        # than the default 5 s per-strategy limit.
        self.timeout: float = strategy_timeout

    async def solve(self, auction: Auction) -> Solution | NoSolution:
        sell_orders = sorted(
            [o for o in auction.orders if o.kind == "sell"],
            key=lambda o: _eth_value_sort_key(o, auction),
            reverse=True,
        )[: self._max_orders]

        if not sell_orders:
            return NoSolution()

        if self._v3_only_batched:
            return await self._solve_v3_batched(auction, sell_orders)
        return await self._solve_legacy(auction, sell_orders)

    # ── V3-only batched path ──────────────────────────────────────────────────

    def _build_v3_candidate_paths(
        self, orders: list[Order], _tokens: object
    ) -> list[V3Path]:
        """Per order: 4 direct paths (one per fee tier) + 4 same-fee 2-hop
        paths per intermediate. Mixed-fee 2-hops are skipped to keep the
        per-order combinatorial blow-up linear in fee tiers (4) instead of
        quadratic (16).
        """
        paths: list[V3Path] = []
        for order in orders:
            for fee in FEE_TIERS:
                paths.append(
                    V3Path(
                        order_uid=order.uid,
                        token_in=order.sell_token,
                        token_out=order.buy_token,
                        amount_in=order.sell_amount,
                        fee_tier_in=fee,
                    )
                )
            for mid in self._intermediates:
                if mid.lower() in (order.sell_token.lower(), order.buy_token.lower()):
                    continue
                for fee in FEE_TIERS:
                    paths.append(
                        V3Path(
                            order_uid=order.uid,
                            token_in=order.sell_token,
                            token_out=order.buy_token,
                            amount_in=order.sell_amount,
                            fee_tier_in=fee,
                            intermediate=mid,
                            fee_tier_out=fee,
                        )
                    )
        return paths

    @staticmethod
    def _select_best_quote_per_order(
        quotes: list[V3BatchedQuote],
    ) -> dict[str, V3BatchedQuote]:
        # Strict `>` keeps the first candidate per order_uid on ties. Since
        # _build_v3_candidate_paths iterates FEE_TIERS in declared order
        # (100, 500, 3000, 10000) and intermediates in declared order, the
        # de-facto tie-break is "lower fee tier wins, then direct over 2-hop".
        # Any future reshuffle of the build order would change selected pools —
        # if a stronger contract is needed, replace with explicit sort key.
        best: dict[str, V3BatchedQuote] = {}
        for q in quotes:
            if q.amount_out == 0:
                continue
            current = best.get(q.path.order_uid)
            if current is None or q.amount_out > current.amount_out:
                best[q.path.order_uid] = q
        return best

    async def _solve_v3_batched(
        self, auction: Auction, sell_orders: list[Order]
    ) -> Solution | NoSolution:
        paths = self._build_v3_candidate_paths(sell_orders, auction.tokens)
        if not paths:
            return NoSolution()
        try:
            quotes = await batched_v3_quote(self._multicall, paths)
        except Exception as exc:  # noqa: BLE001
            log.warning("router_v3_batched_failed", error=str(exc))
            return NoSolution()
        best_per_order = self._select_best_quote_per_order(quotes)

        trades: list[Trade] = []
        prices: dict[str, int] = {}
        for order in sell_orders:
            best = best_per_order.get(order.uid)
            if best is None:
                continue
            if best.amount_out < order.buy_amount:
                continue
            trades.append(
                Trade(
                    kind="fulfillment",
                    order_uid=order.uid,
                    executed_amount=order.sell_amount,
                )
            )
            self._register_prices(prices, order, auction, executed_buy=best.amount_out)

        if not trades:
            return NoSolution()

        log.info(
            "router_solved",
            auction_id=auction.id,
            n_quoted=len(sell_orders),
            n_paths=len(paths),
            n_filled=len(trades),
            mode="v3_batched",
        )
        return Solution(
            id=int(auction.id),
            prices=prices,
            trades=trades,
            interactions=[],
        )

    # ── Legacy per-order asyncio.gather path ──────────────────────────────────

    async def _solve_legacy(
        self, auction: Auction, sell_orders: list[Order]
    ) -> Solution | NoSolution:
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
                        uid=order.uid,  # type: ignore[attr-defined]
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
                    order_uid=order.uid,  # type: ignore[attr-defined]
                    executed_amount=order.sell_amount,  # type: ignore[attr-defined]
                )
            )
            self._register_prices(prices, order, auction, executed_buy=executed_buy)  # type: ignore[arg-type]

        if not trades:
            return NoSolution()

        log.info(
            "router_solved",
            auction_id=auction.id,
            n_quoted=len(sell_orders),
            n_filled=len(trades),
            mode="legacy",
        )
        return Solution(
            id=int(auction.id),
            prices=prices,
            trades=trades,
            interactions=[],
        )

    # ── Shared clearing-price logic ───────────────────────────────────────────

    @staticmethod
    def _register_prices(
        prices: dict[str, int],
        order: Order,
        auction: Auction,
        *,
        executed_buy: int,
    ) -> None:
        """Clearing prices must be ETH-denominated (wei per token unit, scaled
        1e18) so CIP-14 scoring produces correct ETH surplus values. Prefer
        oracle-backed reference prices; fall back to execution ratio only when
        missing (cross-pair scoring then becomes approximate).
        """
        sell_info = auction.tokens.get(order.sell_token)
        buy_info = auction.tokens.get(order.buy_token)
        if (
            sell_info and sell_info.reference_price
            and buy_info and buy_info.reference_price
        ):
            prices.setdefault(order.sell_token, sell_info.reference_price)
            prices.setdefault(order.buy_token, buy_info.reference_price)
        else:
            prices.setdefault(order.sell_token, executed_buy)
            prices.setdefault(order.buy_token, order.sell_amount)
