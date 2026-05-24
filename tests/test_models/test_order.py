"""Tests for the Order model's EIP-1271 / smart-wallet detection.

We only test the derived flag here — wire-shape parsing is exercised
transitively in ``test_auction.py``.  The flag drives downstream branching
for any check that would otherwise require a per-auction on-chain call
(EIP-1271 ``isValidSignature``, pre-sign approval lookup), so getting the
classification right is the whole point.
"""

from __future__ import annotations

import pytest

from src.models.order import Order


def _base_order(**overrides: object) -> dict:
    """Minimal valid raw Order JSON, with overrides applied last."""
    base: dict[str, object] = {
        "uid": "0x" + "ab" * 56,
        "sellToken": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
        "buyToken": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
        "sellAmount": "1000000000000000000",
        "buyAmount": "3450000000",
        "feePolicies": [],
        "validTo": 9999999999,
        "kind": "sell",
        "owner": "0x" + "bb" * 20,
        "partiallyFillable": False,
        "class": "market",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("signing_scheme", "expected"),
    [
        ("presign", True),
        ("eip1271", True),
        ("erc1271", True),
        # Case-insensitive — CoW API has historically capitalised these.
        ("EIP1271", True),
        ("PreSign", True),
        ("eoa", False),
        ("ecdsa", False),
        # Unknown values default to "treat as EOA" — conservative, since we
        # only branch on True to skip checks; False keeps the standard path.
        ("something_weird", False),
    ],
)
def test_is_smart_wallet_signed_classification(
    signing_scheme: str, expected: bool
) -> None:
    raw = _base_order(signingScheme=signing_scheme)
    order = Order.model_validate(raw)
    assert order.signing_scheme == signing_scheme
    assert order.is_smart_wallet_signed is expected


def test_is_smart_wallet_signed_missing_field_defaults_false() -> None:
    """No signingScheme in the raw JSON → treat as EOA.

    Older fixtures + EOA-only auctions omit the field; the flag must not
    raise and must default False so downstream code keeps the standard
    settlement path.
    """
    raw = _base_order()
    assert "signingScheme" not in raw
    order = Order.model_validate(raw)
    assert order.signing_scheme is None
    assert order.is_smart_wallet_signed is False
