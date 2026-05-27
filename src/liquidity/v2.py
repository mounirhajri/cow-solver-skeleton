"""UniswapV2-style LiquiditySource.

One instance per ``(router, factory)`` pair — instantiate separately for
Camelot, SushiSwap, etc. The aggregator runs them in parallel. Coupling
each source to a single router keeps ``encode_interaction`` honest: the
on-chain call always uses the router whose pools were quoted.

Multi-hop within this source means the same factory all the way through.
A V2 router's hop logic walks its own factory's ``getPair`` — there's no
mechanism to route hop 1 through Camelot and hop 2 through Sushi inside
one call. Cross-router multi-hop would need two interactions and is out
of scope for Phase 0b.

V2 buy-kind quoting uses the closed-form constant-product inverse rather
than an on-chain ``getAmountsIn`` call — saves a Multicall round-trip and
matches the math the router will execute (modulo the same fee_bps).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from src.encoder.interactions import Interaction
from src.encoder.v2_calldata import (
    encode_swap_exact_tokens_for_tokens,
    encode_swap_tokens_for_exact_tokens,
)
from src.liquidity.base import Quote, SwapRequest
from src.log import get_logger
from src.routing.amm_v2 import PoolReserves, fetch_reserves, quote_v2_swap
from src.routing.multicall import Call, Multicall3

log = get_logger(__name__)

_SWAP_DEADLINE_SECONDS = 60
_GAS_SINGLE_HOP = 120_000   # V2 swap is cheaper than V3 — no tick traversal
_GAS_TWO_HOP = 180_000

# UniV2 / SushiSwap canonical fee: 0.3 %. Camelot volatile pools default to
# the same but have per-pool overrides — Phase 0b uses the canonical
# constant; per-pool fee fetching is Phase 0b.5 territory.
_DEFAULT_FEE_BPS = 30

# selector for ``getPair(address,address)`` on UniV2 factory
_SEL_GET_PAIR = "e6a43905"


@dataclass(frozen=True)
class _V2RouteMetadata:
    """V2 routing payload carried on Quote.route_metadata['v2_route']."""

    path: list[str]  # token addresses, len >= 2 (direct = 2, two-hop = 3)
    deadline: int


class V2Source:
    """LiquiditySource for one UniswapV2-style router + factory pair."""

    def __init__(
        self,
        name: str,
        multicall: Multicall3,
        router_address: str,
        factory_address: str,
        intermediate_tokens: list[str],
        slippage_bps: int = 50,
        fee_bps: int = _DEFAULT_FEE_BPS,
    ) -> None:
        self.name = name
        self._multicall = multicall
        self._router_address = router_address
        self._factory_address = factory_address
        self._intermediate_tokens = intermediate_tokens
        self._slippage_bps = slippage_bps
        self._fee_bps = fee_bps

    async def quote(self, req: SwapRequest, timeout_ms: int) -> Quote | None:
        try:
            return await asyncio.wait_for(self._quote_impl(req), timeout=timeout_ms / 1000)
        except TimeoutError:
            log.info(
                "v2_quote_timeout",
                source=self.name,
                token_in=req.sell_token,
                token_out=req.buy_token,
            )
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("v2_quote_error", source=self.name, error=str(e))
            return None

    def encode_interaction(self, quote: Quote, recipient: str) -> Interaction:
        meta = quote.route_metadata.get("v2_route")
        if not isinstance(meta, _V2RouteMetadata):
            raise ValueError(
                f"Quote.route_metadata['v2_route'] missing or wrong type "
                f"— V2Source({self.name}).encode_interaction can only consume "
                f"its own Quotes"
            )

        path = meta.path
        deadline = meta.deadline
        token_in = path[0]
        token_out = path[-1]

        if quote.source.endswith(":buy"):
            calldata = encode_swap_tokens_for_exact_tokens(
                token_in=token_in,
                token_out=token_out,
                path=path,
                recipient=recipient,
                deadline=deadline,
                amount_out=quote.buy_amount,
                amount_in_maximum=_apply_slippage_up(quote.sell_amount, self._slippage_bps),
            )
        else:
            calldata = encode_swap_exact_tokens_for_tokens(
                token_in=token_in,
                token_out=token_out,
                path=path,
                recipient=recipient,
                deadline=deadline,
                amount_in=quote.sell_amount,
                amount_out_minimum=_apply_slippage_down(quote.buy_amount, self._slippage_bps),
            )

        return Interaction(target=self._router_address, value=0, call_data=calldata)

    def required_allowances(self, quote: Quote) -> list[tuple[str, str]]:
        meta = quote.route_metadata["v2_route"]
        assert isinstance(meta, _V2RouteMetadata)
        return [(meta.path[0], self._router_address)]

    async def health_check(self) -> bool:
        return True

    # ─────────────────────────────────────────────────────────────────
    # Internals
    # ─────────────────────────────────────────────────────────────────

    async def _quote_impl(self, req: SwapRequest) -> Quote | None:
        # Build candidate paths: direct + one 2-hop per intermediate.
        paths: list[list[str]] = [[req.sell_token, req.buy_token]]
        for mid in self._intermediate_tokens:
            mid_lower = mid.lower()
            if mid_lower == req.sell_token.lower() or mid_lower == req.buy_token.lower():
                continue
            paths.append([req.sell_token, mid, req.buy_token])

        # Resolve pool addresses for every hop in every candidate path.
        unique_pairs: dict[tuple[str, str], None] = {}
        for path in paths:
            for i in range(len(path) - 1):
                key = (path[i].lower(), path[i + 1].lower())
                unique_pairs.setdefault(key, None)

        pool_map = await self._lookup_pools(list(unique_pairs.keys()))

        # Fetch reserves for every discovered pool.
        # fetch_reserves wants a dict[label, pool_addr]; we use the pair-key
        # tuple as the label so we can look reserves back up by pair.
        pool_addrs_by_pair: dict[tuple[str, str], str] = {
            pair: addr for pair, addr in pool_map.items() if addr is not None
        }
        if not pool_addrs_by_pair:
            return None
        labels = {f"{pair[0]}|{pair[1]}": addr for pair, addr in pool_addrs_by_pair.items()}
        reserves_by_label = await fetch_reserves(self._multicall, labels)

        # Score each candidate path. Skip a path if any hop has no pool or
        # its math gives 0 (insufficient liquidity).
        best: tuple[list[str], int] | None = None
        for path in paths:
            ok, amount = self._compute_path(
                path, req, pool_addrs_by_pair, reserves_by_label
            )
            if not ok:
                continue
            if best is None or self._is_better(amount, best[1], req.kind):
                best = (path, amount)

        if best is None:
            return None
        winning_path, amount_variable = best

        deadline = int(time.time()) + _SWAP_DEADLINE_SECONDS
        if req.kind == "sell":
            sell_amount = req.sell_amount
            buy_amount = amount_variable
            source_label = self.name
        else:
            sell_amount = amount_variable
            buy_amount = req.buy_amount
            # Carry kind through to encode_interaction; we don't get the
            # SwapRequest there. ":buy" suffix is the simplest discriminator
            # that survives the Quote interface without polluting route_metadata.
            source_label = self.name + ":buy"

        return Quote(
            source=source_label,
            sell_amount=sell_amount,
            buy_amount=buy_amount,
            valid_until=deadline,
            route_metadata={
                "v2_route": _V2RouteMetadata(path=winning_path, deadline=deadline),
            },
            gas_estimate=_GAS_TWO_HOP if len(winning_path) > 2 else _GAS_SINGLE_HOP,
        )

    async def _lookup_pools(
        self, pairs: list[tuple[str, str]]
    ) -> dict[tuple[str, str], str | None]:
        """Resolve ``factory.getPair`` for each ``(a, b)`` pair in one Multicall."""
        if not pairs:
            return {}

        calls = [
            Call(target=self._factory_address, call_data=self._encode_get_pair(a, b))
            for a, b in pairs
        ]
        results = await self._multicall.aggregate(calls)
        out: dict[tuple[str, str], str | None] = {}
        for pair, result in zip(pairs, results, strict=True):
            if not result.success or len(result.return_data) < 32:
                out[pair] = None
                continue
            addr = "0x" + result.return_data[-20:].hex()
            if int(addr, 16) == 0:
                out[pair] = None
            else:
                out[pair] = addr
        return out

    @staticmethod
    def _encode_get_pair(token_a: str, token_b: str) -> str:
        a = token_a[2:].rjust(64, "0").lower()
        b = token_b[2:].rjust(64, "0").lower()
        return "0x" + _SEL_GET_PAIR + a + b

    def _compute_path(
        self,
        path: list[str],
        req: SwapRequest,
        pool_addrs_by_pair: dict[tuple[str, str], str],
        reserves_by_label: dict[str, PoolReserves],
    ) -> tuple[bool, int]:
        """Walk ``path`` hop-by-hop computing the constant-product result.

        For sell-kind: accumulate amount_out across hops.
        For buy-kind: walk backwards accumulating required amount_in.
        Returns ``(viable, amount)`` — viable is False if any hop has no
        pool or yields zero (insufficient liquidity).
        """
        if req.kind == "sell":
            amount = req.sell_amount
            for i in range(len(path) - 1):
                pair_key = (path[i].lower(), path[i + 1].lower())
                pool_addr = pool_addrs_by_pair.get(pair_key)
                if pool_addr is None:
                    return False, 0
                label = f"{pair_key[0]}|{pair_key[1]}"
                reserves = reserves_by_label.get(label)
                if reserves is None:
                    return False, 0
                reserve_in, reserve_out = self._orient_reserves(reserves, path[i])
                amount = quote_v2_swap(amount, reserve_in, reserve_out, fee_bps=self._fee_bps)
                if amount == 0:
                    return False, 0
            return True, amount
        # buy-kind — walk backwards.
        amount = req.buy_amount
        for i in range(len(path) - 1, 0, -1):
            pair_key = (path[i - 1].lower(), path[i].lower())
            pool_addr = pool_addrs_by_pair.get(pair_key)
            if pool_addr is None:
                return False, 0
            label = f"{pair_key[0]}|{pair_key[1]}"
            reserves = reserves_by_label.get(label)
            if reserves is None:
                return False, 0
            reserve_in, reserve_out = self._orient_reserves(reserves, path[i - 1])
            amount = _get_amount_in(amount, reserve_in, reserve_out, self._fee_bps)
            if amount == 0:
                return False, 0
        return True, amount

    @staticmethod
    def _orient_reserves(
        reserves: PoolReserves, token_in: str
    ) -> tuple[int, int]:
        """Return ``(reserve_in, reserve_out)`` matching the swap direction.

        UniV2 pools store reserves keyed to the lexicographic ``token0`` —
        we ask which of (reserve0, reserve1) is the input side and flip if
        needed.
        """
        if reserves.token0 == token_in.lower():
            return reserves.reserve0, reserves.reserve1
        return reserves.reserve1, reserves.reserve0

    @staticmethod
    def _is_better(amount: int, current_best: int, kind: str) -> bool:
        if kind == "sell":
            return amount > current_best
        return amount < current_best


def _get_amount_in(
    amount_out: int, reserve_in: int, reserve_out: int, fee_bps: int
) -> int:
    """UniV2 closed-form inverse of constant product (for buy-kind quoting).

    Matches the on-chain ``getAmountIn`` formula:

        amount_in = (reserve_in * amount_out * 10000)
                  / ((reserve_out - amount_out) * (10000 - fee_bps))
                  + 1   # +1 for round-up (the on-chain code is integer math)

    Returns 0 when ``amount_out >= reserve_out`` (insufficient liquidity).
    """
    if amount_out <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    if amount_out >= reserve_out:
        return 0
    numerator = reserve_in * amount_out * 10_000
    denominator = (reserve_out - amount_out) * (10_000 - fee_bps)
    return numerator // denominator + 1


def _apply_slippage_down(amount: int, slippage_bps: int) -> int:
    """Reduce ``amount`` by ``slippage_bps`` — for amountOutMinimum on sell-kind."""
    return amount * (10_000 - slippage_bps) // 10_000


def _apply_slippage_up(amount: int, slippage_bps: int) -> int:
    """Increase ``amount`` by ``slippage_bps`` — for amountInMaximum on buy-kind."""
    return (amount * (10_000 + slippage_bps) + 9_999) // 10_000
