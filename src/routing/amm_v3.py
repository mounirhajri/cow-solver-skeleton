"""UniV3 quoter via QuoterV2 contract.

Rather than porting Uniswap V3 tick math to Python (notorious edge cases),
we delegate to the on-chain QuoterV2 via Multicall3-batched eth_call. One
contract call per (token_in, token_out, fee_tier) candidate. With Multicall3
we batch all fee tiers in one RPC round-trip, well within latency budget.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_abi import decode, encode

from src.routing.multicall import Call, Multicall3

# Arbitrum
QUOTER_V2_ADDRESS = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"

# keccak256("quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4]
# = c6a5026a  (verified on Arbiscan)
QUOTE_EXACT_INPUT_SINGLE_SELECTOR = "c6a5026a"

FEE_TIERS: tuple[int, ...] = (100, 500, 3000, 10000)


@dataclass(frozen=True)
class V3Quote:
    fee_tier: int
    amount_out: int
    sqrt_price_x96_after: int
    initialized_ticks_crossed: int
    gas_estimate: int


def _encode_quote_input_single(
    token_in: str,
    token_out: str,
    amount_in: int,
    fee: int,
    sqrt_price_limit_x96: int = 0,
) -> str:
    """Encode QuoteExactInputSingleParams calldata."""
    encoded = encode(
        ["(address,address,uint256,uint24,uint160)"],
        [(token_in, token_out, amount_in, fee, sqrt_price_limit_x96)],
    )
    return "0x" + QUOTE_EXACT_INPUT_SINGLE_SELECTOR + encoded.hex()


def _decode_quote_output(data: bytes) -> V3Quote | None:
    """Decode (uint256, uint160, uint32, uint256) from return data."""
    if len(data) < 128:
        return None
    try:
        amount_out, sqrt_price, ticks_crossed, gas_estimate = decode(
            ["uint256", "uint160", "uint32", "uint256"], data
        )
    except Exception:  # noqa: BLE001
        return None
    return V3Quote(
        fee_tier=0,  # filled in by caller
        amount_out=int(amount_out),
        sqrt_price_x96_after=int(sqrt_price),
        initialized_ticks_crossed=int(ticks_crossed),
        gas_estimate=int(gas_estimate),
    )


async def quote_v3_all_fee_tiers(
    multicall: Multicall3,
    token_in: str,
    token_out: str,
    amount_in: int,
    fee_tiers: tuple[int, ...] = FEE_TIERS,
    quoter_address: str = QUOTER_V2_ADDRESS,
) -> list[V3Quote]:
    """Quote token_in → token_out across all fee tiers, in one Multicall round-trip.

    Returns a list of successful quotes (omits fee tiers where the pool
    doesn't exist or reverts). Empty list means no V3 path found.
    """
    calls = [
        Call(
            target=quoter_address,
            call_data=_encode_quote_input_single(token_in, token_out, amount_in, fee),
            allow_failure=True,
        )
        for fee in fee_tiers
    ]
    results = await multicall.aggregate(calls)
    quotes: list[V3Quote] = []
    for fee, r in zip(fee_tiers, results, strict=True):
        if not r.success:
            continue
        decoded = _decode_quote_output(r.return_data)
        if decoded is None or decoded.amount_out == 0:
            continue
        quotes.append(
            V3Quote(
                fee_tier=fee,
                amount_out=decoded.amount_out,
                sqrt_price_x96_after=decoded.sqrt_price_x96_after,
                initialized_ticks_crossed=decoded.initialized_ticks_crossed,
                gas_estimate=decoded.gas_estimate,
            )
        )
    return quotes


async def best_v3_quote(
    multicall: Multicall3,
    token_in: str,
    token_out: str,
    amount_in: int,
) -> V3Quote | None:
    """Best V3 quote (highest amount_out) across all fee tiers, or None."""
    quotes = await quote_v3_all_fee_tiers(multicall, token_in, token_out, amount_in)
    if not quotes:
        return None
    return max(quotes, key=lambda q: q.amount_out)
