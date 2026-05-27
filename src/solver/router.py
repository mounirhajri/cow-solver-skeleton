"""Router strategy: multi-hop AMM routing.

Two modes:
  * ``v3_only_batched=True`` (default): every candidate path (direct + 2-hop
    via each intermediate, across all V3 fee tiers) for every selected order
    is collapsed into ONE Multicall3 round-trip. Drops V2 entirely. Reduces
    RPC load from ~80 round-trips to 1-2 per auction. Supports both sell
    orders (quoteExactInput) and buy orders (quoteExactOutput).
  * ``v3_only_batched=False``: legacy path, per-order asyncio.gather over
    `quote_best_path` (V2 + V3, all hops). Kept for tests and as escape hatch.
    Buy orders are skipped here — quote_best_path is exact-input only.

Performance (legacy mode, kept for context):
  Arbitrum auctions carry ~1200 orders. Quoting them sequentially exhausts
  the per-strategy timeout. Two mitigations are applied:
    1. Order cap — only the top `max_orders` orders (by surplus headroom)
       are quoted.
    2. asyncio.gather + semaphore — up to `max_concurrent` quotes in
       parallel.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from src.config import settings
from src.encoder.interactions import Interaction
from src.encoder.v3 import encode_v3_swap
from src.liquidity.base import LiquiditySource, SwapRequest
from src.log import get_logger
from src.models.auction import Auction
from src.models.order import Order
from src.models.solution import Solution, Trade
from src.routing.amm_v3 import FEE_TIERS
from src.routing.multicall import Multicall3

if TYPE_CHECKING:
    from edge.matching.ghost_detector import GhostDetector
from src.routing.multihop import HopQuote, quote_best_path
from src.routing.v3_batched import V3BatchedQuote, V3Path, batched_v3_quote
from src.solver.base import NoSolution

# Deadline grace window encoded into the swap call.
_SWAP_DEADLINE_SECONDS = 60

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


def _expected_surplus_sort_key(order: Order, auction: Auction) -> int:
    """Expected surplus headroom at reference prices.

    Computes ``sell_value - buy_value`` in ETH-equivalent units.  Positive
    when the user has given us margin to capture (sell side worth more at
    market than buy side requires); zero / negative when the order is
    already OTM at reference (any router quote would lose).

    Symmetric for sell and buy orders: both expose the same signed
    (sell_amount, buy_amount) pair, and the absolute headroom is the same
    |sell_value - buy_value| regardless of which side the protocol fixes
    as "exact". One key sorts both kinds consistently.

    Why this beats sorting by ETH-value: whale orders (high ETH-value) tend
    to be pre-optimised — their limit prices sit on the live market price
    so the surplus headroom is ~0 and we cannot beat the winner solver.
    Mid-size orders with sloppy limits leave 10–100 bps of headroom; those
    are where router-v2 can win.

    Falls back to ``_eth_value_sort_key`` when either token lacks a
    reference price — without prices we can't compute headroom, and ETH
    value is at least a directional proxy for "interesting order".
    """
    sell_tok = auction.tokens.get(order.sell_token)
    buy_tok = auction.tokens.get(order.buy_token)
    if (
        sell_tok is None
        or sell_tok.reference_price is None
        or buy_tok is None
        or buy_tok.reference_price is None
    ):
        return _eth_value_sort_key(order, auction)
    sell_value = order.sell_amount * sell_tok.reference_price
    buy_value = order.buy_amount * buy_tok.reference_price
    # OTM orders (negative headroom) collapse to 0 so they sort to the back
    # rather than dominating with large negative numbers.  We still process
    # them if max_orders > #ITM_orders, but never preferentially.
    return max(0, (sell_value - buy_value) // 10**18)


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
        # Encoder configuration — addresses + slippage default from
        # settings but exposed as constructor params so tests can override
        # without touching the global Settings instance.
        gpv2_settlement: str | None = None,
        v3_router_address: str | None = None,
        slippage_bps: int | None = None,
        # V2 fallback: optional list of V2-style LiquiditySources. When
        # provided AND settings.router_v2_fallback_enabled is True, orders
        # the V3-batched pass couldn't fill get one more chance through V2
        # quoting. Sources stay None on default-instantiation so existing
        # tests don't pick up the new behaviour.
        v2_sources: list[LiquiditySource] | None = None,
        # Phase A ghost-order filter — same DynamicGhostDetector instance
        # the Bipartite + Multi-Party strategies share. Drops UIDs known
        # not to settle (seen >=20×/24h with 0 winner-fills) before the
        # surplus-headroom sort, so phantom loose-limit orders (e.g. the
        # 1-WBTC TWAP at +22 % oracle observed 2026-05-27) don't dominate
        # the top-N selection.
        ghost_detector: GhostDetector | None = None,
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
        self._gpv2_settlement = (
            settings.gpv2_settlement if gpv2_settlement is None else gpv2_settlement
        )
        self._v3_router_address = (
            settings.v3_swap_router if v3_router_address is None else v3_router_address
        )
        self._slippage_bps = (
            settings.encoder_slippage_bps if slippage_bps is None else slippage_bps
        )
        # Empty list (not None) is the default so iteration is safe without
        # a None check on the hot path. The fallback is gated on this list
        # being non-empty AND the settings flag — either off → V3-only as before.
        self._v2_sources = v2_sources or []
        self._ghost_detector = ghost_detector
        # Advertise a custom timeout so the orchestrator gives us more headroom
        # than the default 5 s per-strategy limit.
        self.timeout: float = strategy_timeout

    async def solve(self, auction: Auction) -> Solution | NoSolution:
        # Phase A ghost-order filter — drop UIDs known not to settle
        # before the surplus-headroom sort. Without this gate, persistent
        # loose-limit phantom orders (e.g. a single +22 % oracle TWAP UID
        # detected by Phase A as a ghost) would dominate the top-N pick
        # because their OTM headroom dwarfs every real order's surplus.
        # Mirror of bipartite.py:152-167 / multi_party.py:151-167.
        candidate_orders = list(auction.orders)
        if self._ghost_detector is not None:
            n_before = len(candidate_orders)
            kept: list[Order] = []
            for o in candidate_orders:
                if not await self._ghost_detector.is_ghost(o):
                    kept.append(o)
            candidate_orders = kept
            if len(candidate_orders) < n_before:
                log.info(
                    "router_dynamic_ghost_filter",
                    auction_id=auction.id,
                    n_filtered=n_before - len(candidate_orders),
                    n_remaining=len(candidate_orders),
                )

        # Both sell and buy orders flow through; _expected_surplus_sort_key
        # is symmetric across kinds. The V3-batched path quotes sells with
        # exactInput and buys with exactOutput. The legacy path filters
        # buys out (quote_best_path is exact-input only).
        orders = sorted(
            candidate_orders,
            key=lambda o: _expected_surplus_sort_key(o, auction),
            reverse=True,
        )[: self._max_orders]

        if not orders:
            return NoSolution()

        if self._v3_only_batched:
            return await self._solve_v3_batched(auction, orders)
        return await self._solve_legacy(auction, orders)

    # ── V3-only batched path ──────────────────────────────────────────────────

    def _build_v3_candidate_paths(
        self, orders: list[Order], _tokens: object
    ) -> list[V3Path]:
        """Per order: 4 direct paths (one per fee tier) + 4 same-fee 2-hop
        paths per intermediate. Mixed-fee 2-hops are skipped to keep the
        per-order combinatorial blow-up linear in fee tiers (4) instead of
        quadratic (16).

        Sell orders use exact-input (amount = sell_amount). Buy orders use
        exact-output (amount = buy_amount); v3_batched flips the selector
        and reverses the multi-hop path encoding accordingly.

        Partial-fill extension (sell-only): for ``partially_fillable=True``
        sell orders, two extra sets of paths are added at ``0.5×sell_amount``
        and ``0.75×sell_amount``.  All three fractions (full + 0.5x + 0.75x)
        travel in the same Multicall3 round-trip — zero extra RPC calls.
        Buy-side partial-quote is deferred (quoteExactOutput semantics for
        partials are subtle).
        """
        paths: list[V3Path] = []
        for order in orders:
            exact_output = order.kind == "buy"
            amount = order.buy_amount if exact_output else order.sell_amount

            # Determine which input amounts to quote for this order.
            # Partial sell orders get 3 fractions; all others get just one.
            if order.kind == "sell" and order.partially_fillable:
                amounts_to_quote = [
                    amount,
                    3 * amount // 4,
                    amount // 2,
                ]
            else:
                amounts_to_quote = [amount]

            for amt in amounts_to_quote:
                for fee in FEE_TIERS:
                    paths.append(
                        V3Path(
                            order_uid=order.uid,
                            token_in=order.sell_token,
                            token_out=order.buy_token,
                            amount_in=amt,
                            fee_tier_in=fee,
                            exact_output=exact_output,
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
                                amount_in=amt,
                                fee_tier_in=fee,
                                intermediate=mid,
                                fee_tier_out=fee,
                                exact_output=exact_output,
                            )
                        )
        return paths

    @staticmethod
    def _select_best_quote_per_order(
        quotes: list[V3BatchedQuote],
        *,
        filter_amount_in: int | None = None,
    ) -> dict[str, V3BatchedQuote]:
        """Select the best quote per order_uid.

        For sell orders (exact-input): higher amount_out wins — more
        buy-token for the user. For buy orders (exact-output):
        V3BatchedQuote.amount_out holds amountIn, so LOWER wins — less
        sell-token spent for the exact buy_amount.

        Strict comparison keeps the first candidate per order_uid on ties.
        Since _build_v3_candidate_paths iterates FEE_TIERS in declared
        order (100, 500, 3000, 10000) and intermediates in declared order,
        the de-facto tie-break is "lower fee tier wins, then direct over
        2-hop". Swap for an explicit sort key if that order matters.

        ``filter_amount_in``: when set, only consider quotes whose path has
        exactly this ``amount_in`` value. Used to select the best quote
        among a specific fraction (e.g. the best 0.75× route).
        """
        best: dict[str, V3BatchedQuote] = {}
        for q in quotes:
            if q.amount_out == 0:
                continue
            if filter_amount_in is not None and q.path.amount_in != filter_amount_in:
                continue
            current = best.get(q.path.order_uid)
            if current is None:
                best[q.path.order_uid] = q
                continue
            if q.path.exact_output:
                if q.amount_out < current.amount_out:
                    best[q.path.order_uid] = q
            else:
                if q.amount_out > current.amount_out:
                    best[q.path.order_uid] = q
        return best

    async def _solve_v3_batched(
        self, auction: Auction, orders: list[Order]
    ) -> Solution | NoSolution:
        paths = self._build_v3_candidate_paths(orders, auction.tokens)
        if not paths:
            return NoSolution()
        try:
            quotes = await batched_v3_quote(self._multicall, paths)
        except Exception as exc:  # noqa: BLE001
            log.warning("router_v3_batched_failed", error=str(exc))
            return NoSolution()
        # Full-amount best quotes (exact_input sell orders use sell_amount as
        # amount_in; buy orders use buy_amount).
        best_per_order = self._select_best_quote_per_order(quotes)

        # Partial-fraction best quotes — only computed once over all quotes;
        # only meaningful for partially_fillable sell orders (buy-side deferred).
        # Keyed by order_uid; values are the best quotes at those fixed amounts.
        # We compute these lazily per-order below using the helper's amount_in
        # filter, rather than building three separate dicts upfront, to keep the
        # code explicit and easy to follow.

        trades: list[Trade] = []
        prices: dict[str, int] = {}
        # GPv2 settles `[pre, intra, post]`; we only populate intra. All V3
        # interactions land in this list and the Solution publishes
        # `[[], intra_interactions, []]` at the end.
        intra_interactions: list[dict[str, object]] = []
        # One deadline for the whole batch — the auction's solve window is
        # 30 s, the swap deadline buffer is _SWAP_DEADLINE_SECONDS = 60 s, so
        # every Interaction inside this Solution shares a single timestamp.
        deadline = int(time.time()) + _SWAP_DEADLINE_SECONDS
        # Track filled UIDs so the V2 fallback (below) only retries genuine
        # misses — not orders we already covered via the V3 batched pass.
        filled_uids: set[str] = set()
        for order in orders:
            best = best_per_order.get(order.uid)
            if order.kind == "buy":
                if best is None:
                    continue
                # Quoter returned amount_in (sell-side). Skip if the AMM
                # demands more sell-token than the user signed away.
                amount_in = best.amount_out
                if amount_in > order.sell_amount:
                    continue
                # CoW Trade convention (see shadow.scoring._score_buy_trade):
                # `executedAmount` is the EXACT side, which for a buy order
                # is buy_amount. The variable amount_in is communicated via
                # clearing prices, not via executedAmount.
                # Register prices BEFORE appending the trade — if a prior
                # trade in this Solution already pinned an incompatible
                # rate for either token, _register_prices returns False
                # and we silently drop this order (CIP-67 invariant).
                if not self._register_prices(
                    prices, order,
                    executed_buy=order.buy_amount,
                    executed_sell=amount_in,
                ):
                    continue
                trades.append(
                    Trade(
                        kind="fulfillment",
                        order_uid=order.uid,
                        executed_amount=order.buy_amount,
                    )
                )
                intra_interactions.append(
                    self._encode_path_interaction(
                        best.path,
                        executed_sell=amount_in,
                        executed_buy=order.buy_amount,
                        deadline=deadline,
                    ).to_gpv2_dict()
                )
                filled_uids.add(order.uid)
            else:
                # Sell order: try full amount first.
                if best is not None and best.amount_out >= order.buy_amount:
                    # Full quote clears — emit at full sell_amount, but only
                    # if the rate is CIP-67-compatible with the in-progress
                    # clearing-price map. See the comment in the buy branch.
                    if not self._register_prices(
                        prices, order,
                        executed_buy=best.amount_out,
                        executed_sell=order.sell_amount,
                    ):
                        continue
                    trades.append(
                        Trade(
                            kind="fulfillment",
                            order_uid=order.uid,
                            executed_amount=order.sell_amount,
                        )
                    )
                    intra_interactions.append(
                        self._encode_path_interaction(
                            best.path,
                            executed_sell=order.sell_amount,
                            executed_buy=best.amount_out,
                            deadline=deadline,
                        ).to_gpv2_dict()
                    )
                    filled_uids.add(order.uid)
                elif order.partially_fillable:
                    # Full amount missed limit. Try partial fractions in
                    # descending order (0.75x then 0.5x) — emit at the
                    # largest feasible fraction.  Each fraction's limit is
                    # proportional: a fill of ``f × sell_amount`` must return
                    # at least ``f × buy_amount`` (pro-rata).
                    # All fraction paths were included in the original batch
                    # (see _build_v3_candidate_paths), so no extra RPC calls.
                    #
                    # Spec-deviation: probe descending (0.75x → 0.5x) instead
                    # of the spec's "if 0.5x clears, midpoint search".
                    # Descending order is strictly better: it emits the larger
                    # feasible fill in EVERY case the spec would emit, AND it
                    # also emits when 0.5x misses but 0.75x clears (an
                    # asymmetric AMM pricing curve can produce this — common
                    # with concentrated liquidity).  Same RPC budget (2 extra
                    # batched quotes), strictly more user fills.
                    # Floor is intentional: pro-rata limits always favour
                    # the user (CoW convention).
                    partial_fractions = [
                        (3 * order.sell_amount // 4, 3 * order.buy_amount // 4),
                        (order.sell_amount // 2, order.buy_amount // 2),
                    ]
                    emitted = False
                    for partial_sell, partial_buy_limit in partial_fractions:
                        frac_best = self._select_best_quote_per_order(
                            quotes, filter_amount_in=partial_sell
                        ).get(order.uid)
                        if frac_best is not None and frac_best.amount_out >= partial_buy_limit:
                            # Same CIP-67 register-first guard as the full
                            # path above. If the fraction's rate conflicts
                            # with an in-progress price map we move on to
                            # the next fraction (or fall through to skip)
                            # rather than dropping the whole order.
                            if not self._register_prices(
                                prices, order,
                                executed_buy=frac_best.amount_out,
                                executed_sell=partial_sell,
                            ):
                                continue
                            trades.append(
                                Trade(
                                    kind="fulfillment",
                                    order_uid=order.uid,
                                    executed_amount=partial_sell,
                                )
                            )
                            intra_interactions.append(
                                self._encode_path_interaction(
                                    frac_best.path,
                                    executed_sell=partial_sell,
                                    executed_buy=frac_best.amount_out,
                                    deadline=deadline,
                                ).to_gpv2_dict()
                            )
                            filled_uids.add(order.uid)
                            log.info(
                                "router_partial_fill_emitted",
                                auction_id=auction.id,
                                order_uid=order.uid,
                                fraction=partial_sell / order.sell_amount,
                                partial_sell=partial_sell,
                                amm_output=frac_best.amount_out,
                            )
                            emitted = True
                            break
                    if not emitted:
                        continue
                else:
                    # Non-partial sell order misses limit → skip (no trade).
                    continue

        # V2 fallback: orders the V3-batched pass couldn't fill (no pool,
        # limit miss, or partial-fraction miss) get one more chance via
        # V2 sources. Gated on both the settings flag AND a non-empty
        # sources list — disabled by default to keep production behaviour
        # unchanged until the path has fork-test coverage.
        if (
            settings.router_v2_fallback_enabled
            and self._v2_sources
            and len(filled_uids) < len(orders)
        ):
            await self._fill_v2_fallback(
                auction=auction,
                orders=orders,
                filled_uids=filled_uids,
                trades=trades,
                prices=prices,
                intra_interactions=intra_interactions,
            )

        if not trades:
            return NoSolution()

        log.info(
            "router_solved",
            auction_id=auction.id,
            n_quoted=len(orders),
            n_paths=len(paths),
            n_filled=len(trades),
            n_interactions=len(intra_interactions),
            mode="v3_batched",
        )
        # All V3 swap calls go in the flat ``interactions`` list. The CoW
        # driver maps each entry into the GPv2 settle() intra-interaction
        # slot — pre/post stages aren't needed for vanilla AMM routing.
        return Solution(
            id=int(auction.id),
            prices=prices,
            trades=trades,
            interactions=intra_interactions,
        )

    # ── Legacy per-order asyncio.gather path ──────────────────────────────────

    async def _solve_legacy(
        self, auction: Auction, orders: list[Order]
    ) -> Solution | NoSolution:
        # quote_best_path is exact-input only; buys would need exact-output
        # plumbing. v3_only_batched is the supported path for buys.
        # Visibility: when v3_only_batched gets disabled in prod (e.g. during
        # an Alchemy outage), we silently lose buy-order volume — log it so
        # the gap is observable. See specs/2026-05-26-router-and-logging-followups.md §2.
        sell_orders = [o for o in orders if o.kind == "sell"]
        n_buys_dropped = len(orders) - len(sell_orders)
        if n_buys_dropped > 0:
            log.warning(
                "router_legacy_path_skips_buys",
                auction_id=auction.id,
                n_dropped=n_buys_dropped,
                n_processed=len(sell_orders),
            )
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
            # Same CIP-67 register-first guard as the V3-batched path.
            if not self._register_prices(
                prices, order,  # type: ignore[arg-type]
                executed_buy=executed_buy,
                executed_sell=order.sell_amount,  # type: ignore[attr-defined]
            ):
                continue
            trades.append(
                Trade(
                    kind="fulfillment",
                    order_uid=order.uid,  # type: ignore[attr-defined]
                    executed_amount=order.sell_amount,  # type: ignore[attr-defined]
                )
            )

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

    # ── V2 fallback ───────────────────────────────────────────────────────────

    async def _fill_v2_fallback(
        self,
        *,
        auction: Auction,
        orders: list[Order],
        filled_uids: set[str],
        trades: list[Trade],
        prices: dict[str, int],
        intra_interactions: list[dict[str, object]],
    ) -> None:
        """Try V2 sources for every order V3 missed; mutate state in place.

        Per-order fan-out (not all-orders-at-once batched like V3) because
        V2 quoting needs two RPC round-trips per pair (getPair → getReserves)
        — batching across orders would blow up the multi-round-trip count.
        At Phase 0b scale, a handful of unfilled orders per auction is the
        norm, so the per-order cost is acceptable.

        Smart-wallet vs EOA orders are handled identically — GPv2 Settlement
        uses the same VaultRelayer.transferFromAccounts mechanism for both
        signature schemes, so there is no extra allowance-check path
        unique to EIP-1271 orders. Same scope as the V3 batched path above.
        """
        # Use a short timeout per source — V2 quoting must not blow the
        # strategy budget on slow RPCs. The total per-order budget is
        # min(N_sources × 800ms) running in parallel.
        per_source_timeout_ms = 800

        for order in orders:
            if order.uid in filled_uids:
                continue

            req_kind = order.kind
            if req_kind == "sell":
                sell_amount = order.sell_amount
                buy_amount = 0
            else:
                sell_amount = 0
                buy_amount = order.buy_amount
            try:
                req = SwapRequest(
                    sell_token=order.sell_token,
                    buy_token=order.buy_token,
                    sell_amount=sell_amount,
                    buy_amount=buy_amount,
                    kind=req_kind,
                    chain_id=getattr(order, "sell_chain_id", 42161),
                )
            except ValueError:
                # Same-token or zero-amount — skip defensively.
                continue

            # Fan out across all configured V2 sources in parallel.
            quote_tasks = [
                src.quote(req, timeout_ms=per_source_timeout_ms)
                for src in self._v2_sources
            ]
            results = await asyncio.gather(*quote_tasks, return_exceptions=True)

            best_quote = None
            best_source = None
            for src, q in zip(self._v2_sources, results, strict=True):
                if isinstance(q, BaseException) or q is None:
                    continue
                # Limit-price check before considering — same predicate as V3.
                if order.kind == "sell" and q.buy_amount < order.buy_amount:
                    continue
                if order.kind == "buy" and q.sell_amount > order.sell_amount:
                    continue
                # Pick across sources: max buy for sell, min sell for buy.
                is_better = (
                    best_quote is None
                    or (
                        order.kind == "sell"
                        and q.buy_amount > best_quote.buy_amount
                    )
                    or (
                        order.kind == "buy"
                        and q.sell_amount < best_quote.sell_amount
                    )
                )
                if is_better:
                    best_quote, best_source = q, src

            if best_quote is None or best_source is None:
                continue

            # Emit the trade + interaction.
            if order.kind == "sell":
                executed_amount = order.sell_amount
                executed_buy = best_quote.buy_amount
                executed_sell = order.sell_amount
            else:
                executed_amount = order.buy_amount
                executed_buy = order.buy_amount
                executed_sell = best_quote.sell_amount

            # Same CIP-67 register-first guard as the V3-batched path.
            # V2 fallback orders are particularly likely to trip this since
            # they're often shared-token with the V3 fills above (USDC is
            # the common denominator for both venue families).
            if not self._register_prices(
                prices, order,
                executed_buy=executed_buy,
                executed_sell=executed_sell,
            ):
                continue
            trades.append(
                Trade(
                    kind="fulfillment",
                    order_uid=order.uid,
                    executed_amount=executed_amount,
                )
            )
            interaction = best_source.encode_interaction(
                best_quote, self._gpv2_settlement
            )
            intra_interactions.append(interaction.to_gpv2_dict())
            filled_uids.add(order.uid)
            log.info(
                "router_v2_fallback_emitted",
                auction_id=auction.id,
                order_uid=order.uid,
                source=best_source.name,
            )

    # ── Interaction encoding ──────────────────────────────────────────────────

    def _encode_path_interaction(
        self,
        path: V3Path,
        *,
        executed_sell: int,
        executed_buy: int,
        deadline: int,
    ) -> Interaction:
        """Encode the winning V3 path into a settle-able GPv2 Interaction.

        Lives here (not in V3Source) so the v3_only_batched path can encode
        directly from its V3BatchedQuote pick without going through the
        LiquidityAggregator round-trip. Both call sites share the same
        ``src.encoder.v3.encode_v3_swap`` dispatch — slippage math and the
        single/multi/sell/buy table are defined once.
        """
        return encode_v3_swap(
            token_in=path.token_in,
            token_out=path.token_out,
            fee_in=path.fee_tier_in,
            intermediate=path.intermediate,
            fee_out=path.fee_tier_out,
            exact_output=path.exact_output,
            executed_sell=executed_sell,
            executed_buy=executed_buy,
            recipient=self._gpv2_settlement,
            deadline=deadline,
            slippage_bps=self._slippage_bps,
            router_address=self._v3_router_address,
        )

    # ── Shared clearing-price logic ───────────────────────────────────────────

    @staticmethod
    def _register_prices(
        prices: dict[str, int],
        order: Order,
        *,
        executed_buy: int,
        executed_sell: int,
    ) -> bool:
        """Register clearing prices reflecting the AMM-realised execution ratio.

        Uses ``cp_sell / cp_buy = executed_buy / executed_sell`` (the rate the
        AMM actually quoted) so that CIP-14 scoring's ``bought = executed *
        cp_sell / cp_buy`` reproduces what the user really receives.

        DO NOT substitute ``token.reference_price`` here even when available:
        reference prices are oracle snapshots, and for orders with limits far
        from oracle (e.g. partiallyFillable BUY at +10% slack) the surplus
        formula collapses to ``signed_buy_at_oracle_rate − signed_buy_limit``,
        which is the order's OTM-headroom — surplus the AMM never realised.
        Bug observed 2026-05-24: a 1-WBTC BUY with USDC limit 22 % above
        oracle persisted score=6.6 ETH per fill, dominated by 134 fills in
        24 h, driving an €67M/Mo phantom projection. See
        ``docs/superpowers/specs/2026-05-26-router-and-logging-followups.md``.

        Returns ``True`` when the rates were registered (or were already
        present at consistent values); ``False`` when a previously-seen
        token's clearing price would conflict with this order's rate.
        Callers must drop the offending trade + interaction on ``False``
        — submitting a Solution with inconsistent prices for the same
        token guarantees a ``settle()`` revert (CIP-67 enforces a single
        uniform clearing price per token across the whole batch).

        Implements Option A from
        ``docs/archive/specs/2026-05-26-router-and-logging-followups.md`` §1:
        first-rate enforcement with explicit drop on mismatch.
        """
        # Forensic log: surplus rate (AMM clearing vs order's limit ratio).
        # >100bps means the order's limit price sits well off-market — either
        # a genuine stale loose-limit order (legit arb capture) or a thinly
        # liquid V3 quote that wouldn't actually settle. Aggregate analytics
        # already deduplicate persistent orders, but per-fill visibility
        # helps debug suspect quote sources before they show up in win-rate
        # projections.
        if order.sell_amount and order.buy_amount and executed_sell and executed_buy:
            numerator = executed_buy * order.sell_amount - order.buy_amount * executed_sell
            denominator = order.buy_amount * executed_sell
            if denominator > 0 and numerator > 0:
                surplus_bps = numerator * 10_000 // denominator
                if surplus_bps > 100:
                    log.info(
                        "router_high_surplus_observed",
                        order_uid=order.uid,
                        surplus_bps=surplus_bps,
                        kind=order.kind,
                    )

        # CIP-67 invariant: ONE clearing price per token across the whole
        # batch. If a previous trade in this Solution already registered a
        # different price for either of our tokens, our AMM-realised rate
        # is incompatible — we must drop this trade rather than emit
        # inconsistent prices that would revert at settlement.
        existing_sell = prices.get(order.sell_token)
        existing_buy = prices.get(order.buy_token)
        if existing_sell is not None and existing_sell != executed_buy:
            log.info(
                "router_clearing_price_conflict",
                order_uid=order.uid,
                token=order.sell_token,
                existing=str(existing_sell),
                attempted=str(executed_buy),
            )
            return False
        if existing_buy is not None and existing_buy != executed_sell:
            log.info(
                "router_clearing_price_conflict",
                order_uid=order.uid,
                token=order.buy_token,
                existing=str(existing_buy),
                attempted=str(executed_sell),
            )
            return False
        prices[order.sell_token] = executed_buy
        prices[order.buy_token] = executed_sell
        return True
