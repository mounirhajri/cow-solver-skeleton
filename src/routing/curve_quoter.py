"""Batched Curve stable-pool quoter for hardcoded top Arbitrum pools.

Loss decomposition (2026-06) showed 7/30 winning Arbitrum settlements swap
via Curve on stable pairs — including two of our worst losses — so we quote
the TOP Arbitrum stable pools directly. Pools are HARDCODED (no registry /
factory discovery): deliberate MVP scope, adding a pool is one line in
``CURVE_POOLS``.

Quoting goes through the pool's own ``get_dy(int128 i, int128 j, uint256 dx)``
view — exact-input only (Curve has no exact-output quote), one call per
(order, pool) candidate. Same batching shape as ``v4_quoter``: candidates are
encoded up front and submitted via chunked ``Multicall3.aggregate_resilient``
calls with per-chunk drop tolerance. ``get_dy`` is a cheap pure view (no swap
simulation), so the gas-cap defences are belt-and-braces rather than
load-bearing here.

Every pool address and coin index below was verified against:
  - Curve's official pool API (https://api.curve.finance/api/getPools/arbitrum/
    {main,factory-stable-ng}), which lists address + ordered coins + TVL, and
  - arbitrum.blockscout.com verified contract source
    (/api/v2/smart-contracts/{addr}), and
  - live on-chain ``coins(uint256)`` + ``get_dy`` eth_calls (2026-06-12).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_abi import decode, encode
from eth_utils import keccak

from src.log import get_logger
from src.routing.multicall import Call, CallResult, Multicall3

log = get_logger(__name__)


@dataclass(frozen=True)
class CurvePool:
    """One hardcoded Curve stable pool.

    ``coins`` is the on-chain coin order (``coins(0)``, ``coins(1)``, …) —
    the int128 indices passed to ``get_dy``/``exchange`` are positions in
    this tuple. ``is_ng`` marks stableswap-NG deployments; both generations
    share the ``get_dy``/``exchange`` ABI we use, the flag exists for
    observability and future NG-only features (e.g. ``exchange_received``).
    """

    address: str
    coins: tuple[str, ...]
    name: str
    is_ng: bool


# Arbitrum token addresses (for readability of the pool table below).
_USDC_E = "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8"  # bridged USDC
_USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"  # native USDC
_USDT = "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"  # USDT (USD₮0)

# Hardcoded top Arbitrum stable pools. Adding a pool = one CurvePool line.
#
# 2pool ("2CRV", classic StableSwap, Vyper 0.2.12):
#   address + coin order verified 2026-06-12 via Curve API
#   (api.curve.finance getPools/arbitrum/main: "Curve.fi USDC/USDT",
#   coins[0]=USDC.e, coins[1]=USD₮0) AND on-chain coins(0)/coins(1)
#   eth_call AND blockscout verified source ("StableSwap ... 2 coin pool").
#
# Strategic USD Reserves (stableswap-NG):
#   the native-USDC/USDT stableswap-ng pool the prompt asked to check —
#   verified 2026-06-12 via Curve API (getPools/arbitrum/factory-stable-ng:
#   "Strategic USD Reserves", coins[0]=native USDC, coins[1]=USD₮0,
#   usdTotal ≈ $170k) AND blockscout verified contract name
#   "CurveStableSwapNG" (vyper 0.3.10) AND on-chain coins()/get_dy calls.
CURVE_POOLS: tuple[CurvePool, ...] = (
    CurvePool(
        address="0x7f90122BF0700F9E7e1F688fe926940E8839F353",
        coins=(_USDC_E, _USDT),
        name="2pool",
        is_ng=False,
    ),
    CurvePool(
        address="0x49b720F1Aab26260BEAec93A7BeB5BF2925b2A8F",
        coins=(_USDC, _USDT),
        name="strategic-usd-reserves",
        is_ng=True,
    ),
)

# Solidity/Vyper signature kept verbatim so a grep finds matching artefacts.
# Identical on classic StableSwap and stableswap-NG (verified against both
# pools' blockscout source: `def get_dy(i: int128, j: int128, dx: uint256)`).
_SIG_GET_DY = "get_dy(int128,int128,uint256)"

# keccak256("get_dy(int128,int128,uint256)")[:4] == 0x5e0d443f.
GET_DY_SELECTOR = keccak(text=_SIG_GET_DY)[:4]


@dataclass(frozen=True)
class CurvePath:
    """One candidate exact-input quote against a hardcoded Curve pool.

    ``i``/``j`` are the pool coin indices of ``token_in``/``token_out``.

    ``exact_output`` is ALWAYS False: Curve's ``get_dy`` is exact-input only
    (there is no exact-output quote entry point). The field exists purely for
    duck-typing with V3/V4 paths — the router's ``_select_best_quote_per_order``
    reads ``path.order_uid`` / ``path.amount_in`` / ``path.exact_output`` and
    treats exact-input quotes as "higher amount_out wins".
    """

    order_uid: str
    token_in: str
    token_out: str
    amount_in: int
    pool: CurvePool
    i: int
    j: int
    exact_output: bool = False


@dataclass(frozen=True)
class CurveQuote:
    """Duck-types the V4BatchedQuote surface used in selection (``amount_out``).

    ``gas_estimate`` defaults to 0 — ``get_dy`` returns no gas figure (unlike
    the V4Quoter's simulate-and-revert), so callers must not read it as a
    real estimate.
    """

    path: CurvePath
    amount_out: int
    gas_estimate: int = 0


def make_curve_paths(
    order_uid: str,
    token_in: str,
    token_out: str,
    amount_in: int,
) -> list[CurvePath]:
    """One CurvePath per hardcoded pool that contains BOTH tokens.

    Token matching is case-insensitive (auction orders carry lowercase
    addresses, the pool table carries checksummed ones). Returns ``[]``
    when no pool covers the pair — the common case for non-stable orders.
    """
    tin, tout = token_in.lower(), token_out.lower()
    paths: list[CurvePath] = []
    for pool in CURVE_POOLS:
        coins_lower = [c.lower() for c in pool.coins]
        if tin in coins_lower and tout in coins_lower:
            paths.append(
                CurvePath(
                    order_uid=order_uid,
                    token_in=token_in,
                    token_out=token_out,
                    amount_in=amount_in,
                    pool=pool,
                    i=coins_lower.index(tin),
                    j=coins_lower.index(tout),
                )
            )
    return paths


def _encode_get_dy(path: CurvePath) -> str:
    """Encode ``get_dy(i, j, dx)`` calldata for ``path``. Selector 5e0d443f."""
    args = encode(["int128", "int128", "uint256"], [path.i, path.j, path.amount_in])
    return "0x" + GET_DY_SELECTOR.hex() + args.hex()


def _decode_get_dy_return(data: bytes) -> int | None:
    """Decode the single ``uint256 dy``, else None on short/garbage data."""
    if len(data) < 32:
        return None
    try:
        (dy,) = decode(["uint256"], data)
    except Exception as e:  # noqa: BLE001
        log.debug("curve_quote_decode_failed", error=str(e), nbytes=len(data))
        return None
    return int(dy)


def _build_call(path: CurvePath) -> Call:
    # allow_failure: a pool revert (paused, garbage amount) must yield
    # amount_out=0, not abort the batch.
    return Call(target=path.pool.address, call_data=_encode_get_dy(path), allow_failure=True)


# get_dy is a cheap pure view (no swap simulation), so this chunk size is
# generous headroom rather than a tuned gas-cap bound; mirrors v4_quoter's
# defensive batching so the two quoters degrade identically under RPC stress.
_MAX_CALLS_PER_BATCH = 8


async def batched_curve_quote(
    multicall: Multicall3,
    paths: list[CurvePath],
    *,
    max_calls_per_batch: int = _MAX_CALLS_PER_BATCH,
) -> list[CurveQuote]:
    """Submit paths in batched Multicall3 calls and decode ``get_dy`` returns.

    Returns one CurveQuote per input CurvePath, in the same order. Reverted
    or undecodable calls yield ``amount_out=0`` — the result list always
    matches the input list length so callers can correlate positionally.

    Chunks of ``max_calls_per_batch`` go through ``aggregate_resilient``
    (gas-overflow bisection); a chunk failing with a NON-gas error
    (rate-limit / timeout under RPC contention) is dropped in place —
    positionally aligned zero quotes — rather than aborting the whole pass,
    mirroring ``batched_v4_quote``.
    """
    if not paths:
        return []

    calls = [_build_call(p) for p in paths]
    results: list[Any] = []
    for i in range(0, len(calls), max_calls_per_batch):
        chunk = calls[i : i + max_calls_per_batch]
        try:
            results.extend(await multicall.aggregate_resilient(chunk))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "curve_chunk_dropped", error=str(exc), chunk_start=i, chunk_len=len(chunk)
            )
            results.extend(CallResult(success=False, return_data=b"") for _ in chunk)

    quotes: list[CurveQuote] = []
    for path, result in zip(paths, results, strict=True):
        if not result.success:
            quotes.append(CurveQuote(path=path, amount_out=0))
            continue
        dy = _decode_get_dy_return(result.return_data)
        quotes.append(CurveQuote(path=path, amount_out=dy if dy is not None else 0))
    return quotes
