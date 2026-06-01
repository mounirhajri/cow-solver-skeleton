from __future__ import annotations

from eth_abi import decode
from eth_utils import keccak

from src.encoder.settle import SETTLE_SELECTOR, SettleTrade, encode_settle

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
