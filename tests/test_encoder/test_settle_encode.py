from __future__ import annotations

import pytest
from eth_abi import decode
from eth_utils import keccak

from src.encoder.settle import (
    SETTLE_SELECTOR,
    DecodedSettle,
    SettleTrade,
    decode_settle,
    encode_settle,
)

_A = "0x1111111111111111111111111111111111111111"
_B = "0x2222222222222222222222222222222222222222"
_RCV = "0x3333333333333333333333333333333333333333"


def test_selector_matches_signature() -> None:
    sig = (
        "settle(address[],uint256[],"
        "(uint256,uint256,address,uint256,uint256,uint32,bytes32,uint256,uint256,uint256,bytes)[],"
        "(address,uint256,bytes)[][3])"
    )
    assert keccak(text=sig)[:4] == SETTLE_SELECTOR


def test_encode_settle_roundtrips_via_abi_decode() -> None:
    trade = SettleTrade(
        sell_token_index=0,
        buy_token_index=1,
        receiver=_RCV,
        sell_amount=1000,
        buy_amount=900,
        valid_to=1900000000,
        app_data=b"\x00" * 32,
        fee_amount=0,
        flags=0x40,  # eip1271
        executed_amount=1000,
        signature=bytes.fromhex("abcd"),
    )
    calldata = encode_settle(
        tokens=[_A, _B],
        clearing_prices=[900, 1000],
        trades=[trade],
        intra_interactions=[(_B, 0, bytes.fromhex("dead"))],
    )
    assert calldata[:4] == SETTLE_SELECTOR

    tokens, prices, trades, interactions = decode(
        [
            "address[]",
            "uint256[]",
            "(uint256,uint256,address,uint256,uint256,uint32,bytes32,uint256,uint256,uint256,bytes)[]",
            "(address,uint256,bytes)[][3]",
        ],
        calldata[4:],
    )
    assert [t.lower() for t in tokens] == [_A, _B]
    assert prices == (900, 1000)
    assert trades[0][0] == 0 and trades[0][1] == 1
    assert trades[0][10] == bytes.fromhex("abcd")  # signature
    # pre / intra / post
    assert interactions[0] == ()        # pre empty
    assert interactions[1][0][2] == bytes.fromhex("dead")  # intra callData
    assert interactions[2] == ()        # post empty


# ── decode_settle (the encoder-fidelity gate's inverse) ───────────────────────


def _sample_calldata() -> bytes:
    """A representative AMM-only settle(): 2 tokens, 1 trade, 1 intra interaction."""
    trade = SettleTrade(
        sell_token_index=0,
        buy_token_index=1,
        receiver=_RCV,
        sell_amount=1000,
        buy_amount=900,
        valid_to=1900000000,
        app_data=bytes.fromhex("11" * 32),
        fee_amount=7,
        flags=0x42,  # eip1271 + partially fillable
        executed_amount=1000,
        signature=bytes.fromhex("abcd"),
    )
    return encode_settle(
        tokens=[_A, _B],
        clearing_prices=[900, 1000],
        trades=[trade],
        intra_interactions=[(_B, 0, bytes.fromhex("dead"))],
    )


def test_decode_settle_recovers_every_field() -> None:
    decoded = decode_settle(_sample_calldata())
    assert isinstance(decoded, DecodedSettle)
    assert [t.lower() for t in decoded.tokens] == [_A, _B]
    assert decoded.clearing_prices == [900, 1000]
    assert decoded.pre_interactions == []
    assert decoded.post_interactions == []
    assert len(decoded.intra_interactions) == 1
    target, value, calldata = decoded.intra_interactions[0]
    assert target.lower() == _B
    assert value == 0
    assert calldata == bytes.fromhex("dead")

    assert len(decoded.trades) == 1
    t = decoded.trades[0]
    assert (t.sell_token_index, t.buy_token_index) == (0, 1)
    assert t.receiver.lower() == _RCV
    assert (t.sell_amount, t.buy_amount) == (1000, 900)
    assert t.valid_to == 1900000000
    assert t.app_data == bytes.fromhex("11" * 32)
    assert t.fee_amount == 7
    assert t.flags == 0x42
    assert t.executed_amount == 1000
    assert t.signature == bytes.fromhex("abcd")


def test_decode_settle_is_exact_inverse_of_encode_settle() -> None:
    """Ground-truth gate: re-encoding the decoded fields reproduces the bytes.

    Canonical eth_abi.decode parses our hand-rolled encoder's output; feeding it
    back through encode_settle must reproduce the exact calldata. Any ABI
    deviation (offsets, padding, ordering) would break byte-equality here.
    """
    calldata = _sample_calldata()
    decoded = decode_settle(calldata)
    reencoded = encode_settle(
        tokens=decoded.tokens,
        clearing_prices=decoded.clearing_prices,
        trades=decoded.trades,
        intra_interactions=decoded.intra_interactions,
    )
    assert reencoded == calldata


def test_decode_settle_handles_empty_trades_and_interactions() -> None:
    calldata = encode_settle(
        tokens=[_A], clearing_prices=[1], trades=[], intra_interactions=[]
    )
    decoded = decode_settle(calldata)
    assert decoded.trades == []
    assert decoded.intra_interactions == []
    assert encode_settle(
        tokens=decoded.tokens,
        clearing_prices=decoded.clearing_prices,
        trades=decoded.trades,
        intra_interactions=decoded.intra_interactions,
    ) == calldata


def test_decode_settle_rejects_wrong_selector() -> None:
    bad = bytes.fromhex("deadbeef") + _sample_calldata()[4:]
    with pytest.raises(ValueError, match="selector"):
        decode_settle(bad)
