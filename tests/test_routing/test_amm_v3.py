"""Tests for UniV3 quoter (mocked Multicall3)."""
from unittest.mock import AsyncMock

import pytest
from eth_abi import encode

from src.routing.amm_v3 import (
    FEE_TIERS,
    QUOTE_EXACT_INPUT_SINGLE_SELECTOR,
    V3Quote,
    _decode_quote_output,
    _encode_quote_input_single,
    best_v3_quote,
    quote_v3_all_fee_tiers,
)
from src.routing.multicall import CallResult, Multicall3


def test_encode_quote_input_single_starts_with_selector() -> None:
    data = _encode_quote_input_single(
        token_in="0x" + "11" * 20,
        token_out="0x" + "22" * 20,
        amount_in=10**18,
        fee=500,
    )
    assert data.startswith("0x" + QUOTE_EXACT_INPUT_SINGLE_SELECTOR)


def test_decode_quote_output_valid() -> None:
    raw = encode(
        ["uint256", "uint160", "uint32", "uint256"],
        [1234567890, 99999, 5, 100000],
    )
    q = _decode_quote_output(raw)
    assert q is not None
    assert q.amount_out == 1234567890
    assert q.sqrt_price_x96_after == 99999
    assert q.initialized_ticks_crossed == 5
    assert q.gas_estimate == 100000


def test_decode_quote_output_invalid_returns_none() -> None:
    assert _decode_quote_output(b"") is None
    assert _decode_quote_output(b"\x00" * 30) is None


@pytest.mark.asyncio
async def test_quote_v3_all_fee_tiers_returns_quotes() -> None:
    # Mock multicall: first 2 fee tiers succeed, last 2 fail
    successful_data = encode(
        ["uint256", "uint160", "uint32", "uint256"],
        [10**6, 0, 0, 50000],
    )
    rpc = AsyncMock()
    mc = Multicall3(rpc)

    async def fake_aggregate(calls: list) -> list[CallResult]:
        return [
            CallResult(success=True, return_data=successful_data),
            CallResult(success=True, return_data=successful_data),
            CallResult(success=False, return_data=b""),
            CallResult(success=False, return_data=b""),
        ]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    quotes = await quote_v3_all_fee_tiers(
        mc, "0x" + "11" * 20, "0x" + "22" * 20, 10**18
    )
    assert len(quotes) == 2
    assert quotes[0].fee_tier == FEE_TIERS[0]
    assert quotes[1].fee_tier == FEE_TIERS[1]
    assert all(q.amount_out == 10**6 for q in quotes)


@pytest.mark.asyncio
async def test_best_v3_quote_picks_highest_amount_out() -> None:
    rpc = AsyncMock()
    mc = Multicall3(rpc)

    async def fake_aggregate(calls: list) -> list[CallResult]:
        # All 4 fee tiers return different amounts
        results = []
        for amt in (100, 500, 200, 50):
            results.append(
                CallResult(
                    success=True,
                    return_data=encode(
                        ["uint256", "uint160", "uint32", "uint256"],
                        [amt, 0, 0, 50000],
                    ),
                )
            )
        return results

    mc.aggregate = fake_aggregate  # type: ignore[assignment]
    best = await best_v3_quote(mc, "0x" + "11" * 20, "0x" + "22" * 20, 10**18)
    assert best is not None
    assert best.amount_out == 500
    assert best.fee_tier == FEE_TIERS[1]


@pytest.mark.asyncio
async def test_quote_v3_zero_amount_out_filtered() -> None:
    rpc = AsyncMock()
    mc = Multicall3(rpc)

    async def fake_aggregate(calls: list) -> list[CallResult]:
        return [
            CallResult(
                success=True,
                return_data=encode(
                    ["uint256", "uint160", "uint32", "uint256"], [0, 0, 0, 0]
                ),
            ),
        ]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]
    quotes = await quote_v3_all_fee_tiers(
        mc, "0x" + "11" * 20, "0x" + "22" * 20, 10**18, fee_tiers=(500,)
    )
    assert quotes == []


@pytest.mark.asyncio
async def test_best_v3_quote_all_fail_returns_none() -> None:
    rpc = AsyncMock()
    mc = Multicall3(rpc)

    async def fake_aggregate(calls: list) -> list[CallResult]:
        return [CallResult(success=False, return_data=b"")] * len(calls)

    mc.aggregate = fake_aggregate  # type: ignore[assignment]
    result = await best_v3_quote(mc, "0x" + "11" * 20, "0x" + "22" * 20, 10**18)
    assert result is None


def test_v3quote_fee_tier_set_correctly() -> None:
    q = V3Quote(
        fee_tier=3000,
        amount_out=42,
        sqrt_price_x96_after=1,
        initialized_ticks_crossed=2,
        gas_estimate=3,
    )
    assert q.fee_tier == 3000
