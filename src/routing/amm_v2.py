"""UniV2 constant-product math + pool discovery.

Pool reserves are read via Multicall3 (getReserves()). Pool address for
a token pair is derived via factory.getPair(token_a, token_b).
"""

from __future__ import annotations

from dataclasses import dataclass

from src.routing.multicall import Call, Multicall3

# Arbitrum
FACTORIES_V2: dict[str, str] = {
    "sushi": "0xc35DADB65012eC5796536bD9864eD8773aBc74C4",
    "camelot": "0x6EcCab422D763aC031210895C81787E87B43A652",
}

# Function selectors
SEL_GET_RESERVES = "0902f1ac"  # getReserves() returns (uint112, uint112, uint32)
SEL_GET_PAIR = "e6a43905"  # getPair(address,address) returns address
SEL_TOKEN0 = "0dfe1681"  # token0() returns address


@dataclass(frozen=True)
class PoolReserves:
    reserve0: int
    reserve1: int
    token0: str  # lower-case
    token1: str


def quote_v2_swap(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int = 30) -> int:
    """UniV2 constant-product swap with fee.

    For sushi and uniswap v2 forks fee_bps=30 (0.30%). Camelot V2 uses
    variable fees but defaults to 30bps for non-stable pools.
    """
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    amount_in_with_fee = amount_in * (10_000 - fee_bps)
    numerator = amount_in_with_fee * reserve_out
    denominator = reserve_in * 10_000 + amount_in_with_fee
    return numerator // denominator


def _encode_get_pair(token_a: str, token_b: str) -> str:
    a = token_a[2:].zfill(64).lower()
    b = token_b[2:].zfill(64).lower()
    return "0x" + SEL_GET_PAIR + a + b


def _encode_token0() -> str:
    return "0x" + SEL_TOKEN0


def _encode_get_reserves() -> str:
    return "0x" + SEL_GET_RESERVES


async def find_pool_addresses(
    multicall: Multicall3, token_a: str, token_b: str
) -> dict[str, str]:
    """For each known factory, return its pool address for (a,b) or skip if zero."""
    calls = [
        Call(target=fact, call_data=_encode_get_pair(token_a, token_b))
        for fact in FACTORIES_V2.values()
    ]
    results = await multicall.aggregate(calls)
    addresses: dict[str, str] = {}
    for (name, _), r in zip(FACTORIES_V2.items(), results, strict=True):
        if not r.success or len(r.return_data) < 32:
            continue
        addr_bytes = r.return_data[-20:]
        addr = "0x" + addr_bytes.hex()
        if int(addr, 16) == 0:
            continue
        addresses[name] = addr
    return addresses


async def fetch_reserves(
    multicall: Multicall3, pools: dict[str, str]
) -> dict[str, PoolReserves]:
    """For each pool, fetch (reserve0, reserve1) + token0."""
    if not pools:
        return {}
    # Two calls per pool: getReserves + token0 (so we know orientation)
    pool_list = list(pools.values())
    calls: list[Call] = []
    for p in pool_list:
        calls.append(Call(target=p, call_data=_encode_get_reserves()))
        calls.append(Call(target=p, call_data=_encode_token0()))
    results = await multicall.aggregate(calls)

    out: dict[str, PoolReserves] = {}
    for (name, _addr), i in zip(pools.items(), range(0, len(results), 2), strict=False):
        rsv = results[i]
        tok = results[i + 1]
        if (
            not rsv.success
            or not tok.success
            or len(rsv.return_data) < 64
            or len(tok.return_data) < 32
        ):
            continue
        # getReserves returns (uint112, uint112, uint32) packed into 3 × 32 bytes
        r0 = int.from_bytes(rsv.return_data[0:32], "big")
        r1 = int.from_bytes(rsv.return_data[32:64], "big")
        t0_bytes = tok.return_data[-20:]
        token0 = "0x" + t0_bytes.hex().lower()
        # Caller knows the pair; token1 = whichever of (a,b) isn't token0
        # but we don't track which pool this is for here. We return PoolReserves
        # with token0 only; caller must derive token1 from their own context.
        # Simpler: include name → reserves mapping with token0 stored.
        out[name] = PoolReserves(
            reserve0=r0,
            reserve1=r1,
            token0=token0,
            token1="",  # caller fills in
        )

    return out
