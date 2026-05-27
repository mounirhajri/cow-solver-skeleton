"""Tests for V3 multi-hop path packing and the exactInput/exactOutput encoders."""

import pytest
from eth_abi import decode
from eth_utils import keccak

from src.encoder.v3_path import (
    EXACT_INPUT_SELECTOR,
    EXACT_OUTPUT_SELECTOR,
    encode_exact_input,
    encode_exact_output,
    pack_v3_path,
)

USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
DAI = "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1"
SETTLEMENT = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"


def test_exact_input_selector_matches_keccak() -> None:
    sig = "exactInput((bytes,address,uint256,uint256,uint256))"
    assert EXACT_INPUT_SELECTOR == keccak(text=sig)[:4]
    assert EXACT_INPUT_SELECTOR.hex() == "c04b8d59"


def test_exact_output_selector_matches_keccak() -> None:
    sig = "exactOutput((bytes,address,uint256,uint256,uint256))"
    assert EXACT_OUTPUT_SELECTOR == keccak(text=sig)[:4]
    assert EXACT_OUTPUT_SELECTOR.hex() == "f28c0498"


def test_pack_v3_path_two_hop_layout_is_byte_exact() -> None:
    """V3's packed path is the byte layout the router reads directly. The
    layout is non-obvious — verifying each section byte-for-byte catches
    fee-width and address-padding mistakes at unit-test time instead of
    on-chain revert."""
    path = pack_v3_path(tokens=[USDC, WETH, DAI], fees=[500, 3000])
    # 3 × 20-byte addresses + 2 × 3-byte fees = 66 bytes
    assert len(path) == 3 * 20 + 2 * 3
    assert path[0:20] == bytes.fromhex(USDC[2:])
    assert path[20:23] == (500).to_bytes(3, "big")
    assert path[23:43] == bytes.fromhex(WETH[2:])
    assert path[43:46] == (3000).to_bytes(3, "big")
    assert path[46:66] == bytes.fromhex(DAI[2:])


def test_pack_v3_path_single_hop_works_for_completeness() -> None:
    """A 2-token / 1-fee path is technically still a valid V3 path even
    though single-hop swaps usually go through exactInputSingle. Useful
    for sources that want one code path for everything."""
    path = pack_v3_path(tokens=[USDC, WETH], fees=[500])
    assert len(path) == 2 * 20 + 1 * 3
    assert path[:20] == bytes.fromhex(USDC[2:])
    assert path[20:23] == (500).to_bytes(3, "big")
    assert path[23:43] == bytes.fromhex(WETH[2:])


@pytest.mark.parametrize(
    "tokens,fees,err",
    [
        ([USDC], [], "at least 2 tokens"),
        ([USDC, WETH], [500, 3000], "exactly one fee per hop"),
        ([USDC, WETH], [], "exactly one fee per hop"),
        ([USDC, "0xshort"], [500], "20-byte address"),
        ([USDC, WETH], [2**24], "uint24"),
        ([USDC, WETH], [-1], "uint24"),
    ],
)
def test_pack_v3_path_rejects_invalid_shapes(
    tokens: list[str], fees: list[int], err: str
) -> None:
    with pytest.raises(ValueError, match=err):
        pack_v3_path(tokens, fees)


def test_exact_input_roundtrips_through_abi_decode() -> None:
    path = pack_v3_path(tokens=[USDC, WETH, DAI], fees=[500, 3000])
    cd = encode_exact_input(
        path=path,
        recipient=SETTLEMENT,
        deadline=1_900_000_000,
        amount_in=100_000_000,
        amount_out_minimum=99_000_000_000_000_000_000,
    )
    assert cd[:4] == EXACT_INPUT_SELECTOR

    (params,) = decode(["(bytes,address,uint256,uint256,uint256)"], cd[4:])
    decoded_path, recipient, deadline, amount_in, amount_out_minimum = params
    assert decoded_path == path
    assert recipient.lower() == SETTLEMENT.lower()
    assert deadline == 1_900_000_000
    assert amount_in == 100_000_000
    assert amount_out_minimum == 99_000_000_000_000_000_000


def test_exact_output_roundtrips_through_abi_decode() -> None:
    """V3 exactOutput uses the SAME path direction as exactInput — token A
    is still the input even though the router walks the path backwards.
    Reversing it (a tempting "fix") would route the swap wrong."""
    path = pack_v3_path(tokens=[USDC, WETH, DAI], fees=[500, 3000])
    cd = encode_exact_output(
        path=path,
        recipient=SETTLEMENT,
        deadline=1_900_000_000,
        amount_out=99_000_000_000_000_000_000,
        amount_in_maximum=200_000_000,
    )
    assert cd[:4] == EXACT_OUTPUT_SELECTOR

    (params,) = decode(["(bytes,address,uint256,uint256,uint256)"], cd[4:])
    decoded_path, recipient, deadline, amount_out, amount_in_maximum = params
    assert decoded_path == path  # direction is NOT reversed
    assert amount_out == 99_000_000_000_000_000_000
    assert amount_in_maximum == 200_000_000
