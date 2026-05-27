"""Tests for V3 single-hop encoders.

Selectors are pinned against fresh keccak so a stray edit to the Solidity
signature string is caught at unit-test time rather than producing
calldata that routes to the wrong on-chain function (silent revert).

Struct fields are decoded back from the produced calldata to verify the
exact byte layout the V3 SwapRouter sees — this is the only way to catch
field-ordering bugs without a fork test.
"""

from eth_abi import decode
from eth_utils import keccak

from src.encoder.v3_calldata import (
    EXACT_INPUT_SINGLE_SELECTOR,
    EXACT_OUTPUT_SINGLE_SELECTOR,
    encode_exact_input_single,
    encode_exact_output_single,
)

# Arbitrum mainnet addresses — pinned values so the tests double as
# documentation of which contracts we target.
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
SETTLEMENT = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"

_STRUCT_TYPE = "(address,address,uint24,address,uint256,uint256,uint256,uint160)"


def test_exact_input_single_selector_matches_keccak() -> None:
    sig = (
        "exactInputSingle((address,address,uint24,address,"
        "uint256,uint256,uint256,uint160))"
    )
    assert keccak(text=sig)[:4] == EXACT_INPUT_SINGLE_SELECTOR
    # Pin the literal too — if the struct shape ever changes (e.g. accidentally
    # adopting SwapRouter02's deadline-less struct) this catches it.
    assert EXACT_INPUT_SINGLE_SELECTOR.hex() == "414bf389"


def test_exact_output_single_selector_matches_keccak() -> None:
    sig = (
        "exactOutputSingle((address,address,uint24,address,"
        "uint256,uint256,uint256,uint160))"
    )
    assert keccak(text=sig)[:4] == EXACT_OUTPUT_SINGLE_SELECTOR
    assert EXACT_OUTPUT_SINGLE_SELECTOR.hex() == "db3e2198"


def test_exact_input_single_roundtrips_through_abi_decode() -> None:
    """The encoder must produce calldata whose struct decode matches the
    inputs verbatim — catches every kind of field-shuffling bug."""
    cd = encode_exact_input_single(
        token_in=USDC,
        token_out=WETH,
        fee=500,
        recipient=SETTLEMENT,
        deadline=1_900_000_000,
        amount_in=100_000_000,
        amount_out_minimum=34_000_000_000_000,
    )
    assert cd[:4] == EXACT_INPUT_SINGLE_SELECTOR
    # 4-byte selector + 8 × 32-byte slots = 260 bytes
    assert len(cd) == 4 + 8 * 32

    (params,) = decode([_STRUCT_TYPE], cd[4:])
    (
        token_in,
        token_out,
        fee,
        recipient,
        deadline,
        amount_in,
        amount_out_minimum,
        sqrt_price_limit_x96,
    ) = params
    assert token_in.lower() == USDC.lower()
    assert token_out.lower() == WETH.lower()
    assert fee == 500
    assert recipient.lower() == SETTLEMENT.lower()
    assert deadline == 1_900_000_000
    assert amount_in == 100_000_000
    assert amount_out_minimum == 34_000_000_000_000
    # We hard-code 0; a non-zero limit can cause partial fills which CoW
    # solutions can't express in interactions.
    assert sqrt_price_limit_x96 == 0


def test_exact_output_single_roundtrips_through_abi_decode() -> None:
    cd = encode_exact_output_single(
        token_in=USDC,
        token_out=WETH,
        fee=500,
        recipient=SETTLEMENT,
        deadline=1_900_000_000,
        amount_out=50_000_000_000_000,
        amount_in_maximum=200_000_000,
    )
    assert cd[:4] == EXACT_OUTPUT_SINGLE_SELECTOR
    assert len(cd) == 4 + 8 * 32

    (params,) = decode([_STRUCT_TYPE], cd[4:])
    (
        token_in,
        token_out,
        fee,
        recipient,
        deadline,
        amount_out,
        amount_in_maximum,
        sqrt_price_limit_x96,
    ) = params
    assert token_in.lower() == USDC.lower()
    assert token_out.lower() == WETH.lower()
    assert fee == 500
    assert deadline == 1_900_000_000
    # For exactOutput the V3 router reuses the same struct slots: amount_out
    # lands where exactInput puts amount_in; amount_in_maximum lands where
    # exactInput puts amount_out_minimum. The encoder relies on that — if
    # this assertion ever flips it means we crossed the wires.
    assert amount_out == 50_000_000_000_000
    assert amount_in_maximum == 200_000_000
    assert sqrt_price_limit_x96 == 0
