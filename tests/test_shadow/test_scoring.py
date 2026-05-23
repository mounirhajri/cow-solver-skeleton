"""Tests for CIP-14 score computation (src/shadow/scoring.py).

Reference formula from cowprotocol/services:
  crates/driver/src/domain/competition/solution/scoring.rs
"""

from __future__ import annotations

import pytest

from src.shadow.scoring import (
    _ceil_div,
    compute_solution_score,
    extract_native_prices,
    orders_by_uid_from_auction,
)


# ── helpers ─────────────────────────────────────────────────────────────────


def test_ceil_div_basic() -> None:
    assert _ceil_div(10, 3) == 4
    assert _ceil_div(9, 3) == 3
    assert _ceil_div(1, 1) == 1
    assert _ceil_div(0, 5) == 0


def test_ceil_div_by_zero() -> None:
    with pytest.raises(ZeroDivisionError):
        _ceil_div(5, 0)


# ── sell order surplus ───────────────────────────────────────────────────────


def test_sell_order_positive_surplus() -> None:
    """Sell order where our price is better than limit price → positive score."""
    ETH = 10**18

    orders = {
        "0xabc": {
            "sellToken": "0xSELL",
            "buyToken": "0xBUY",
            "sellAmount": str(1000 * ETH),   # limit: sell 1000
            "buyAmount": str(900 * ETH),     # limit: receive at least 900
            "kind": "sell",
        }
    }
    solution = {
        # cp_sell / cp_buy = 1.1  → bought = 1100, limit_buy = 900  → surplus 200
        "prices": {"0xsell": str(11 * ETH), "0xbuy": str(10 * ETH)},
        "trades": [{"kind": "fulfillment", "orderUid": "0xabc",
                    "executedAmount": str(1000 * ETH)}],
    }
    # native_price_buy = 1 ETH per token → score in wei = surplus_buy * 1
    native_prices = {"0xbuy": ETH}

    score = compute_solution_score(solution, orders, native_prices)
    # surplus_buy = ceil(1000 * 900/1000) vs ceil(1000 * 11/10)
    # limit_buy = ceil(1000e18 * 900e18 / 1000e18) = 900e18
    # bought    = ceil(1000e18 * 11e18 / 10e18) = 1100e18
    # surplus   = 200e18 tokens  * 1e18 native / 1e18 = 200e18 wei
    assert score == 200 * ETH


def test_sell_order_zero_surplus_at_limit() -> None:
    """Sold exactly at limit price → zero surplus."""
    ETH = 10**18
    orders = {
        "uid1": {
            "sellToken": "0xa",
            "buyToken": "0xb",
            "sellAmount": str(100 * ETH),
            "buyAmount": str(100 * ETH),  # 1:1
            "kind": "sell",
        }
    }
    solution = {
        "prices": {"0xa": str(ETH), "0xb": str(ETH)},  # 1:1
        "trades": [{"kind": "fulfillment", "orderUid": "uid1",
                    "executedAmount": str(100 * ETH)}],
    }
    native_prices = {"0xb": ETH}
    assert compute_solution_score(solution, orders, native_prices) == 0


