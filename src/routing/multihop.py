"""1- and 2-hop AMM routing over UniV2-style factories and UniV3."""

from __future__ import annotations

from dataclasses import dataclass

from src.routing.amm_v2 import (
    fetch_reserves,
    find_pool_addresses,
    quote_v2_swap,
)
from src.routing.amm_v3 import QUOTER_V2_ADDRESS, best_v3_quote
from src.routing.multicall import Multicall3


@dataclass(frozen=True)
class HopQuote:
    factory: str  # "sushi" | "camelot" | "uniswap-v3"
    pool: str
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int
    fee_tier: int | None = None  # only set for V3


async def _quote_best_single_hop(
    multicall: Multicall3,
    token_in: str,
    token_out: str,
    amount_in: int,
) -> HopQuote | None:
    """Combine V2 + V3 candidates, return highest amount_out."""
    candidates: list[HopQuote] = []

    # V2: existing logic across known factories
    addresses = await find_pool_addresses(multicall, token_in, token_out)
    if addresses:
        reserves = await fetch_reserves(multicall, addresses)
        token_in_lc = token_in.lower()
        for factory_name, pool_addr in addresses.items():
            rsv = reserves.get(factory_name)
            if rsv is None:
                continue
            if rsv.token0 == token_in_lc:
                reserve_in, reserve_out = rsv.reserve0, rsv.reserve1
            else:
                reserve_in, reserve_out = rsv.reserve1, rsv.reserve0
            out = quote_v2_swap(amount_in, reserve_in, reserve_out)
            if out > 0:
                candidates.append(
                    HopQuote(
                        factory=factory_name,
                        pool=pool_addr,
                        token_in=token_in,
                        token_out=token_out,
                        amount_in=amount_in,
                        amount_out=out,
                        fee_tier=None,
                    )
                )

    # V3: QuoterV2 across all fee tiers in one Multicall round-trip
    v3 = await best_v3_quote(multicall, token_in, token_out, amount_in)
    if v3 is not None:
        candidates.append(
            HopQuote(
                factory="uniswap-v3",
                pool=QUOTER_V2_ADDRESS,
                token_in=token_in,
                token_out=token_out,
                amount_in=amount_in,
                amount_out=v3.amount_out,
                fee_tier=v3.fee_tier,
            )
        )

    if not candidates:
        return None
    return max(candidates, key=lambda c: c.amount_out)


async def quote_best_path(
    multicall: Multicall3,
    token_in: str,
    token_out: str,
    amount_in: int,
    intermediates: list[str],
) -> list[HopQuote] | None:
    """Try direct + each 2-hop via intermediate. Return path with max final amount_out."""
    candidates: list[list[HopQuote]] = []

    direct = await _quote_best_single_hop(multicall, token_in, token_out, amount_in)
    if direct is not None:
        candidates.append([direct])

    for mid in intermediates:
        if mid.lower() in (token_in.lower(), token_out.lower()):
            continue
        h1 = await _quote_best_single_hop(multicall, token_in, mid, amount_in)
        if h1 is None:
            continue
        h2 = await _quote_best_single_hop(multicall, mid, token_out, h1.amount_out)
        if h2 is None:
            continue
        candidates.append([h1, h2])

    if not candidates:
        return None
    return max(candidates, key=lambda path: path[-1].amount_out)
