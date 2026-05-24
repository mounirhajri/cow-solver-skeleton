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


def test_unknown_signing_scheme_logs_warning_once() -> None:
    """Spec §4: a signingScheme value outside the known sets (smart-wallet
    or EOA) must trigger the dedup-set membership the first time it appears,
    so the warning fires once per novel value per process lifetime.

    Soft failure mode otherwise: unknown scheme silently maps to EOA,
    downstream branching keyed on the flag would behave wrong with no signal.

    Asserting on the dedup-set is more robust than capturing structlog's
    PrintLoggerFactory output (writes to stdout directly, not via stdlib).
    """
    from src.models import order as order_mod

    order_mod._warned_unknown_schemes.clear()

    Order.model_validate(_base_order(uid="0x" + "11" * 56, signingScheme="future-scheme-name"))
    assert "future-scheme-name" in order_mod._warned_unknown_schemes, (
        "first occurrence must register in the dedup set"
    )

    # Second call with the same scheme: set membership unchanged, warning
    # path is short-circuited.  Test by reading the set itself rather than
    # the logger output (structlog → stdout bypasses caplog).
    snapshot_before = order_mod._warned_unknown_schemes.copy()
    Order.model_validate(_base_order(uid="0x" + "22" * 56, signingScheme="future-scheme-name"))
    assert order_mod._warned_unknown_schemes == snapshot_before, (
        "second occurrence must not re-register (idempotent dedup)"
    )


def test_known_schemes_do_not_register_unknown() -> None:
    """eip712 / ethsign / eip1271 / presign / erc1271 are all expected — must
    NOT register in the unknown-scheme dedup set, otherwise legitimate
    schemes silently get flagged as novel."""
    from src.models import order as order_mod

    order_mod._warned_unknown_schemes.clear()

    for scheme in ("eip712", "ethsign", "eip1271", "presign", "erc1271", "PreSign", "EIP712"):
        Order.model_validate(_base_order(signingScheme=scheme))

    assert order_mod._warned_unknown_schemes == set(), (
        f"known schemes must not be flagged as unknown; "
        f"got {order_mod._warned_unknown_schemes}"
    )
