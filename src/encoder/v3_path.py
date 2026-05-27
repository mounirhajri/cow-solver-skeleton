"""Uniswap V3 multi-hop encoding.

A multi-hop V3 swap takes a *packed path* — a single ``bytes`` value that
interleaves token addresses (20 bytes each) with pool fees (3 bytes each):

    tokenA | fee_AB | tokenB | fee_BC | tokenC  …

For sell-kind multi-hop we call ``exactInput(ExactInputParams)``. For
buy-kind multi-hop we call ``exactOutput(ExactOutputParams)``. The struct
shapes differ only in the meaning of the last two amount fields, mirroring
the single-hop pair.

The path direction is **the same in both cases** in Uniswap V3 — token A
is always the input token, the last token is always the output. There is
no need to reverse the path for exactOutput; the router walks it backwards
internally.
"""

from __future__ import annotations

from eth_abi import encode
from eth_utils import keccak

_SIG_EXACT_INPUT = "exactInput((bytes,address,uint256,uint256,uint256))"
_SIG_EXACT_OUTPUT = "exactOutput((bytes,address,uint256,uint256,uint256))"

EXACT_INPUT_SELECTOR = keccak(text=_SIG_EXACT_INPUT)[:4]
EXACT_OUTPUT_SELECTOR = keccak(text=_SIG_EXACT_OUTPUT)[:4]

_PARAMS_STRUCT = "(bytes,address,uint256,uint256,uint256)"


def pack_v3_path(tokens: list[str], fees: list[int]) -> bytes:
    """Pack a multi-hop path into Uniswap V3's wire format.

    Constraints:
      - ``len(tokens) >= 2`` (a single-hop has 2 tokens; use this for >=3)
      - ``len(fees) == len(tokens) - 1`` (one fee per hop)
      - Each fee fits in uint24 (V3 tiers are 100, 500, 3000, 10000)
      - Addresses must be 0x-prefixed 20-byte hex
    """
    if len(tokens) < 2:
        raise ValueError(f"need at least 2 tokens for a path, got {len(tokens)}")
    if len(fees) != len(tokens) - 1:
        raise ValueError(
            f"need exactly one fee per hop: tokens={len(tokens)} → expect "
            f"{len(tokens) - 1} fees, got {len(fees)}"
        )

    out = bytearray()
    for i, token in enumerate(tokens):
        if not (token.startswith("0x") and len(token) == 42):
            raise ValueError(f"tokens[{i}] is not a 0x-prefixed 20-byte address: {token!r}")
        out += bytes.fromhex(token[2:])
        if i < len(fees):
            fee = fees[i]
            if not (0 <= fee < 2**24):
                raise ValueError(f"fees[{i}] does not fit in uint24: {fee}")
            out += fee.to_bytes(3, "big")
    return bytes(out)


def encode_exact_input(
    path: bytes,
    recipient: str,
    deadline: int,
    amount_in: int,
    amount_out_minimum: int,
) -> bytes:
    """Encode ``ISwapRouter.exactInput((path, recipient, deadline, amountIn, amountOutMinimum))``.

    For sell-kind multi-hop. ``path`` is the output of ``pack_v3_path``.
    """
    params = encode(
        [_PARAMS_STRUCT],
        [(path, recipient, deadline, amount_in, amount_out_minimum)],
    )
    return EXACT_INPUT_SELECTOR + params


def encode_exact_output(
    path: bytes,
    recipient: str,
    deadline: int,
    amount_out: int,
    amount_in_maximum: int,
) -> bytes:
    """Encode ``ISwapRouter.exactOutput((path, recipient, deadline, amountOut, amountInMaximum))``.

    For buy-kind multi-hop. Path direction is identical to ``exactInput`` —
    do not reverse it; the V3 router walks backwards from the last token
    when settling exactOutput.
    """
    params = encode(
        [_PARAMS_STRUCT],
        [(path, recipient, deadline, amount_out, amount_in_maximum)],
    )
    return EXACT_OUTPUT_SELECTOR + params
