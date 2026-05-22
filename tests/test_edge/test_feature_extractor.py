"""Tests for the on-chain feature extractor (mocked Multicall)."""
from unittest.mock import AsyncMock

import pytest
from eth_abi import encode

from edge.classifier.feature_extractor import (
    _encode_decimals_call,
    extract_onchain_features,
)
from src.routing.multicall import CallResult, Multicall3


def test_encode_decimals_call_starts_with_selector():
    data = _encode_decimals_call()
    assert data == "0x313ce567"


@pytest.mark.asyncio
async def test_extract_empty_returns_empty():
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    out = await extract_onchain_features(mc, [])
    assert out == {}


@pytest.mark.asyncio
async def test_extract_single_token_decimals_only():
    """One token, decimals call succeeds, all factory pair calls fail."""
    rpc = AsyncMock()
    mc = Multicall3(rpc)

    decimals_data = encode(["uint8"], [18])
    zero_addr_data = b"\x00" * 32

    async def fake_aggregate(calls):
        # First call is decimals, rest are factory.getPair (all return 0 address = no pool)
        results = [CallResult(success=True, return_data=decimals_data)]
        for _ in calls[1:]:
            results.append(CallResult(success=True, return_data=zero_addr_data))
        return results

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    token = "0x" + "ab" * 20
    out = await extract_onchain_features(mc, [token])
    assert token in out
    assert out[token].decimals == 18
    assert out[token].pool_count_v2 == 0
    assert out[token].pool_count_camelot == 0


@pytest.mark.asyncio
async def test_extract_counts_pools():
    """A token has 2 successful pool addresses returned."""
    rpc = AsyncMock()
    mc = Multicall3(rpc)

    decimals_data = encode(["uint8"], [6])
    nonzero_pool = b"\x00" * 12 + b"\x11" * 20  # left-pad to 32 bytes
    zero_addr_data = b"\x00" * 32

    call_results = [CallResult(success=True, return_data=decimals_data)]
    # 2 factories × 3 intermediates = 6 calls; mark first 2 as having pools
    for i in range(6):
        if i < 2:
            call_results.append(CallResult(success=True, return_data=nonzero_pool))
        else:
            call_results.append(CallResult(success=True, return_data=zero_addr_data))

    async def fake_aggregate(calls):
        return call_results

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    token = "0x" + "ab" * 20
    out = await extract_onchain_features(mc, [token])
    assert out[token].decimals == 6
    assert out[token].pool_count_v2 == 2


@pytest.mark.asyncio
async def test_extract_handles_decimals_failure():
    """If decimals call fails, decimals is None but other features still extracted."""
    rpc = AsyncMock()
    mc = Multicall3(rpc)

    nonzero_pool = b"\x00" * 12 + b"\x11" * 20

    async def fake_aggregate(calls):
        results = [CallResult(success=False, return_data=b"")]
        for _ in calls[1:]:
            results.append(CallResult(success=True, return_data=nonzero_pool))
        return results

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    token = "0x" + "ab" * 20
    out = await extract_onchain_features(mc, [token])
    assert out[token].decimals is None
    assert out[token].pool_count_v2 == 6  # all 6 pair calls returned non-zero
