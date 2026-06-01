"""Gate-logic tests for scripts.smoke_settle_decode (no live RPC needed)."""

from __future__ import annotations

from eth_abi import encode

from scripts.smoke_settle_decode import _check_one, _first_diff, _to_bytes
from src.encoder.settle import _SETTLE_ARG_TYPES as _TYPES
from src.encoder.settle import (
    SETTLE_SELECTOR,
    SettleTrade,
    encode_settle,
)

_A = "0x1111111111111111111111111111111111111111"
_B = "0x2222222222222222222222222222222222222222"


def _amm_only_calldata() -> bytes:
    trade = SettleTrade(
        sell_token_index=0,
        buy_token_index=1,
        receiver=_A,
        sell_amount=1000,
        buy_amount=900,
        valid_to=1900000000,
        app_data=b"\x00" * 32,
        fee_amount=0,
        flags=0x40,
        executed_amount=1000,
        signature=bytes.fromhex("abcd"),
    )
    return encode_settle(
        tokens=[_A, _B],
        clearing_prices=[900, 1000],
        trades=[trade],
        intra_interactions=[(_B, 0, bytes.fromhex("dead"))],
    )


def test_check_one_passes_on_amm_only_settlement() -> None:
    assert _check_one("0xfeed", _amm_only_calldata()) is True


def test_check_one_passes_with_appended_solver_metadata() -> None:
    # Real CoW settlements append non-ABI metadata bytes after the encoded args.
    # Our clean ABI encoding is a prefix of the real calldata → still a PASS.
    real = _amm_only_calldata() + bytes.fromhex("0000000000719402")
    assert _check_one("0xtagged", real) is True


def test_check_one_skips_non_settle_calldata() -> None:
    assert _check_one("0xbad", bytes.fromhex("deadbeef") + b"\x00" * 32) is None


def test_check_one_skips_settlement_with_pre_post_hooks() -> None:
    # A settle() with a non-empty PRE interaction — out of scope for our
    # AMM-only encoder, so the gate must skip (None), never mismatch (False).
    args = encode(_TYPES, [[_A], [1], [], [[(_A, 0, b"")], [], []]])
    calldata = SETTLE_SELECTOR + args
    assert _check_one("0xhooked", calldata) is None


def test_to_bytes_accepts_hex_and_bytes() -> None:
    assert _to_bytes("0xabcd") == bytes.fromhex("abcd")
    assert _to_bytes(b"\x01\x02") == b"\x01\x02"


def test_first_diff_reports_offset() -> None:
    assert _first_diff(b"\x00\x01\x02", b"\x00\x09\x02") == 1
    assert _first_diff(b"\x00\x01", b"\x00\x01") == -1
