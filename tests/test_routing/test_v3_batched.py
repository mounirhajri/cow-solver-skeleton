"""Tests for batched V3 quoter (mocked Multicall3)."""
from unittest.mock import AsyncMock

import pytest
from eth_abi import encode
from eth_utils import keccak

from src.routing.multicall import Call, CallResult, Multicall3
from src.routing.v3_batched import (
    QUOTE_EXACT_INPUT_SELECTOR,
    QUOTE_EXACT_OUTPUT_SELECTOR,
    QUOTE_EXACT_OUTPUT_SINGLE_SELECTOR,
    V3BatchedQuote,
    V3Path,
    _build_call,
    _encode_path_bytes,
    _encode_quote_exact_input,
    _encode_quote_exact_output,
    _encode_quote_exact_output_single,
    batched_v3_quote,
)


def test_quote_exact_input_selector_matches_keccak() -> None:
    expected = keccak(b"quoteExactInput(bytes,uint256)")[:4].hex()
    assert expected == QUOTE_EXACT_INPUT_SELECTOR


def test_quote_exact_output_selectors_match_keccak() -> None:
    """Selectors for QuoterV2's exact-output entry points. Mismatched
    selectors would silently route to the wrong function on-chain and
    revert — pin them against keccak to catch typos at unit-test time."""
    assert (
        keccak(b"quoteExactOutput(bytes,uint256)")[:4].hex()
        == QUOTE_EXACT_OUTPUT_SELECTOR
    )
    assert (
        keccak(
            b"quoteExactOutputSingle((address,address,uint256,uint24,uint160))"
        )[:4].hex()
        == QUOTE_EXACT_OUTPUT_SINGLE_SELECTOR
    )


def test_encode_quote_exact_output_single_calldata_prefix() -> None:
    cd = _encode_quote_exact_output_single("0x" + "11" * 20, "0x" + "22" * 20, 500, 3000)
    assert cd.startswith("0x" + QUOTE_EXACT_OUTPUT_SINGLE_SELECTOR)


def test_encode_quote_exact_output_calldata_prefix() -> None:
    path_bytes = bytes.fromhex("22" * 20 + "000bb8" + "11" * 20)  # reversed!
    cd = _encode_quote_exact_output(path_bytes, 500)
    assert cd.startswith("0x" + QUOTE_EXACT_OUTPUT_SELECTOR)


def test_build_call_exact_output_multihop_reverses_path() -> None:
    """For exact-output multi-hop, path bytes must be encoded
    tokenOut → fee_BC → intermediate → fee_AB → tokenIn (reverse of input).
    Verify by comparing calldata to a path-bytes call we encode manually."""
    token_in = "0x" + "11" * 20    # A (sell)
    intermediate = "0x" + "22" * 20  # B
    token_out = "0x" + "33" * 20   # C (buy)
    fee_ab = 500
    fee_bc = 3000
    amount_out = 7777

    path = V3Path(
        order_uid="o1",
        token_in=token_in,
        token_out=token_out,
        amount_in=amount_out,
        fee_tier_in=fee_ab,
        intermediate=intermediate,
        fee_tier_out=fee_bc,
        exact_output=True,
    )
    call = _build_call(path, "0x" + "9" * 40)
    # Build the expected reversed-path calldata directly.
    expected_path_bytes = _encode_path_bytes(
        token_out, fee_bc, intermediate, fee_ab, token_in
    )
    expected = _encode_quote_exact_output(expected_path_bytes, amount_out)
    assert call.call_data == expected


def test_encode_quote_exact_input_path() -> None:
    """WETH | 3000 | USDC path bytes: 20 + 3 + 20 = 43 bytes; fee encoded big-endian."""
    weth = "0x" + "11" * 20
    usdc = "0x" + "22" * 20
    path_bytes = _encode_path_bytes(weth, 3000, None, None, usdc)
    assert len(path_bytes) == 43
    assert path_bytes[:20] == bytes.fromhex("11" * 20)
    # 3000 = 0x0bb8 → big-endian 3-byte: 00 0b b8
    assert path_bytes[20:23] == b"\x00\x0b\xb8"
    assert path_bytes[23:43] == bytes.fromhex("22" * 20)


def test_encode_path_bytes_multi_hop_length() -> None:
    """2-hop: 20 + 3 + 20 + 3 + 20 = 66 bytes."""
    a = "0x" + "aa" * 20
    b = "0x" + "bb" * 20
    c = "0x" + "cc" * 20
    path_bytes = _encode_path_bytes(a, 500, b, 3000, c)
    assert len(path_bytes) == 66
    assert path_bytes[20:23] == (500).to_bytes(3, "big")
    assert path_bytes[43:46] == (3000).to_bytes(3, "big")


