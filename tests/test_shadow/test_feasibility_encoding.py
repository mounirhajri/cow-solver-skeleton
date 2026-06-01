"""Encoder-fidelity gate for the feasibility validator.

We assert the trade-flags bitfield reproduces values decoded from REAL
Arbitrum settle() transactions. If a known-good settlement's flags don't match,
the live eth_call would misread the order kind / signing scheme and revert —
making every solution look phantom. This test catches that class of bug offline.

Fixture provenance: flags values below are taken from decoding GPv2Trade.flags
in settled Arbitrum settlement txs. Update with `cast` / a decoder if the
GPv2Trade layout ever changes (it has been stable since CIP-14).
"""

from __future__ import annotations

from src.encoder.settle import encode_trade_flags


def test_flags_fixture_sell_fok_eip712() -> None:
    # A plain sell, fill-or-kill, EOA-signed order → flags == 0x00.
    assert encode_trade_flags("sell", False, "eip712") == 0x00


def test_flags_fixture_sell_fok_eip1271() -> None:
    # Smart-wallet (Safe) sell order, fill-or-kill → flags == 0x40.
    assert encode_trade_flags("sell", False, "eip1271") == 0x40


def test_flags_fixture_sell_partial_eip712() -> None:
    # TWAP child orders are partiallyFillable sells, EOA-signed → 0x02.
    assert encode_trade_flags("sell", True, "eip712") == 0x02
