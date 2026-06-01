from __future__ import annotations

import pytest

from src.encoder.settle import encode_trade_flags


def test_sell_fok_eip712_is_zero() -> None:
    # sell + fill-or-kill + erc20/erc20 + eip712 => all bits clear
    assert encode_trade_flags("sell", False, "eip712") == 0


def test_buy_bit_set() -> None:
    assert encode_trade_flags("buy", False, "eip712") == 0x01


def test_partially_fillable_bit_set() -> None:
    assert encode_trade_flags("sell", True, "eip712") == 0x02


def test_eip1271_scheme_bits() -> None:
    # eip1271 = scheme index 2, shifted left 5 => 0x40
    assert encode_trade_flags("sell", False, "eip1271") == 0x40


def test_presign_scheme_bits() -> None:
    assert encode_trade_flags("sell", False, "presign") == 0x60


def test_ethsign_scheme_bits() -> None:
    assert encode_trade_flags("sell", False, "ethsign") == 0x20


def test_combined_buy_partial_eip1271() -> None:
    # 0x01 (buy) | 0x02 (partial) | 0x40 (eip1271) = 0x43
    assert encode_trade_flags("buy", True, "eip1271") == 0x43


def test_unknown_scheme_raises() -> None:
    with pytest.raises(ValueError, match="signing scheme"):
        encode_trade_flags("sell", False, "magic")


def test_sell_token_balance_external() -> None:
    assert encode_trade_flags("sell", False, "eip712", sell_token_balance="external") == 0x08


def test_sell_token_balance_internal() -> None:
    assert encode_trade_flags("sell", False, "eip712", sell_token_balance="internal") == 0x0C


def test_buy_token_balance_internal() -> None:
    assert encode_trade_flags("sell", False, "eip712", buy_token_balance="internal") == 0x10
