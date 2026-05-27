"""Tests for the allowance-setup diagnose script.

Validates calldata encoding and the threshold / zero-reset logic against
synthetic Multicall3 returns. Real RPC is out of scope — that's covered
by the on-chain fork tests in tests/fork/.
"""

from __future__ import annotations

import pytest
from eth_abi import decode

from scripts.setup_v3_allowances import (
    _ALLOWANCE_THRESHOLD,
    _MAX_UINT256,
    _check_allowances,
    _encode_approve_calldata,
)
from src.routing.multicall import Call, CallResult


def _allowance_return(value: int) -> bytes:
    return value.to_bytes(32, "big")


@pytest.mark.asyncio
async def test_full_allowance_marks_no_approve_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Above-threshold allowance means the spender is already fully
    approved — script must NOT mark it as pending."""
    async def _fake_aggregate(_self: object, _calls: list[Call]) -> list[CallResult]:
        return [CallResult(success=True, return_data=_allowance_return(_MAX_UINT256))]

    monkeypatch.setattr("src.routing.multicall.Multicall3.aggregate", _fake_aggregate)
    checks = await _check_allowances(
        "http://fake", "0x" + "0" * 40, "0x" + "0" * 40, [("WETH", "0x" + "1" * 40)]
    )
    assert len(checks) == 1
    assert checks[0].needs_approve is False
    assert checks[0].needs_zero_reset is False


@pytest.mark.asyncio
async def test_zero_allowance_marks_approve_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_aggregate(_self: object, _calls: list[Call]) -> list[CallResult]:
        return [CallResult(success=True, return_data=_allowance_return(0))]

    monkeypatch.setattr("src.routing.multicall.Multicall3.aggregate", _fake_aggregate)
    checks = await _check_allowances(
        "http://fake", "0x" + "0" * 40, "0x" + "0" * 40, [("WETH", "0x" + "1" * 40)]
    )
    assert checks[0].needs_approve is True
    # Zero current → no USDT-style reset required
    assert checks[0].needs_zero_reset is False


@pytest.mark.asyncio
async def test_nonzero_but_low_allowance_flags_zero_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """USDT-style: existing non-zero allowance below threshold. Broadcaster
    must send approve(0) before approve(MAX) or the second tx reverts."""
    async def _fake_aggregate(_self: object, _calls: list[Call]) -> list[CallResult]:
        # 10 USDT worth of allowance — far below threshold
        return [CallResult(success=True, return_data=_allowance_return(10_000_000))]

    monkeypatch.setattr("src.routing.multicall.Multicall3.aggregate", _fake_aggregate)
    checks = await _check_allowances(
        "http://fake", "0x" + "0" * 40, "0x" + "0" * 40, [("USDT", "0x" + "1" * 40)]
    )
    assert checks[0].needs_approve is True
    assert checks[0].needs_zero_reset is True


@pytest.mark.asyncio
async def test_failed_read_treated_as_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multicall failure (e.g. non-existent contract at the address) is
    treated as ``needs_approve=True`` — safer to attempt than assume."""
    async def _fake_aggregate(_self: object, _calls: list[Call]) -> list[CallResult]:
        return [CallResult(success=False, return_data=b"")]

    monkeypatch.setattr("src.routing.multicall.Multicall3.aggregate", _fake_aggregate)
    checks = await _check_allowances(
        "http://fake", "0x" + "0" * 40, "0x" + "0" * 40, [("X", "0x" + "1" * 40)]
    )
    assert checks[0].needs_approve is True
    assert checks[0].current == 0


def test_encode_approve_calldata_matches_erc20_spec() -> None:
    """approve(address,uint256) calldata must decode to the inputs we
    passed, byte-for-byte. Mismatched encoding here would silently
    misbehave at the broadcasting step.
    """
    spender = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
    cd = _encode_approve_calldata(spender, _MAX_UINT256)
    # First 4 bytes = approve selector
    assert cd[:4].hex() == "095ea7b3"
    # Rest = (address, uint256)
    addr, amount = decode(["address", "uint256"], cd[4:])
    assert addr.lower() == spender.lower()
    assert amount == _MAX_UINT256


def test_threshold_is_unbounded_enough() -> None:
    """Threshold is 2^200 — well below MAX_UINT (2^256-1) so a normal
    MAX_UINT approve registers as "above threshold", but far above any
    realistic per-tx allowance amount (a 1000-token approval at 18
    decimals is only ~2^70)."""
    assert _ALLOWANCE_THRESHOLD < _MAX_UINT256
    assert _ALLOWANCE_THRESHOLD > 2**100  # plausible per-tx amounts can't reach this
