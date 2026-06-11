from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.shadow.feasibility import FeasibilityGate, _sig_bytes, validate_solution

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
async def test_block_defaults_to_latest() -> None:
    """Omitting block keeps the historical default of simulating at 'latest'."""
    cache, api, rpc = _deps(rpc_result=(True, "0x"))
    await validate_solution(
        _SOLUTION, cache=cache, api=api, rpc=rpc,
        settlement_addr="0xset", solver_addr="0xslv",
    )
    assert rpc.eth_call_capture.await_args.kwargs["block"] == "latest"


@pytest.mark.asyncio
async def test_block_is_forwarded_to_eth_call() -> None:
    """A caller-supplied block (the competition sim block) reaches the RPC so
    feasibility is judged against auction-time state, not 'latest'."""
    cache, api, rpc = _deps(rpc_result=(True, "0x"))
    await validate_solution(
        _SOLUTION, cache=cache, api=api, rpc=rpc,
        settlement_addr="0xset", solver_addr="0xslv", block="0x123abc",
    )
    assert rpc.eth_call_capture.await_args.kwargs["block"] == "0x123abc"


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


# ── _sig_bytes: scheme-specific trade-signature framing ─────────────────────
# GPv2Signing reads the trade signature differently per scheme. Getting this
# wrong silently condemned every eip1271 solution (117/117 on 2026-06-11) and
# produced the historical "malformed presignature" artifact for presign.

_OWNER = "0x" + "ab" * 20


def test_sig_bytes_ecdsa_passes_raw_signature() -> None:
    o = {"signature": "0x" + "11" * 65, "signingScheme": "eip712", "owner": _OWNER}
    assert _sig_bytes(o) == bytes.fromhex("11" * 65)


def test_sig_bytes_eip1271_prefixes_owner() -> None:
    """GPv2 reads signature[0:20] as the verifier address — without the owner
    prefix it staticcalls a garbage address decoded from ABI padding, whose
    empty returndata makes GPv2 revert with EMPTY data (the silent-phantom
    class)."""
    payload = "00" * 12 + "af" * 20 + "cd" * 100  # ABI-ish, NOT owner-prefixed
    o = {"signature": "0x" + payload, "signingScheme": "eip1271", "owner": _OWNER}
    out = _sig_bytes(o)
    assert out[:20] == bytes.fromhex("ab" * 20)        # owner first
    assert out[20:] == bytes.fromhex(payload)          # payload verbatim


def test_sig_bytes_eip1271_does_not_double_prefix() -> None:
    already = bytes.fromhex("ab" * 20 + "cd" * 64)
    o = {"signature": "0x" + already.hex(), "signingScheme": "eip1271", "owner": _OWNER}
    assert _sig_bytes(o) == already


def test_sig_bytes_presign_is_exactly_the_owner() -> None:
    """presign trade signature = the 20-byte owner. The API sends '0x';
    passing that through produced 'GPv2: malformed presignature'."""
    o = {"signature": "0x", "signingScheme": "presign", "owner": _OWNER}
    assert _sig_bytes(o) == bytes.fromhex("ab" * 20)


def test_sig_bytes_smart_wallet_without_owner_raises_to_unknown() -> None:
    """eip1271/presign without a usable owner can NOT be framed — raising lets
    validate_solution map it to UNKNOWN instead of re-creating the silent
    phantom (reviewer-flagged wrong-direction failure)."""
    for scheme in ("eip1271", "presign"):
        with pytest.raises(ValueError):
            _sig_bytes({"signature": "0x" + "cd" * 32, "signingScheme": scheme})


def test_sig_bytes_missing_scheme_defaults_to_raw() -> None:
    o = {"signature": "0x" + "22" * 65, "owner": _OWNER}
    assert _sig_bytes(o) == bytes.fromhex("22" * 65)


# ── FeasibilityGate: the hard pre-submission wrapper ────────────────────────


@pytest.mark.asyncio
async def test_gate_check_forwards_deps_and_latest_block() -> None:
    """The gate threads its stored deps + block='latest' into validate_solution."""
    cache, api, rpc = _deps(rpc_result=(True, "0x"))
    gate = FeasibilityGate(
        cache=cache, api=api, rpc=rpc,
        settlement_addr="0xset", solver_addr="0xslv",
    )
    v = await gate.check(_SOLUTION)
    assert v.feasible is True
    assert rpc.eth_call_capture.await_args.kwargs["block"] == "latest"
    assert rpc.eth_call_capture.await_args.kwargs["from_addr"] == "0xslv"
    assert rpc.eth_call_capture.await_args.kwargs["to"] == "0xset"


@pytest.mark.asyncio
async def test_gate_check_propagates_phantom_verdict() -> None:
    cache, api, rpc = _deps(rpc_result=(False, "execution reverted: STF"))
    gate = FeasibilityGate(
        cache=cache, api=api, rpc=rpc,
        settlement_addr="0xset", solver_addr="0xslv",
    )
    v = await gate.check(_SOLUTION)
    assert v.feasible is False
    assert "STF" in v.reason


@pytest.mark.asyncio
async def test_gate_check_propagates_unknown_verdict() -> None:
    """Infra failure → UNKNOWN propagates unchanged (caller treats as no-submit)."""
    cache, api, rpc = _deps(rpc_result=(True, "0x"))
    rpc.eth_call_capture = AsyncMock(side_effect=TimeoutError("rpc down"))
    gate = FeasibilityGate(
        cache=cache, api=api, rpc=rpc,
        settlement_addr="0xset", solver_addr="0xslv",
    )
    v = await gate.check(_SOLUTION)
    assert v.feasible is None