def test_sell_order_below_limit_ignored() -> None:
    """Price worse than limit (bought < limit_buy) → surplus clamped to 0."""
    ETH = 10**18
    orders = {
        "uid1": {
            "sellToken": "0xa",
            "buyToken": "0xb",
            "sellAmount": str(100 * ETH),
            "buyAmount": str(100 * ETH),  # expect 1:1
            "kind": "sell",
        }
    }
    solution = {
        "prices": {"0xa": str(9 * ETH // 10), "0xb": str(ETH)},  # 0.9:1 — worse
        "trades": [{"kind": "fulfillment", "orderUid": "uid1",
                    "executedAmount": str(100 * ETH)}],
    }
    native_prices = {"0xb": ETH}
    assert compute_solution_score(solution, orders, native_prices) == 0


# ── buy order surplus ────────────────────────────────────────────────────────


def test_buy_order_positive_surplus() -> None:
    """Buy order where we spent less sell-token than the limit → positive score."""
    ETH = 10**18
    orders = {
        "uid2": {
            "sellToken": "0xSELL",
            "buyToken": "0xBUY",
            "sellAmount": str(1100 * ETH),   # user willing to spend up to 1100
            "buyAmount": str(1000 * ETH),    # wants exactly 1000 buy-tokens
            "kind": "buy",
        }
    }
    solution = {
        # cp_buy / cp_sell = 1.0 → sold = executed * 1.0 = 1000
        # limit_sell = executed * signed_sell / signed_buy = 1000*1100/1000 = 1100
        # surplus_sell = 1100 - 1000 = 100 (sell tokens saved)
        # convert: surplus_buy = 100 * signed_buy/signed_sell = 100 * 1000/1100 ≈ 90
        "prices": {"0xsell": str(ETH), "0xbuy": str(ETH)},  # 1:1
        "trades": [{"kind": "fulfillment", "orderUid": "uid2",
                    "executedAmount": str(1000 * ETH)}],
    }
    native_prices = {"0xbuy": ETH}

    score = compute_solution_score(solution, orders, native_prices)
    # surplus_sell = 1000e18*1100e18//1000e18 - 1000e18*1e18//1e18 = 1100e18 - 1000e18 = 100e18
    # surplus_buy  = 100e18 * 1000e18 // 1100e18 = 90909090909090909090 ≈ 90.9e18
    # score_wei    = surplus_buy * 1e18 // 1e18 = 90909090909090909090
    assert score == 100 * ETH * 1000 // 1100


# ── edge / missing data cases ────────────────────────────────────────────────


def test_empty_inputs_return_zero() -> None:
    assert compute_solution_score({}, {}, {}) == 0
    assert compute_solution_score(None, {}, {}) == 0  # type: ignore[arg-type]


def test_unknown_order_uid_skipped() -> None:
    ETH = 10**18
    solution = {
        "prices": {"0xa": str(ETH), "0xb": str(ETH)},
        "trades": [{"kind": "fulfillment", "orderUid": "unknown",
                    "executedAmount": str(ETH)}],
    }
    # orders_by_uid has no "unknown" → trade skipped
    assert compute_solution_score(solution, {"other": {}}, {"0xb": ETH}) == 0


def test_non_fulfillment_trades_ignored() -> None:
    ETH = 10**18
    solution = {
        "prices": {"0xa": str(ETH), "0xb": str(ETH)},
        "trades": [{"kind": "jit", "orderUid": "uid1", "executedAmount": str(ETH)}],
    }
    orders = {"uid1": {"sellToken": "0xa", "buyToken": "0xb",
                       "sellAmount": str(ETH), "buyAmount": str(ETH), "kind": "sell"}}
    assert compute_solution_score(solution, orders, {"0xb": ETH}) == 0


def test_missing_native_price_skips_trade() -> None:
    ETH = 10**18
    orders = {"uid1": {"sellToken": "0xa", "buyToken": "0xb",
                       "sellAmount": str(ETH), "buyAmount": str(ETH // 2), "kind": "sell"}}
    solution = {
        "prices": {"0xa": str(ETH), "0xb": str(ETH)},
        "trades": [{"kind": "fulfillment", "orderUid": "uid1",
                    "executedAmount": str(ETH)}],
    }
    # No native price for buy token → trade skipped
    assert compute_solution_score(solution, orders, {}) == 0


# ── extract_native_prices ────────────────────────────────────────────────────


def test_extract_native_prices_normal() -> None:
    ETH = 10**18
    rc = {"auction": {"prices": {"0xTOKEN": str(ETH), "0xOther": "500000000000000000"}}}
    result = extract_native_prices(rc)
    assert result["0xtoken"] == ETH
    assert result["0xother"] == ETH // 2


def test_extract_native_prices_missing() -> None:
    assert extract_native_prices({}) == {}
    assert extract_native_prices({"auction": {}}) == {}


# ── orders_by_uid_from_auction ───────────────────────────────────────────────


def test_orders_by_uid_from_dict() -> None:
    raw = {
        "orders": [
            {"uid": "UID1", "sellToken": "0xa", "buyToken": "0xb",
             "sellAmount": "1000", "buyAmount": "900", "kind": "sell"},
        ]
    }
    result = orders_by_uid_from_auction(raw)
    assert "uid1" in result
    assert result["uid1"]["sellToken"] == "0xa"


def test_orders_by_uid_from_pydantic() -> None:
    """Should accept Pydantic Auction model without crashing."""
    from src.models.auction import Auction

    data = {
        "id": "42",
        "tokens": {},
        "orders": [
            {
                "uid": "0xdeadbeef",
                "sellToken": "0xS",
                "buyToken": "0xB",
                "sellAmount": "1000",
                "buyAmount": "900",
                "feePolicies": [],
                "validTo": 9999999999,
                "kind": "sell",
                "owner": "0xOwner",
                "partiallyFillable": False,
                "class": "market",
            }
        ],
    }
    auction = Auction.model_validate(data)
    result = orders_by_uid_from_auction(auction)
    assert "0xdeadbeef" in result
    assert result["0xdeadbeef"]["sellToken"] == "0xS"
    assert result["0xdeadbeef"]["sellAmount"] == 1000
