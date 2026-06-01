from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.shadow.feasibility import Verdict, validate_solution

_SELL = "0x1111111111111111111111111111111111111111"
_BUY = "0x2222222222222222222222222222222222222222"
_RCV = "0x3333333333333333333333333333333333333333"
_UID = "0xabc"

_SOLUTION = {
    "prices": {_SELL: "900", _BUY: "1000"},
    "trades": [{"kind": "fulfillment", "orderUid": _UID, "executedAmount": "1000"}],
    "interactions": [{"target": _BUY, "value": "0", "callData": "0xdead"}],
}

_ORDER = {
    "uid": _UID,
    "sellToken": _SELL,
    "buyToken": _BUY,
    "sellAmount": "1000",
    "buyAmount": "900",
    "validTo": 1900000000,
    "appData": "0x" + "00" * 32,
    "feeAmount": "0",
    "kind": "sell",
    "partiallyFillable": False,
    "receiver": _RCV,
    "signingScheme": "eip712",
    "signature": "0x" + "ab" * 65,
}


def _deps(order=_ORDER, rpc_result=(True, "0x01")):
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=order)
    cache.set = AsyncMock()
    api = AsyncMock()
    api.fetch_order = AsyncMock(return_value=order)
    rpc = AsyncMock()
    rpc.eth_call_capture = AsyncMock(return_value=rpc_result)
    return cache, api, rpc


@pytest.mark.asyncio
async def test_feasible_when_settle_succeeds() -> None:
    cache, api, rpc = _deps(rpc_result=(True, "0x"))
    v = await validate_solution(
        _SOLUTION, cache=cache, api=api, rpc=rpc,
        settlement_addr="0xset", solver_addr="0xslv",
    )
    assert v.feasible is True
    assert v.reason is None


@pytest.mark.asyncio
async def test_phantom_when_settle_reverts() -> None:
    cache, api, rpc = _deps(rpc_result=(False, "execution reverted: TransferFailed"))
    v = await validate_solution(
        _SOLUTION, cache=cache, api=api, rpc=rpc,
        settlement_addr="0xset", solver_addr="0xslv",
    )
    assert v.feasible is False
    assert "TransferFailed" in v.reason


@pytest.mark.asyncio
async def test_unknown_when_order_unfetchable() -> None:
    cache, api, rpc = _deps()
    cache.get = AsyncMock(return_value=None)
    api.fetch_order = AsyncMock(return_value=None)  # 404
    v = await validate_solution(
        _SOLUTION, cache=cache, api=api, rpc=rpc,
        settlement_addr="0xset", solver_addr="0xslv",
    )
    assert v.feasible is None
    rpc.eth_call_capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_hit_skips_api_fetch() -> None:
    cache, api, rpc = _deps(rpc_result=(True, "0x"))
    await validate_solution(
        _SOLUTION, cache=cache, api=api, rpc=rpc,
        settlement_addr="0xset", solver_addr="0xslv",
    )
    api.fetch_order.assert_not_awaited()  # served from cache


@pytest.mark.asyncio
async def test_cache_miss_fetches_then_populates() -> None:
    cache, api, rpc = _deps(rpc_result=(True, "0x"))
    cache.get = AsyncMock(return_value=None)  # miss → go to API
    await validate_solution(
        _SOLUTION, cache=cache, api=api, rpc=rpc,
        settlement_addr="0xset", solver_addr="0xslv",
    )
    api.fetch_order.assert_awaited_once_with(_UID)
    cache.set.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_when_rpc_raises() -> None:
    """An RPC-layer exception is an infra gap → None, never False (phantom)."""
    cache, api, rpc = _deps(rpc_result=(True, "0x"))
    rpc.eth_call_capture = AsyncMock(side_effect=TimeoutError("rpc down"))
    v = await validate_solution(
        _SOLUTION, cache=cache, api=api, rpc=rpc,
        settlement_addr="0xset", solver_addr="0xslv",
    )
    assert v.feasible is None  # NOT False — must not be conflated with phantom


@pytest.mark.asyncio
async def test_multi_trade_resolves_each_order() -> None:
    """Two fulfillment trades → both orders resolved, single settle() eth_call."""
    sell2 = "0x4444444444444444444444444444444444444444"
    buy2 = "0x5555555555555555555555555555555555555555"
    uid2 = "0xdef"
    order2 = dict(_ORDER, uid=uid2, sellToken=sell2, buyToken=buy2)
    solution = {
        "prices": {_SELL: "900", _BUY: "1000", sell2: "800", buy2: "1100"},
        "trades": [
            {"kind": "fulfillment", "orderUid": _UID, "executedAmount": "1000"},
            {"kind": "fulfillment", "orderUid": uid2, "executedAmount": "500"},
        ],
        "interactions": [],
    }
    cache = AsyncMock()
    cache.get = AsyncMock(side_effect=lambda uid: _ORDER if uid == _UID else order2)
    cache.set = AsyncMock()
    api = AsyncMock()
    rpc = AsyncMock()
    rpc.eth_call_capture = AsyncMock(return_value=(True, "0x"))
    v = await validate_solution(
        solution, cache=cache, api=api, rpc=rpc,
        settlement_addr="0xset", solver_addr="0xslv",
    )
    assert v.feasible is True
    assert cache.get.await_count == 2
    rpc.eth_call_capture.assert_awaited_once()
