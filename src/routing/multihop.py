"""1- and 2-hop AMM routing over UniV2-style factories."""

from __future__ import annotations

from dataclasses import dataclass

from src.routing.amm_v2 import (
    fetch_reserves,
    find_pool_addresses,
    quote_v2_swap,
)
from src.routing.multicall import Multicall3


@dataclass(frozen=True)
class HopQuote:
    factory: str  # "sushi" | "camelot"
    pool: str
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int


async def _quote_best_single_hop(
    multicall: Multicall3,
    token_in: str,
    token_out: str,
    amount_in: int,
) -> HopQuote | None:
    """Find best (highest out) single-hop quote across factories."""
    addresses = await find_pool_addresses(multicall, token_in, token_out)
    if not addresses:
        return None
    reserves = await fetch_reserves(multicall, addresses)
    if not reserves:
        return None

    best: HopQuote | None = None
    for factory_name, pool_addr in addresses.items():
        rsv = reserves.get(factory_name)
        if rsv is None:
            continue
        # Orient: if token_in matches token0, reserve_in=reserve0
        token_in_lc = token_in.lower()
        if rsv.token0 == token_in_lc:
            reserve_in, reserve_out = rsv.reserve0, rsv.reserve1
        else:
            reserve_in, reserve_out = rsv.reserve1, rsv.reserve0
        out = quote_v2_swap(amount_in, reserve_in, reserve_out)
        if out == 0:
            continue
        if best is None or out > best.amount_out:
            best = HopQuote(
                factory=factory_name,
                pool=pool_addr,
                token_in=token_in,
                token_out=token_out,
                amount_in=amount_in,
                amount_out=out,
            )
    return best


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
