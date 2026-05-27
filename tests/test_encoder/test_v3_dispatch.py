"""Tests for the encode_v3_swap dispatcher.

The dispatcher picks one of four entry points (exactInputSingle /
exactOutputSingle / exactInput / exactOutput) and — critically — *flips
the packed path direction* for the multi-hop exactOutput case to match
Uniswap V3 SwapRouter's expectation. These tests pin that behaviour
against the bytes our own quoter encoded in ``src/routing/v3_batched.py``
— if the two ever drift the swap will revert at settlement against a
quote it can never honour.
"""

from __future__ import annotations

from eth_abi import decode

from src.encoder.v3 import encode_v3_swap
from src.encoder.v3_calldata import (
    EXACT_INPUT_SINGLE_SELECTOR,
    EXACT_OUTPUT_SINGLE_SELECTOR,
)
from src.encoder.v3_path import (
    EXACT_INPUT_SELECTOR,
    EXACT_OUTPUT_SELECTOR,
    pack_v3_path,
)

USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
DAI = "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1"
SETTLEMENT = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"
ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"


def _swap(**overrides: object) -> bytes:
    """Build a baseline encode_v3_swap call and return the call_data bytes
    of the resulting Interaction. Tests override one knob at a time."""
    args = {
        "token_in": USDC,
        "token_out": WETH,
        "fee_in": 500,
        "intermediate": None,
        "fee_out": None,
        "exact_output": False,
        "executed_sell": 1_000_000,
        "executed_buy": 10**14,
        "recipient": SETTLEMENT,
        "deadline": 1_900_000_000,
        "slippage_bps": 50,
        "router_address": ROUTER,
    }
    args.update(overrides)
    return encode_v3_swap(**args).call_data  # type: ignore[arg-type]


def test_single_hop_sell_uses_exact_input_single() -> None:
    assert _swap()[:4] == EXACT_INPUT_SINGLE_SELECTOR


def test_single_hop_buy_uses_exact_output_single() -> None:
    cd = _swap(exact_output=True)
    assert cd[:4] == EXACT_OUTPUT_SINGLE_SELECTOR


def test_multi_hop_sell_packs_path_in_forward_order() -> None:
    """Sell-kind multi-hop USDC → WETH → DAI: packed path starts with
    USDC (the swap's input). exactInputInternal reads
    ``(tokenIn, tokenOut, fee)`` from the first pool, so this is the
    direction it expects."""
    cd = _swap(
        token_in=USDC, token_out=DAI,
        intermediate=WETH, fee_out=3000,
        exact_output=False,
    )
    assert cd[:4] == EXACT_INPUT_SELECTOR

    (params,) = decode(["(bytes,address,uint256,uint256,uint256)"], cd[4:])
    actual_path, *_ = params
    expected_path = pack_v3_path(tokens=[USDC, WETH, DAI], fees=[500, 3000])
    assert actual_path == expected_path


def test_multi_hop_buy_REVERSES_path_to_match_router_decoding() -> None:
    """The bug this test was added to lock down: multi-hop exactOutput
    requires the path to be reversed so the router decodes the first
    pool as ``(tokenOut, tokenIn, fee)``. If we packed the forward path
    here the router would attempt the swap in the wrong direction —
    every multi-hop buy order would revert on-chain even though its
    quote (which already reverses correctly in src/routing/v3_batched.py)
    appeared to clear.

    Pinned against the exact reversal of the sell-kind test above:
    the call walks DAI → WETH → USDC for the same logical USDC → DAI
    buy.
    """
    cd = _swap(
        token_in=USDC, token_out=DAI,
        intermediate=WETH, fee_out=3000,
        exact_output=True,
    )
    assert cd[:4] == EXACT_OUTPUT_SELECTOR

    (params,) = decode(["(bytes,address,uint256,uint256,uint256)"], cd[4:])
    actual_path, *_ = params
    # Reversed: tokens [token_out, intermediate, token_in],
    # fees [fee_out, fee_in].
    expected_path = pack_v3_path(tokens=[DAI, WETH, USDC], fees=[3000, 500])
    assert actual_path == expected_path
    # And specifically NOT the forward direction — the test that pinned
    # the wrong behaviour would have matched this:
    forward_path = pack_v3_path(tokens=[USDC, WETH, DAI], fees=[500, 3000])
    assert actual_path != forward_path


def test_multi_hop_buy_matches_quoter_path_byte_for_byte() -> None:
    """The settlement swap MUST traverse the same pools the quoter
    quoted; otherwise we'd quote one route and execute another. This
    test reproduces what ``src/routing/v3_batched.py::_build_call``
    encodes for an exact_output multi-hop quote and asserts byte
    equality with our swap encoder's path.
    """
    # Replicate the exact reversal _build_call does — see its inline
    # comment about Uniswap v3-periphery SwapRouter docs.
    quoter_packed_path = pack_v3_path(
        tokens=[DAI, WETH, USDC], fees=[3000, 500],
    )

    cd = _swap(
        token_in=USDC, token_out=DAI,
        intermediate=WETH, fee_out=3000,
        exact_output=True,
    )
    (params,) = decode(["(bytes,address,uint256,uint256,uint256)"], cd[4:])
    encoder_packed_path, *_ = params
    assert encoder_packed_path == quoter_packed_path
