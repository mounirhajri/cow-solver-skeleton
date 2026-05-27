"""Tests for the LiquiditySource protocol and its companion dataclasses."""

import pytest

from src.liquidity.base import Quote, SwapRequest

USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"


def test_sell_swap_request_constructs_cleanly() -> None:
    r = SwapRequest(
        sell_token=USDC,
        buy_token=WETH,
        sell_amount=100_000_000,
        buy_amount=0,
        kind="sell",
        chain_id=42161,
    )
    assert r.kind == "sell"
    assert r.sell_amount == 100_000_000


def test_buy_swap_request_constructs_cleanly() -> None:
    r = SwapRequest(
        sell_token=USDC,
        buy_token=WETH,
        sell_amount=0,
        buy_amount=10**15,
        kind="buy",
        chain_id=42161,
    )
    assert r.kind == "buy"
    assert r.buy_amount == 10**15


def test_sell_kind_rejects_zero_sell_amount() -> None:
    """A sell-kind request with zero fixed amount is a caller bug — sources
    would either return None or divide by zero. Fail loudly at construction."""
    with pytest.raises(ValueError, match="positive sell_amount"):
        SwapRequest(
            sell_token=USDC,
            buy_token=WETH,
            sell_amount=0,
            buy_amount=0,
            kind="sell",
            chain_id=42161,
        )


def test_buy_kind_rejects_zero_buy_amount() -> None:
    with pytest.raises(ValueError, match="positive buy_amount"):
        SwapRequest(
            sell_token=USDC,
            buy_token=WETH,
            sell_amount=0,
            buy_amount=0,
            kind="buy",
            chain_id=42161,
        )


def test_swap_request_rejects_same_token_both_sides() -> None:
    """Same-token swaps are a degenerate case that no real liquidity source
    will quote sensibly. Reject at construction rather than wasting fan-out
    latency on it."""
    with pytest.raises(ValueError, match="must differ"):
        SwapRequest(
            sell_token=USDC,
            buy_token=USDC,
            sell_amount=100,
            buy_amount=0,
            kind="sell",
            chain_id=42161,
        )


def test_swap_request_case_insensitive_token_check() -> None:
    """Addresses can arrive in mixed checksum casing — the same-token check
    must be case-insensitive."""
    with pytest.raises(ValueError, match="must differ"):
        SwapRequest(
            sell_token=USDC.lower(),
            buy_token=USDC.upper(),
            sell_amount=100,
            buy_amount=0,
            kind="sell",
            chain_id=42161,
        )


def test_quote_carries_route_metadata_as_dict() -> None:
    """Sources stash routing-specific info (V3 fee tier, packed path, RFQ
    signature) in route_metadata. Treat it as opaque outside the producing
    source — the dataclass just holds it."""
    q = Quote(
        source="v3",
        sell_amount=100_000_000,
        buy_amount=345_000_000_000_000,
        valid_until=2**31 - 1,
        route_metadata={"fee": 500, "pool": "0xabc..."},
        gas_estimate=120_000,
    )
    assert q.route_metadata["fee"] == 500
    assert q.gas_estimate == 120_000
