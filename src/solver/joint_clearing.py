"""Joint clearing strategy: batch multiple same-pair orders at a shared price.

Current gap
-----------
RouterSolver submits at most ONE trade per solution when orders share a token.
The CIP-67 invariant requires a single clearing price per token, so two WETH→USDC
orders whose individual AMM quotes returned different rates fail the compatibility
check and only the best-rate order is included.

Joint clearing fix
------------------
Instead of quoting each order independently, quote the **combined** sell amount
for same-pair order groups in one AMM call.  If the combined rate satisfies every
individual limit, all orders in the group share the combined clearing price and
settle in a single AMM interaction.

CIP-14 impact
-------------
The score is additive: surplus(A) + surplus(B) > max(surplus(A), surplus(B)).
Batching raises the total score vs. single-order RouterSolver submissions —
especially for same-pair whale orders (bucket 4/5) where AMM price impact pushes
individual rates off-market but a combined quote is still above each limit.

Execution model
---------------
One Multicall3 round-trip:
  • ``N × (fee_tiers + intermediates)`` paths for individual orders (reused from
    RouterSolver for fallback + singleton processing)
  • ``M × (fee_tiers + intermediates)`` extra paths for combined group amounts
    where M = number of groups with ≥ min_group_size orders

The combined-amount quote uses a synthetic order_uid (``__jc_group__…``) so
``_select_best_quote_per_order`` never confuses group quotes with individual ones.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from src.config import settings
from src.log import get_logger
from src.models.auction import Auction
from src.models.order import Order
from src.models.solution import Solution, Trade
from src.routing.amm_v3 import FEE_TIERS
from src.routing.multicall import Multicall3
from src.routing.v3_batched import V3BatchedQuote, V3Path, batched_v3_quote
from src.solver.base import NoSolution
from src.solver.router import (
    RouterSolver,
    _SWAP_DEADLINE_SECONDS,
    _expected_surplus_sort_key,
)

if TYPE_CHECKING:
    from edge.matching.ghost_detector import GhostDetector

log = get_logger(__name__)

_DEFAULT_MAX_ORDERS = 50
_DEFAULT_MAX_CONCURRENT = 20
_DEFAULT_STRATEGY_TIMEOUT = 13.0
_DEFAULT_MIN_GROUP_SIZE = 2

# Synthetic prefix that marks combined-group quote paths so they are never
# confused with individual-order paths by the quote-selection helpers.
_GROUP_UID_PREFIX = "__jc_group__"


def _ceil_div(a: int, b: int) -> int:
    if b <= 0:
        return 0
    return (a + b - 1) // b


def _group_key(order: Order) -> tuple[str, str]:
    return (order.sell_token.lower(), order.buy_token.lower())


def _all_limits_satisfied(
    orders: list[Order],
    combined_sell: int,
    combined_buy: int,
) -> bool:
    """True iff every sell order clears its limit at the combined clearing price.

    At clearing prices {sell_token: combined_buy, buy_token: combined_sell}:
        received_i = ceil(sell_amount_i * combined_buy / combined_sell)
    Must be >= buy_amount_i for every order i.
    """
    if combined_sell <= 0 or combined_buy <= 0:
        return False
    for order in orders:
        received = _ceil_div(order.sell_amount * combined_buy, combined_sell)
        if received < order.buy_amount:
            return False
    return True


def _best_joint_quote(
    quotes: list[V3BatchedQuote],
    sell_tok: str,
    buy_tok: str,
) -> V3BatchedQuote | None:
    """Best non-zero combined-amount quote for a given token pair."""
    best: V3BatchedQuote | None = None
    for q in quotes:
        if not q.path.order_uid.startswith(_GROUP_UID_PREFIX):
            continue
        if q.path.token_in.lower() != sell_tok or q.path.token_out.lower() != buy_tok:
            continue
        if q.amount_out <= 0:
            continue
        if best is None or q.amount_out > best.amount_out:
            best = q
    return best


def _build_group_paths(
    sell_tok: str,
    buy_tok: str,
    combined_sell: int,
    intermediates: list[str],
) -> list[V3Path]:
    """V3 candidate paths for the combined sell amount of a group."""
    paths: list[V3Path] = []
    uid_base = f"{_GROUP_UID_PREFIX}{sell_tok[:8]}_{buy_tok[:8]}"
    for fee in FEE_TIERS:
        paths.append(V3Path(
            order_uid=f"{uid_base}_f{fee}",
            token_in=sell_tok,
            token_out=buy_tok,
            amount_in=combined_sell,
            fee_tier_in=fee,
            exact_output=False,
        ))
        for mid in intermediates:
            if mid.lower() in (sell_tok, buy_tok):
                continue
            paths.append(V3Path(
                order_uid=f"{uid_base}_f{fee}_m{mid[:8]}",
                token_in=sell_tok,
                token_out=buy_tok,
                amount_in=combined_sell,
                fee_tier_in=fee,
                intermediate=mid,
                fee_tier_out=fee,
                exact_output=False,
            ))
    return paths


class JointClearingSolver:
    """Batch-clears multiple same-pair orders at one shared AMM clearing price.

    Wraps RouterSolver's V3 batched quote machinery.  Adds combined-amount
    quotes for every group of ≥ min_group_size same-pair sell orders.  When a
    combined quote satisfies all group members' limits, emits a single AMM
    interaction for the combined amount (saving gas) and includes all trades —
    raising the total CIP-14 score vs. single-order submissions.

    Orders not in a qualifying group fall through to the standard
    RouterSolver per-order path.
    """

    name = "joint-clearing"

    def __init__(
        self,
        multicall: Multicall3,
        intermediates: list[str],
        max_orders: int = _DEFAULT_MAX_ORDERS,
        max_concurrent: int = _DEFAULT_MAX_CONCURRENT,
        strategy_timeout: float = _DEFAULT_STRATEGY_TIMEOUT,
        min_group_size: int = _DEFAULT_MIN_GROUP_SIZE,
        ghost_detector: "GhostDetector | None" = None,
        gpv2_settlement: str | None = None,
        v3_router_address: str | None = None,
        slippage_bps: int | None = None,
    ) -> None:
        self._multicall = multicall
        self._intermediates = intermediates
        self._max_orders = max_orders
        self._max_concurrent = max_concurrent
        self._min_group_size = min_group_size
        self.timeout: float = strategy_timeout
        self._ghost_detector = ghost_detector
        # Reuse RouterSolver for individual-order paths and shared helpers.
        self._router = RouterSolver(
            multicall=multicall,
            intermediates=intermediates,
            max_orders=max_orders,
            max_concurrent=max_concurrent,
            strategy_timeout=strategy_timeout,
            ghost_detector=ghost_detector,
            gpv2_settlement=gpv2_settlement,
            v3_router_address=v3_router_address,
            slippage_bps=slippage_bps,
        )

    async def solve(self, auction: Auction) -> Solution | NoSolution:
        assert auction.id is not None, "joint-clearing must not run for quote-only auctions"

        # ── 1. Ghost filter ──────────────────────────────────────────────────
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
                    "joint_clearing_ghost_filter",
                    auction_id=auction.id,
                    n_filtered=n_before - len(candidate_orders),
                )

        # ── 2. Select top-N by surplus headroom ──────────────────────────────
        orders = sorted(
            candidate_orders,
            key=lambda o: _expected_surplus_sort_key(o, auction),
            reverse=True,
        )[: self._max_orders]

        if not orders:
            return NoSolution()

        # ── 3. Group non-partial sell orders by token pair ───────────────────
        groups: dict[tuple[str, str], list[Order]] = defaultdict(list)
        for order in orders:
            if order.kind == "sell" and not order.partially_fillable:
                groups[_group_key(order)].append(order)

        joint_pairs = {k: v for k, v in groups.items() if len(v) >= self._min_group_size}
        grouped_uids: set[str] = {
            o.uid for group in joint_pairs.values() for o in group
        }

        # ── 4. Build all paths in one batch ──────────────────────────────────
        individual_paths = self._router._build_v3_candidate_paths(orders, auction.tokens)
        group_paths: list[V3Path] = []
        group_combined_sells: dict[tuple[str, str], int] = {}

        for pair, group in joint_pairs.items():
            sell_tok, buy_tok = pair
            combined = sum(o.sell_amount for o in group)
            group_combined_sells[pair] = combined
            group_paths.extend(
                _build_group_paths(sell_tok, buy_tok, combined, self._intermediates)
            )

        all_paths = individual_paths + group_paths
        if not all_paths:
            return NoSolution()

        try:
            all_quotes = await batched_v3_quote(self._multicall, all_paths)
        except Exception as exc:  # noqa: BLE001
            log.warning("joint_clearing_batched_quote_failed", error=str(exc))
            return NoSolution()

        individual_quotes = [
            q for q in all_quotes
            if not q.path.order_uid.startswith(_GROUP_UID_PREFIX)
        ]

        # ── 5. Build solution ────────────────────────────────────────────────
        trades: list[Trade] = []
        prices: dict[str, int] = {}
        intra_interactions: list[dict[str, object]] = []
        deadline = int(time.time()) + _SWAP_DEADLINE_SECONDS
        filled_uids: set[str] = set()
        n_joint_orders = 0

        # 5a. Process joint groups
        for pair, group in joint_pairs.items():
            sell_tok, buy_tok = pair
            combined_sell = group_combined_sells[pair]

            best = _best_joint_quote(all_quotes, sell_tok, buy_tok)
            if best is None:
                log.info(
                    "joint_clearing_group_no_quote",
                    auction_id=auction.id,
                    sell_token=sell_tok[:10],
                    buy_token=buy_tok[:10],
                    n_orders=len(group),
                )
                continue

            combined_buy = best.amount_out

            if not _all_limits_satisfied(group, combined_sell, combined_buy):
                # Combined rate doesn't cover all limits.  Individuals will be
                # retried in the RouterSolver fallback pass below.
                log.info(
                    "joint_clearing_group_limits_missed",
                    auction_id=auction.id,
                    sell_token=sell_tok[:10],
                    buy_token=buy_tok[:10],
                    n_orders=len(group),
                    combined_sell=combined_sell,
                    combined_buy=combined_buy,
                )
                continue

            # CIP-67 price consistency check against already-registered prices
            existing_sell = prices.get(sell_tok)
            existing_buy = prices.get(buy_tok)
            if (existing_sell is not None and existing_sell != combined_buy) or (
                existing_buy is not None and existing_buy != combined_sell
            ):
                log.info(
                    "joint_clearing_price_conflict",
                    auction_id=auction.id,
                    sell_token=sell_tok[:10],
                    buy_token=buy_tok[:10],
                )
                continue

            prices[sell_tok] = combined_buy
            prices[buy_tok] = combined_sell

            intra_interactions.append(
                self._router._encode_path_interaction(
                    best.path,
                    executed_sell=combined_sell,
                    executed_buy=combined_buy,
                    deadline=deadline,
                ).to_gpv2_dict()
            )
            for order in group:
                trades.append(Trade(
                    kind="fulfillment",
                    order_uid=order.uid,
                    executed_amount=order.sell_amount,
                ))
                filled_uids.add(order.uid)

            n_joint_orders += len(group)
            log.info(
                "joint_clearing_group_settled",
                auction_id=auction.id,
                sell_token=sell_tok[:10],
                buy_token=buy_tok[:10],
                n_orders=len(group),
                combined_sell=str(combined_sell),
                combined_buy=str(combined_buy),
            )

        # 5b. RouterSolver fallback for ungrouped orders and group failures
        unfilled = [o for o in orders if o.uid not in filled_uids]
        if unfilled:
            best_per_order = self._router._select_best_quote_per_order(individual_quotes)
            for order in unfilled:
                if order.kind == "buy":
                    # Buy-order joint clearing deferred; skip for now.
                    continue
                best_q = best_per_order.get(order.uid)
                if best_q is None or best_q.amount_out < order.buy_amount:
                    continue
                if not self._router._register_prices(
                    prices,
                    order,
                    executed_buy=best_q.amount_out,
                    executed_sell=order.sell_amount,
                ):
                    continue
                trades.append(Trade(
                    kind="fulfillment",
                    order_uid=order.uid,
                    executed_amount=order.sell_amount,
                ))
                intra_interactions.append(
                    self._router._encode_path_interaction(
                        best_q.path,
                        executed_sell=order.sell_amount,
                        executed_buy=best_q.amount_out,
                        deadline=deadline,
                    ).to_gpv2_dict()
                )
                filled_uids.add(order.uid)

        if not trades:
            return NoSolution()

        log.info(
            "joint_clearing_solved",
            auction_id=auction.id,
            n_joint=n_joint_orders,
            n_total=len(trades),
            n_interactions=len(intra_interactions),
        )
        return Solution(
            id=int(auction.id),
            prices=prices,
            trades=trades,
            interactions=intra_interactions,
        )
