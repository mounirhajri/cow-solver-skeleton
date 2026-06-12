"""Manual smoke test: live-quote 100 units through every hardcoded Curve pool.

Run inside the cow-solver container:
    python -m scripts.smoke_curve_quote

For each pool in ``CURVE_POOLS`` this quotes 100 units (scaled by the live
on-chain ``decimals()`` of the input coin) in BOTH directions — for the
2pool that is 100 USDC.e→USDT and 100 USDT→USDC.e; for the NG pool 100
native-USDC→USDT and back — and prints the raw + human amounts. This is the
on-server verification gate before trusting the hardcoded pool table: a
sane stable-pool quote returns ≈100 (within fees + peg drift). A revert
(amount_out=0) or a wildly off-par amount means a wrong address, wrong coin
order, or wrong selector — fix the table before wiring Curve into the
router.

Exit code is non-zero if any pool/direction returns a non-positive or
grossly off-par quote, so this can gate a deploy.
"""

from __future__ import annotations

import asyncio
import sys

from eth_utils import keccak

from src.config import settings
from src.routing.curve_quoter import (
    CURVE_POOLS,
    CurvePath,
    batched_curve_quote,
)
from src.routing.multicall import Call, Multicall3
from src.routing.rpc import RpcClient

# Sanity window for a 100-unit stable↔stable quote: fees are bps-scale, so
# anything outside ±5% means a broken pool entry, not market conditions.
_PAR_LOW, _PAR_HIGH = 95.0, 105.0

_DECIMALS_SELECTOR = "0x" + keccak(text="decimals()")[:4].hex()


async def _decimals(multicall: Multicall3, tokens: list[str]) -> dict[str, int]:
    calls = [Call(target=t, call_data=_DECIMALS_SELECTOR, allow_failure=False) for t in tokens]
    results = await multicall.aggregate(calls)
    out: dict[str, int] = {}
    for token, res in zip(tokens, results, strict=True):
        assert res.success and len(res.return_data) >= 32, f"decimals() failed for {token}"
        out[token] = int.from_bytes(res.return_data[-32:], "big")
    return out


async def main() -> None:
    rpc = RpcClient(settings.rpc_arbitrum)
    multicall = Multicall3(rpc)

    all_coins = sorted({c for pool in CURVE_POOLS for c in pool.coins})
    decimals = await _decimals(multicall, all_coins)
    for token, d in decimals.items():
        print(f"decimals({token}) = {d}")

    # Both directions through every configured pool, 100 units each.
    paths: list[CurvePath] = []
    for pool in CURVE_POOLS:
        for i, j in ((0, 1), (1, 0)):
            token_in, token_out = pool.coins[i], pool.coins[j]
            paths.append(
                CurvePath(
                    order_uid=f"smoke-{pool.name}-{i}{j}",
                    token_in=token_in,
                    token_out=token_out,
                    amount_in=100 * 10 ** decimals[token_in],
                    pool=pool,
                    i=i,
                    j=j,
                )
            )

    quotes = await batched_curve_quote(multicall, paths)

    ok = True
    for q in quotes:
        p = q.path
        human_in = p.amount_in / 10 ** decimals[p.token_in]
        human_out = q.amount_out / 10 ** decimals[p.token_out]
        verdict = "OK" if _PAR_LOW <= human_out <= _PAR_HIGH else "FAIL"
        if verdict == "FAIL":
            ok = False
        print(
            f"[{verdict}] {p.pool.name} ({p.pool.address}) "
            f"get_dy(i={p.i}, j={p.j}): {human_in:.2f} {p.token_in} -> "
            f"{human_out:.6f} {p.token_out} (raw {q.amount_out})"
        )

    if not ok:
        print(
            "FAIL: at least one pool quoted 0 or grossly off-par — wrong "
            "address / coin order / selector in CURVE_POOLS. Do NOT wire "
            "Curve into the router until this passes."
        )
        sys.exit(1)
    print(f"all {len(quotes)} pool-direction quotes within ±5% of par. POOL TABLE OK.")


if __name__ == "__main__":
    asyncio.run(main())
