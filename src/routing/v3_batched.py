"""Batched UniV3 quoter: all candidate paths (cross-orders, cross-fee-tiers,
single- and multi-hop) submitted in ONE Multicall3.aggregate() call.

RouterSolver was issuing ~80 sequential RPC round-trips per auction which
saturated Alchemy free-tier concurrent-connection limits. This module
collapses the entire auction's quote phase into a single eth_call by
encoding every candidate path's `quoteExactInputSingle` or
`quoteExactInput` calldata up front and submitting all of them as one
Multicall3 batch.

V2 is deliberately omitted from this code path — on Arbitrum the V2 edge
is negligible and the RPC budget is the bottleneck.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_abi import decode, encode  # type: ignore[attr-defined]

from src.log import get_logger
from src.routing.amm_v3 import (
    QUOTE_EXACT_INPUT_SINGLE_SELECTOR,
    QUOTER_V2_ADDRESS,
)
from src.routing.multicall import Call, Multicall3

log = get_logger(__name__)

# keccak256("quoteExactInput(bytes,uint256)")[:4] = cdca1753 (verified)
QUOTE_EXACT_INPUT_SELECTOR = "cdca1753"


@dataclass(frozen=True)
class V3Path:
    """One candidate quote path: direct single-hop, or 2-hop via an intermediate."""

    order_uid: str
    token_in: str
    token_out: str
    amount_in: int
    fee_tier_in: int
    intermediate: str | None = None
    fee_tier_out: int | None = None


@dataclass(frozen=True)
class V3BatchedQuote:
    path: V3Path
    amount_out: int  # 0 on revert / pool-not-found


def _strip_0x(addr: str) -> str:
    return addr[2:] if addr.startswith("0x") or addr.startswith("0X") else addr


def _encode_path_bytes(
    token_in: str,
    fee_in: int,
    intermediate: str | None,
    fee_out: int | None,
    token_out: str,
) -> bytes:
    """Encode a V3 path: tokenA[20] | fee[3 BE] | tokenB[20] [| fee[3] | tokenC[20]]."""
    parts: list[bytes] = [bytes.fromhex(_strip_0x(token_in).rjust(40, "0"))]
    parts.append(fee_in.to_bytes(3, "big"))
    if intermediate is not None:
        if fee_out is None:
            raise ValueError("fee_out required when intermediate is set")
        parts.append(bytes.fromhex(_strip_0x(intermediate).rjust(40, "0")))
        parts.append(fee_out.to_bytes(3, "big"))
    parts.append(bytes.fromhex(_strip_0x(token_out).rjust(40, "0")))
    return b"".join(parts)


def _encode_quote_exact_input(path_bytes: bytes, amount_in: int) -> str:
    """Encode quoteExactInput(bytes,uint256) calldata. Selector cdca1753."""
    encoded = encode(["bytes", "uint256"], [path_bytes, amount_in])
    return "0x" + QUOTE_EXACT_INPUT_SELECTOR + encoded.hex()


def _encode_quote_exact_input_single(
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


def _decode_single_hop_return(data: bytes) -> int:
    """Decode `(uint256, uint160, uint32, uint256)` and return amount_out, else 0."""
    # 4 static-typed 32-byte fields = 128 bytes minimum.
    if len(data) < 128:
        return 0
    try:
        amount_out, _sqrt, _ticks, _gas = decode(
            ["uint256", "uint160", "uint32", "uint256"], data
        )
    except Exception as e:  # noqa: BLE001
        log.debug("single_hop_decode_failed", error=str(e), nbytes=len(data))
        return 0
    return int(amount_out)


def _decode_multi_hop_return(data: bytes) -> int:
    """Decode `(uint256, uint160[], uint32[], uint256)` and return amount_out, else 0."""
    if not data:
        return 0
    try:
        amount_out, _sqrts, _ticks, _gas = decode(
            ["uint256", "uint160[]", "uint32[]", "uint256"], data
        )
    except Exception as e:  # noqa: BLE001
        log.debug("multi_hop_decode_failed", error=str(e), nbytes=len(data))
        return 0
    return int(amount_out)


def _build_call(path: V3Path, quoter_address: str) -> Call:
    if path.intermediate is None:
        call_data = _encode_quote_exact_input_single(
            path.token_in, path.token_out, path.amount_in, path.fee_tier_in
        )
    else:
        path_bytes = _encode_path_bytes(
            path.token_in,
            path.fee_tier_in,
            path.intermediate,
            path.fee_tier_out,
            path.token_out,
        )
        call_data = _encode_quote_exact_input(path_bytes, path.amount_in)
    # allow_failure: pools that don't exist revert; we want amount_out=0 not a crash.
    return Call(target=quoter_address, call_data=call_data, allow_failure=True)


# Each QuoterV2 call costs ~200–300k gas; Multicall3 adds dispatch overhead.
# Alchemy and most providers cap eth_call at 30M gas. Empirically a single
# aggregate of 72 quotes overflowed ("out of gas" -32000) — chunk to stay
# comfortably under the cap. 25 quotes ≈ 7M gas, well within budget.
_MAX_CALLS_PER_BATCH = 25


async def batched_v3_quote(
    multicall: Multicall3,
    paths: list[V3Path],
    quoter_address: str = QUOTER_V2_ADDRESS,
) -> list[V3BatchedQuote]:
    """Submit paths in batched Multicall3.aggregate() calls and decode.

    Returns one V3BatchedQuote per input V3Path, in the same order. Reverted
    calls (pool not found, etc.) yield amount_out=0 — the result list always
    matches the input list length so callers can correlate positionally.

    Paths are chunked into batches of `_MAX_CALLS_PER_BATCH` to stay under
    provider-side eth_call gas caps. With 72 paths and chunk=25 that's 3
    multicalls — still ~27× fewer RPC round-trips than the per-order legacy
    fan-out.
    """
    if not paths:
        return []

    calls = [_build_call(p, quoter_address) for p in paths]
    results: list[Any] = []
    for i in range(0, len(calls), _MAX_CALLS_PER_BATCH):
        chunk = calls[i : i + _MAX_CALLS_PER_BATCH]
        results.extend(await multicall.aggregate(chunk))

    quotes: list[V3BatchedQuote] = []
    for path, result in zip(paths, results, strict=True):
        if not result.success:
            quotes.append(V3BatchedQuote(path=path, amount_out=0))
            continue
        if path.intermediate is None:
            amt = _decode_single_hop_return(result.return_data)
        else:
            amt = _decode_multi_hop_return(result.return_data)
        quotes.append(V3BatchedQuote(path=path, amount_out=amt))
    return quotes
