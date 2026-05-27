"""Uniswap V3 SwapRouter calldata builders.

Single-hop variants (``exactInputSingle`` for sell-kind orders,
``exactOutputSingle`` for buy-kind) live here. Multi-hop variants that take
a packed path live in ``v3_path.py`` because the path construction is its
own concern.

**Targets the original SwapRouter** (``0xE592427A0AEce92De3Edee1F18E0157C05861564``),
NOT SwapRouter02. The two have subtly different struct shapes:

  - SwapRouter:   ``(tokenIn, tokenOut, fee, recipient, deadline, amountIn, amountOutMinimum, sqrtPriceLimitX96)``  → selector 0x414bf389
  - SwapRouter02: ``(tokenIn, tokenOut, fee, recipient,           amountIn, amountOutMinimum, sqrtPriceLimitX96)``  → selector 0x04e45aaf

We want the deadline guard, so we use SwapRouter. If you ever switch
``settings.v3_swap_router`` to the SwapRouter02 address you must change the
struct here too — there is no graceful runtime fallback.

Selectors are precomputed module-level constants — they never change for a
given Solidity signature and recomputing them per call adds keccak overhead
for no reason.
"""

from __future__ import annotations

from eth_abi import encode
from eth_utils import keccak

# Solidity signatures, kept verbatim so a grep across both this module and
# the V3 source code finds matching artefacts.
_SIG_EXACT_INPUT_SINGLE = (
    "exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))"
)
_SIG_EXACT_OUTPUT_SINGLE = (
    "exactOutputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))"
)

EXACT_INPUT_SINGLE_SELECTOR = keccak(text=_SIG_EXACT_INPUT_SINGLE)[:4]
EXACT_OUTPUT_SINGLE_SELECTOR = keccak(text=_SIG_EXACT_OUTPUT_SINGLE)[:4]

# Struct ABI used for both functions — fields differ in meaning, not shape.
_PARAMS_STRUCT = "(address,address,uint24,address,uint256,uint256,uint256,uint160)"


def encode_exact_input_single(
    token_in: str,
    token_out: str,
    fee: int,
    recipient: str,
    deadline: int,
    amount_in: int,
    amount_out_minimum: int,
) -> bytes:
    """Encode ``ISwapRouter.exactInputSingle(params)`` calldata.

    Use for **sell-kind** orders: we fix ``amount_in`` and protect against
    pool drift via ``amount_out_minimum``. The on-chain swap reverts if the
    realised output falls below the minimum.

    ``recipient`` is the address that receives the buy_token output —
    during CoW settlement this is always the GPv2 Settlement contract, but
    we keep it as a parameter so the encoder doesn't reach for global state.

    ``sqrtPriceLimitX96`` is fixed to 0 (no price limit). A non-zero limit
    can cause partial fills which CoW solutions can't represent at the
    interaction layer — better to revert than partially fill.
    """
    params = encode(
        [_PARAMS_STRUCT],
        [
            (
                token_in,
                token_out,
                fee,
                recipient,
                deadline,
                amount_in,
                amount_out_minimum,
                0,
            )
        ],
    )
    return EXACT_INPUT_SINGLE_SELECTOR + params


def encode_exact_output_single(
    token_in: str,
    token_out: str,
    fee: int,
    recipient: str,
    deadline: int,
    amount_out: int,
    amount_in_maximum: int,
) -> bytes:
    """Encode ``ISwapRouter.exactOutputSingle(params)`` calldata.

    Use for **buy-kind** orders: we fix ``amount_out`` (the user wants
    exactly this much buy_token) and cap how much sell_token we'll
    consume via ``amount_in_maximum``. The on-chain swap reverts if the
    required input exceeds the cap.

    Same recipient and price-limit semantics as ``encode_exact_input_single``.
    Note the struct field order is identical — V3 packs amount_out into the
    same slot ``exactInputSingle`` uses for amount_in, and amount_in_maximum
    into the slot it uses for amount_out_minimum.
    """
    params = encode(
        [_PARAMS_STRUCT],
        [
            (
                token_in,
                token_out,
                fee,
                recipient,
                deadline,
                amount_out,
                amount_in_maximum,
                0,
            )
        ],
    )
    return EXACT_OUTPUT_SINGLE_SELECTOR + params