def test_encode_quote_exact_input_calldata_prefix() -> None:
    path_bytes = bytes.fromhex("11" * 20 + "000bb8" + "22" * 20)
    calldata = _encode_quote_exact_input(path_bytes, 10**18)
    assert calldata.startswith("0x" + QUOTE_EXACT_INPUT_SELECTOR)


@pytest.mark.asyncio
async def test_batched_v3_quote_single_hop_decode() -> None:
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    return_data = encode(
        ["uint256", "uint160", "uint32", "uint256"],
        [123456, 0, 0, 50000],
    )

    async def fake_aggregate(_calls: list[Call]) -> list[CallResult]:
        return [CallResult(success=True, return_data=return_data)]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    path = V3Path(
        order_uid="o1",
        token_in="0x" + "11" * 20,
        token_out="0x" + "22" * 20,
        amount_in=10**18,
        fee_tier_in=500,
    )
    quotes = await batched_v3_quote(mc, [path])
    assert len(quotes) == 1
    assert quotes[0].amount_out == 123456
    assert quotes[0].path is path


@pytest.mark.asyncio
async def test_batched_v3_quote_multi_hop_decode() -> None:
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    # quoteExactInput returns (uint256, uint160[], uint32[], uint256)
    return_data = encode(
        ["uint256", "uint160[]", "uint32[]", "uint256"],
        [999999, [1, 2], [3, 4], 80000],
    )

    async def fake_aggregate(_calls: list[Call]) -> list[CallResult]:
        return [CallResult(success=True, return_data=return_data)]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    path = V3Path(
        order_uid="o2",
        token_in="0x" + "11" * 20,
        token_out="0x" + "33" * 20,
        amount_in=10**18,
        fee_tier_in=500,
        intermediate="0x" + "22" * 20,
        fee_tier_out=3000,
    )
    quotes = await batched_v3_quote(mc, [path])
    assert len(quotes) == 1
    assert quotes[0].amount_out == 999999


@pytest.mark.asyncio
async def test_batched_v3_quote_revert_returns_zero_amount() -> None:
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    ok_data = encode(
        ["uint256", "uint160", "uint32", "uint256"],
        [777, 0, 0, 0],
    )

    async def fake_aggregate(_calls: list[Call]) -> list[CallResult]:
        return [
            CallResult(success=False, return_data=b""),  # revert
            CallResult(success=True, return_data=ok_data),
        ]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    paths = [
        V3Path(
            order_uid="o1",
            token_in="0x" + "11" * 20,
            token_out="0x" + "22" * 20,
            amount_in=10**18,
            fee_tier_in=100,
        ),
        V3Path(
            order_uid="o1",
            token_in="0x" + "11" * 20,
            token_out="0x" + "22" * 20,
            amount_in=10**18,
            fee_tier_in=500,
        ),
    ]
    quotes = await batched_v3_quote(mc, paths)
    assert len(quotes) == 2
    assert quotes[0].amount_out == 0
    assert quotes[0].path is paths[0]  # preserved index
    assert quotes[1].amount_out == 777


@pytest.mark.asyncio
async def test_batched_v3_quote_batches_all_in_one_aggregate_call() -> None:
    """One Multicall3.aggregate() — not N of them."""
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    ok_data = encode(["uint256", "uint160", "uint32", "uint256"], [1, 0, 0, 0])

    call_count = 0
    last_call_size = 0

    async def fake_aggregate(calls: list[Call]) -> list[CallResult]:
        nonlocal call_count, last_call_size
        call_count += 1
        last_call_size = len(calls)
        return [CallResult(success=True, return_data=ok_data) for _ in calls]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    paths = [
        V3Path(
            order_uid=f"o{i}",
            token_in="0x" + "11" * 20,
            token_out="0x" + "22" * 20,
            amount_in=10**18,
            fee_tier_in=fee,
        )
        for i in range(3)
        for fee in (100, 500, 3000, 10000)
    ]
    quotes = await batched_v3_quote(mc, paths)
    assert call_count == 1
    assert last_call_size == 12
    assert len(quotes) == 12


@pytest.mark.asyncio
async def test_batched_v3_quote_empty_paths_skips_rpc() -> None:
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    calls_made = 0

    async def fake_aggregate(_calls: list[Call]) -> list[CallResult]:
        nonlocal calls_made
        calls_made += 1
        return []

    mc.aggregate = fake_aggregate  # type: ignore[assignment]
    quotes = await batched_v3_quote(mc, [])
    assert quotes == []
    assert calls_made == 0


def test_v3batched_quote_default_values() -> None:
    path = V3Path(
        order_uid="x", token_in="0xa", token_out="0xb", amount_in=1, fee_tier_in=500
    )
    q = V3BatchedQuote(path=path, amount_out=0)
    assert q.amount_out == 0
    assert q.path.intermediate is None
    assert q.path.fee_tier_out is None
