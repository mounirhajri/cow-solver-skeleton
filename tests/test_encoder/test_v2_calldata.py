"""Tests for V2-style (Camelot/Ramses/Sushi) router encoders."""

import pytest
from eth_abi import decode
from eth_utils import keccak

from src.encoder.v2_calldata import (
    SWAP_EXACT_TOKENS_FOR_TOKENS_SELECTOR,
    SWAP_TOKENS_FOR_EXACT_TOKENS_SELECTOR,
    encode_swap_exact_tokens_for_tokens,
    encode_swap_tokens_for_exact_tokens,
)

USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
DAI = "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1"
SETTLEMENT = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"

_ARG_TYPES = ["uint256", "uint256", "address[]", "address", "uint256"]


def test_swap_exact_tokens_for_tokens_selector_matches_keccak() -> None:
    sig = "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)"
    assert keccak(text=sig)[:4] == SWAP_EXACT_TOKENS_FOR_TOKENS_SELECTOR
    assert SWAP_EXACT_TOKENS_FOR_TOKENS_SELECTOR.hex() == "38ed1739"


def test_swap_tokens_for_exact_tokens_selector_matches_keccak() -> None:
    sig = "swapTokensForExactTokens(uint256,uint256,address[],address,uint256)"
    assert keccak(text=sig)[:4] == SWAP_TOKENS_FOR_EXACT_TOKENS_SELECTOR
    assert SWAP_TOKENS_FOR_EXACT_TOKENS_SELECTOR.hex() == "8803dbee"


def test_swap_exact_tokens_for_tokens_roundtrips_direct() -> None:
    cd = encode_swap_exact_tokens_for_tokens(
        token_in=USDC,
        token_out=WETH,
        path=[USDC, WETH],
        recipient=SETTLEMENT,
        deadline=1_900_000_000,
        amount_in=100_000_000,
        amount_out_minimum=34_000_000_000_000,
    )
    assert cd[:4] == SWAP_EXACT_TOKENS_FOR_TOKENS_SELECTOR

    amount_in, amount_out_minimum, path, recipient, deadline = decode(_ARG_TYPES, cd[4:])
    # V2's path arg order: amount_in, amount_out_minimum, path, to, deadline
    # If the encoder ever swaps amount_in and amount_out_minimum the swap
    # would still execute but with inverted slippage protection — almost
    # always reverts because the user "asks for 100 USDC out but accepts 34T wei
    # min in". Pin the order explicitly.
    assert amount_in == 100_000_000
    assert amount_out_minimum == 34_000_000_000_000
    assert [a.lower() for a in path] == [USDC.lower(), WETH.lower()]
    assert recipient.lower() == SETTLEMENT.lower()
    assert deadline == 1_900_000_000


def test_swap_exact_tokens_for_tokens_roundtrips_multihop() -> None:
    """V2 has no separate multi-hop function — the same selector handles
    any path length. Verifying the longer path round-trips ensures the
    dynamic array encoding is doing the right thing."""
    cd = encode_swap_exact_tokens_for_tokens(
        token_in=USDC,
        token_out=DAI,
        path=[USDC, WETH, DAI],
        recipient=SETTLEMENT,
        deadline=1_900_000_000,
        amount_in=100_000_000,
        amount_out_minimum=99_000_000_000_000_000_000,
    )
    assert cd[:4] == SWAP_EXACT_TOKENS_FOR_TOKENS_SELECTOR

    _, _, path, _, _ = decode(_ARG_TYPES, cd[4:])
    assert [a.lower() for a in path] == [USDC.lower(), WETH.lower(), DAI.lower()]


def test_swap_tokens_for_exact_tokens_roundtrips() -> None:
    cd = encode_swap_tokens_for_exact_tokens(
        token_in=USDC,
        token_out=WETH,
        path=[USDC, WETH],
        recipient=SETTLEMENT,
        deadline=1_900_000_000,
        amount_out=1_000_000_000_000_000,
        amount_in_maximum=5_000_000,
    )
    assert cd[:4] == SWAP_TOKENS_FOR_EXACT_TOKENS_SELECTOR

    amount_out, amount_in_maximum, path, _, _ = decode(_ARG_TYPES, cd[4:])
    assert amount_out == 1_000_000_000_000_000
    assert amount_in_maximum == 5_000_000


@pytest.mark.parametrize(
    "path,err",
    [
        ([USDC], "at least 2 tokens"),
        ([DAI, WETH], "path\\[0\\] must equal token_in"),
        ([USDC, DAI], "path\\[-1\\] must equal token_out"),
        ([USDC, "0xshort"], "20-byte address"),
    ],
)
def test_path_validation_rejects_bad_inputs(path: list[str], err: str) -> None:
    with pytest.raises(ValueError, match=err):
        encode_swap_exact_tokens_for_tokens(
            token_in=USDC,
            token_out=WETH,
            path=path,
            recipient=SETTLEMENT,
            deadline=1_900_000_000,
            amount_in=100,
            amount_out_minimum=1,
        )
