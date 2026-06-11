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


# ── aggregate_resilient: survive node gas-cap overflow ──────────────────────


def _call(i: int) -> Call:
    return Call(target="0x" + f"{i:02x}" * 20, call_data="0x" + f"{i:02x}")


@pytest.mark.asyncio
async def test_aggregate_resilient_empty_returns_empty() -> None:
    mc = Multicall3(AsyncMock())
    assert await mc.aggregate_resilient([]) == []


@pytest.mark.asyncio
async def test_aggregate_resilient_passthrough_when_fits() -> None:
    from src.routing.multicall import CallResult
    mc = Multicall3(AsyncMock())

    async def fake(calls: list[Call], block: str = "latest") -> list[CallResult]:
        return [CallResult(success=True, return_data=b"\x01") for _ in calls]

    mc.aggregate = fake  # type: ignore[assignment]
    res = await mc.aggregate_resilient([_call(0), _call(1)])
    assert [r.success for r in res] == [True, True]


@pytest.mark.asyncio
async def test_aggregate_resilient_bisects_on_overflow_and_stays_aligned() -> None:
    """A batch over the cap is bisected; results stay positionally aligned."""
    from src.routing.multicall import CallResult
    mc = Multicall3(AsyncMock())

    async def fake(calls: list[Call], block: str = "latest") -> list[CallResult]:
        if len(calls) > 2:
            raise RuntimeError("RPC error -32000: out of gas")
        # Echo the call index (last byte of target) so we can assert alignment.
        return [CallResult(success=True, return_data=bytes([int(c.target[-2:], 16)]))
                for c in calls]

    mc.aggregate = fake  # type: ignore[assignment]
    calls = [_call(i) for i in range(5)]
    res = await mc.aggregate_resilient(calls)
    assert [r.return_data[0] for r in res] == [0, 1, 2, 3, 4]  # order preserved


@pytest.mark.asyncio
async def test_aggregate_resilient_drops_single_overcap_call() -> None:
    from src.routing.multicall import CallResult
    mc = Multicall3(AsyncMock())

    async def fake(calls: list[Call], block: str = "latest") -> list[CallResult]:
        # Any multi-call batch overflows; the poison singleton (_call(1),
        # call_data "0x01") overflows even alone; other singletons succeed.
        if len(calls) > 1 or calls[0].call_data == "0x01":
            raise RuntimeError("RPC error -32000: out of gas")
        return [CallResult(success=True, return_data=b"\x09")]

    mc.aggregate = fake  # type: ignore[assignment]
    res = await mc.aggregate_resilient([_call(0), _call(1), _call(2)])
    assert len(res) == 3
    # Index 1 is the poison call → dropped (failed); 0 and 2 resolve.
    assert [r.success for r in res] == [True, False, True]


@pytest.mark.asyncio
async def test_aggregate_resilient_reraises_non_gas_error() -> None:
    mc = Multicall3(AsyncMock())

    async def fake(_calls: list[Call], block: str = "latest") -> list:
        raise RuntimeError("RPC error 500: internal server error")

    mc.aggregate = fake  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="internal server error"):
        await mc.aggregate_resilient([_call(0), _call(1)])
