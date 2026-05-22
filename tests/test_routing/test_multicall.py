from unittest.mock import AsyncMock

import pytest

from src.routing.multicall import AGGREGATE3_SELECTOR, Call, Multicall3


@pytest.mark.asyncio
async def test_multicall_empty_returns_empty() -> None:
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    result = await mc.aggregate([])
    assert result == []
    rpc.eth_call.assert_not_called()


@pytest.mark.asyncio
async def test_multicall_encodes_calls_into_aggregate3() -> None:
    rpc = AsyncMock()
    # Minimal valid (bool, bytes)[] with 1 entry, success=true, data=b"\x01\x02"
    from eth_abi import encode

    encoded = encode(["(bool,bytes)[]"], [[(True, b"\x01\x02")]])
    rpc.eth_call = AsyncMock(return_value="0x" + encoded.hex())

    mc = Multicall3(rpc)
    calls = [Call(target="0x" + "11" * 20, call_data="0xabcdef00")]
    result = await mc.aggregate(calls)
    assert len(result) == 1
    assert result[0].success
    assert result[0].return_data == b"\x01\x02"

    # Verify the outgoing data starts with aggregate3 selector
    sent_data = rpc.eth_call.call_args[0][1]
    assert sent_data[2:10] == AGGREGATE3_SELECTOR
