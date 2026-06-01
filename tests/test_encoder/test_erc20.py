from __future__ import annotations

from eth_abi import decode

from src.encoder.erc20 import APPROVE_SELECTOR, encode_approve

_SPENDER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"


def test_approve_selector_matches_erc20_spec() -> None:
    """keccak256("approve(address,uint256)")[:4] == 0x095ea7b3 (canonical)."""
    assert APPROVE_SELECTOR.hex() == "095ea7b3"


def test_encode_approve_prefixes_canonical_selector() -> None:
    cd = encode_approve(_SPENDER, 1_000)
    assert cd[:4].hex() == "095ea7b3"


def test_encode_approve_round_trips_args() -> None:
    amount = 123_456_789
    cd = encode_approve(_SPENDER, amount)
    spender, decoded_amount = decode(["address", "uint256"], cd[4:])
    assert spender.lower() == _SPENDER.lower()
    assert decoded_amount == amount


def test_encode_approve_returns_bytes() -> None:
    cd = encode_approve(_SPENDER, 0)
    assert isinstance(cd, bytes)
    # selector (4) + address word (32) + uint256 word (32)
    assert len(cd) == 4 + 32 + 32
