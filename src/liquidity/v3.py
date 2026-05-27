"""Uniswap V3 LiquiditySource.

Wraps the existing batched V3 quoting pipeline (``src/routing/v3_batched.py``)
behind the ``LiquiditySource`` protocol. The quote step builds every
candidate path for the requested swap (direct via each fee tier + 2-hop
via each configured intermediate) and picks the best one. The encode step
turns the winning path into a settle-able V3 SwapRouter call.

Why fan paths *inside* the source instead of letting the aggregator do it:
V3 fee-tier selection and intermediate-routing are V3-specific concerns —
1inch, Bebop etc. don't know what a 0.05% pool is. Keeping the path search
inside V3Source means the aggregator stays simple (one Quote per source).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from src.encoder.interactions import Interaction
from src.encoder.v3_calldata import (
    encode_exact_input_single,
    encode_exact_output_single,
)
from src.encoder.v3_path import (
    encode_exact_input,
    encode_exact_output,
    pack_v3_path,
)
from src.liquidity.base import Quote, SwapRequest
from src.log import get_logger
from src.routing.amm_v3 import FEE_TIERS, QUOTER_V2_ADDRESS
from src.routing.multicall import Multicall3
from src.routing.v3_batched import V3BatchedQuote, V3Path, batched_v3_quote

log = get_logger(__name__)

# Deadline grace window encoded into the swap call. Long enough to survive
# normal block-time variance between when the driver submits and when the
# tx mines (Arbitrum: ~250ms blocks, but bundle propagation can take seconds).
# Short enough that a stuck/delayed settlement reverts cleanly rather than
# executing at stale pool state. 60s is the typical solver-industry choice.
_SWAP_DEADLINE_SECONDS = 60

# Approximate gas costs for use in surplus/cost trade-off in the solver.
# Real numbers fluctuate with pool state but these are good enough for
# break-even gating. Verified against typical Arbitrum traces.
_GAS_SINGLE_HOP = 150_000
_GAS_TWO_HOP = 250_000


@dataclass(frozen=True)
class _V3RouteMetadata:
    """V3-specific routing data carried inside Quote.route_metadata.

    Stored as a dataclass instance under the key ``"v3_route"`` so callers
    (just V3Source.encode_interaction) get type-checked access instead of
    fishing through an ``Any`` dict.
    """

    path: V3Path
    deadline: int


class V3Source:
    """LiquiditySource for Uniswap V3 (and V3 forks that share the SwapRouter ABI)."""

    name = "v3"

    def __init__(
        self,
        multicall: Multicall3,
        router_address: str,
        intermediate_tokens: list[str],
        slippage_bps: int = 50,
        quoter_address: str = QUOTER_V2_ADDRESS,
        fee_tiers: tuple[int, ...] = FEE_TIERS,
    ) -> None:
        self._multicall = multicall
        self._router_address = router_address
        self._intermediate_tokens = intermediate_tokens
        self._slippage_bps = slippage_bps
        self._quoter_address = quoter_address
        self._fee_tiers = fee_tiers

    async def quote(self, req: SwapRequest, timeout_ms: int) -> Quote | None:
        paths = self._candidate_paths(req)
        if not paths:
            return None

        try:
            quotes = await asyncio.wait_for(
                batched_v3_quote(self._multicall, paths, self._quoter_address),
                timeout=timeout_ms / 1000,
            )
        except TimeoutError:
            log.info("v3_quote_timeout", token_in=req.sell_token, token_out=req.buy_token)
            return None
        except Exception as e:  # noqa: BLE001
            # Multicall3 RPC failures, decode errors, etc. The Protocol
            # forbids raising — return None and let the aggregator move on.
            log.warning("v3_quote_error", error=str(e))
            return None

        best = self._pick_best(quotes, req.kind)
        if best is None or best.amount_out == 0:
            return None

        return self._to_quote(req, best)

    def encode_interaction(self, quote: Quote, recipient: str) -> Interaction:
        meta = quote.route_metadata.get("v3_route")
        if not isinstance(meta, _V3RouteMetadata):
            raise ValueError(
                "Quote.route_metadata['v3_route'] missing or wrong type — "
                "encode_interaction can only consume Quotes produced by V3Source.quote()"
            )

        path = meta.path
        deadline = meta.deadline

        if path.intermediate is None:
            calldata = self._encode_single_hop(path, quote, recipient, deadline)
        else:
            calldata = self._encode_multi_hop(path, quote, recipient, deadline)

        return Interaction(target=self._router_address, value=0, call_data=calldata)

    def required_allowances(self, quote: Quote) -> list[tuple[str, str]]:
        meta = quote.route_metadata["v3_route"]
        assert isinstance(meta, _V3RouteMetadata)
        return [(meta.path.token_in, self._router_address)]

    async def health_check(self) -> bool:
        # No cheap "ping" exists on a Multicall3 endpoint; the underlying
        # RPC's health is the aggregator's concern. A V3-specific check
        # would have to do a real quote, which costs latency we don't want
        # to pay on every batch. Default to optimistic.
        return True

    # ─────────────────────────────────────────────────────────────────
    # Internals
    # ─────────────────────────────────────────────────────────────────

    def _candidate_paths(self, req: SwapRequest) -> list[V3Path]:
        """Build every candidate V3Path the on-chain quoter will be asked about.

        For each fee tier: one direct single-hop path. For each fee tier × tier
        × intermediate: one 2-hop path. With 4 tiers and 1 intermediate that
        is 4 + 16 = 20 paths per swap. Multicall3 batches them all in one RPC
        round-trip via batched_v3_quote.
        """
        paths: list[V3Path] = []
        exact_output = req.kind == "buy"
        # The "fixed" amount the quoter takes: sell_amount for exactInput,
        # buy_amount for exactOutput. V3Path packs both into the same field.
        fixed_amount = req.buy_amount if exact_output else req.sell_amount
        if fixed_amount == 0:
            return []

        # uid carried for logging/correlation; the source-internal flow
        # doesn't otherwise need it.
        uid = f"{req.sell_token[:6]}_{req.buy_token[:6]}"

        # Direct single-hop, one per fee tier.
        for fee in self._fee_tiers:
            paths.append(
                V3Path(
                    order_uid=uid,
                    token_in=req.sell_token,
                    token_out=req.buy_token,
                    amount_in=fixed_amount,
                    fee_tier_in=fee,
                    exact_output=exact_output,
                )
            )

        # 2-hop via each intermediate, all fee tier combinations.
        for intermediate in self._intermediate_tokens:
            if (
                intermediate.lower() == req.sell_token.lower()
                or intermediate.lower() == req.buy_token.lower()
            ):
                # Same-token intermediate degenerates to single-hop.
                continue
            for fee_in in self._fee_tiers:
                for fee_out in self._fee_tiers:
                    paths.append(
                        V3Path(
                            order_uid=uid,
                            token_in=req.sell_token,
                            token_out=req.buy_token,
                            amount_in=fixed_amount,
                            fee_tier_in=fee_in,
                            intermediate=intermediate,
                            fee_tier_out=fee_out,
                            exact_output=exact_output,
                        )
                    )

        return paths

    @staticmethod
    def _pick_best(
        quotes: list[V3BatchedQuote], kind: str
    ) -> V3BatchedQuote | None:
        """Pick the V3BatchedQuote that maximises user utility.

        For ``sell`` orders the variable side is amount_out (buy amount) and
        we want it maximised. For ``buy`` orders the variable side is
        amount_in (sell amount) and we want it minimised. Zero-amount quotes
        signal a reverted call (no liquidity on that path) and are excluded.
        """
        viable = [q for q in quotes if q.amount_out > 0]
        if not viable:
            return None
        if kind == "sell":
            return max(viable, key=lambda q: q.amount_out)
        return min(viable, key=lambda q: q.amount_out)

    def _to_quote(self, req: SwapRequest, best: V3BatchedQuote) -> Quote:
        deadline = int(time.time()) + _SWAP_DEADLINE_SECONDS
        gas_estimate = (
            _GAS_TWO_HOP if best.path.intermediate is not None else _GAS_SINGLE_HOP
        )
        if req.kind == "sell":
            sell_amount = req.sell_amount
            buy_amount = best.amount_out
        else:
            sell_amount = best.amount_out
            buy_amount = req.buy_amount

        return Quote(
            source=self.name,
            sell_amount=sell_amount,
            buy_amount=buy_amount,
            # AMM pools have no signed-quote expiry. Use the swap-call
            # deadline so downstream consumers don't get a "valid_forever"
            # signal that would mask real timing constraints.
            valid_until=deadline,
            route_metadata={
                "v3_route": _V3RouteMetadata(path=best.path, deadline=deadline),
            },
            gas_estimate=gas_estimate,
        )

    def _encode_single_hop(
        self, path: V3Path, quote: Quote, recipient: str, deadline: int
    ) -> bytes:
        if path.exact_output:
            return encode_exact_output_single(
                token_in=path.token_in,
                token_out=path.token_out,
                fee=path.fee_tier_in,
                recipient=recipient,
                deadline=deadline,
                amount_out=quote.buy_amount,
                amount_in_maximum=_apply_slippage_up(quote.sell_amount, self._slippage_bps),
            )
        return encode_exact_input_single(
            token_in=path.token_in,
            token_out=path.token_out,
            fee=path.fee_tier_in,
            recipient=recipient,
            deadline=deadline,
            amount_in=quote.sell_amount,
            amount_out_minimum=_apply_slippage_down(quote.buy_amount, self._slippage_bps),
        )

    def _encode_multi_hop(
        self, path: V3Path, quote: Quote, recipient: str, deadline: int
    ) -> bytes:
        assert path.intermediate is not None and path.fee_tier_out is not None
        packed = pack_v3_path(
            tokens=[path.token_in, path.intermediate, path.token_out],
            fees=[path.fee_tier_in, path.fee_tier_out],
        )
        if path.exact_output:
            return encode_exact_output(
                path=packed,
                recipient=recipient,
                deadline=deadline,
                amount_out=quote.buy_amount,
                amount_in_maximum=_apply_slippage_up(quote.sell_amount, self._slippage_bps),
            )
        return encode_exact_input(
            path=packed,
            recipient=recipient,
            deadline=deadline,
            amount_in=quote.sell_amount,
            amount_out_minimum=_apply_slippage_down(quote.buy_amount, self._slippage_bps),
        )


def _apply_slippage_down(amount: int, slippage_bps: int) -> int:
    """Reduce ``amount`` by ``slippage_bps`` — used for amountOutMinimum on
    sell-kind swaps. We accept up to this much less buy_token than quoted."""
    return amount * (10_000 - slippage_bps) // 10_000


def _apply_slippage_up(amount: int, slippage_bps: int) -> int:
    """Increase ``amount`` by ``slippage_bps`` — used for amountInMaximum on
    buy-kind swaps. We accept consuming up to this much more sell_token."""
    # Round up so the cap doesn't become tighter than intended by integer truncation.
    return (amount * (10_000 + slippage_bps) + 9_999) // 10_000
