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
    reconstruct_clearing_prices_from_executed,
    score_at_external_prices,
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


# ── score_at_external_prices (Phase 4a) ──────────────────────────────────────


def _sell_setup() -> tuple[dict, dict, dict]:
    ETH = 10**18
    orders = {
        "0xabc": {
            "sellToken": "0xSELL",
            "buyToken": "0xBUY",
            "sellAmount": str(1000 * ETH),
            "buyAmount": str(900 * ETH),
            "kind": "sell",
        }
    }
    solution = {
        "prices": {"0xsell": str(11 * ETH), "0xbuy": str(10 * ETH)},
        "trades": [
            {
                "kind": "fulfillment",
                "orderUid": "0xabc",
                "executedAmount": str(1000 * ETH),
            }
        ],
    }
    native = {"0xbuy": ETH}
    return solution, orders, native


def test_score_at_external_prices_identity_matches_compute() -> None:
    """Passing the solution's own prices reproduces compute_solution_score."""
    solution, orders, native = _sell_setup()
    expected = compute_solution_score(solution, orders, native)
    assert (
        score_at_external_prices(solution, orders, native, solution["prices"]) == expected
    )


def test_score_at_external_prices_does_not_mutate_input() -> None:
    solution, orders, native = _sell_setup()
    snapshot = dict(solution["prices"])
    ETH = 10**18
    other = {"0xsell": str(20 * ETH), "0xbuy": str(10 * ETH)}
    score_at_external_prices(solution, orders, native, other)
    assert solution["prices"] == snapshot


def test_score_at_external_prices_substitution_changes_score() -> None:
    """Different clearing prices deterministically change the score."""
    solution, orders, native = _sell_setup()
    own = compute_solution_score(solution, orders, native)
    ETH = 10**18
    # Better external price: 2.0 instead of 1.1  →  larger surplus
    better = {"0xsell": str(2 * ETH), "0xbuy": str(ETH)}
    new_score = score_at_external_prices(solution, orders, native, better)
    assert new_score > own
    # Worse external price (at the limit) → zero
    at_limit = {"0xsell": str(9 * ETH), "0xbuy": str(10 * ETH)}
    assert score_at_external_prices(solution, orders, native, at_limit) == 0


def test_score_at_external_prices_missing_token_skips_trade() -> None:
    """If a trade's token has no entry in clearing_prices, the trade scores 0."""
    solution, orders, native = _sell_setup()
    # Drop the buy-token price → cp_buy = 0 → trade skipped
    ETH = 10**18
    partial = {"0xsell": str(11 * ETH)}
    assert score_at_external_prices(solution, orders, native, partial) == 0


def test_score_at_external_prices_empty_returns_zero() -> None:
    solution, orders, native = _sell_setup()
    assert score_at_external_prices(solution, orders, native, {}) == 0


def test_score_at_external_prices_lowercases_keys() -> None:
    """Mixed-case external clearing prices must be normalised."""
    solution, orders, native = _sell_setup()
    ETH = 10**18
    mixed = {"0xSELL": str(11 * ETH), "0xBUY": str(10 * ETH)}
    expected = compute_solution_score(solution, orders, native)
    assert score_at_external_prices(solution, orders, native, mixed) == expected


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


# ── reconstruct_clearing_prices_from_executed (Arbitrum empty-clearingPrices) ─


def test_reconstruct_single_order_builds_pair_prices() -> None:
    # Winner filled one order: sold 1e18 sell-token, received 3000 buy-token.
    # price[sell] = executed_buy, price[buy] = executed_sell.
    winner_sol = {
        "orders": [
            {"id": "0xABC", "sellAmount": "1000000000000000000", "buyAmount": "3000"}
        ]
    }
    uid_map = {"0xabc": {"sellToken": "0xSell", "buyToken": "0xBuy"}}
    cp = reconstruct_clearing_prices_from_executed(winner_sol, uid_map)
    assert cp == {"0xsell": 3000, "0xbuy": 1000000000000000000}


def test_reconstruct_uses_camelcase_and_snakecase_token_keys() -> None:
    winner_sol = {"orders": [{"id": "0xAbC", "sellAmount": "10", "buyAmount": "20"}]}
    uid_map = {"0xabc": {"sell_token": "0xSELL", "buy_token": "0xBUY"}}
    cp = reconstruct_clearing_prices_from_executed(winner_sol, uid_map)
    assert cp == {"0xsell": 20, "0xbuy": 10}


def test_reconstruct_returns_empty_when_uid_not_in_map() -> None:
    winner_sol = {"orders": [{"id": "0xABC", "sellAmount": "10", "buyAmount": "20"}]}
    assert reconstruct_clearing_prices_from_executed(winner_sol, {}) == {}


def test_reconstruct_returns_empty_for_multi_pair_winner() -> None:
    # Two different pairs → 4 distinct tokens → no consistent 2-token vector.
    winner_sol = {
        "orders": [
            {"id": "0x1", "sellAmount": "10", "buyAmount": "20"},
            {"id": "0x2", "sellAmount": "30", "buyAmount": "40"},
        ]
    }
    uid_map = {
        "0x1": {"sellToken": "0xA", "buyToken": "0xB"},
        "0x2": {"sellToken": "0xC", "buyToken": "0xD"},
    }
    assert reconstruct_clearing_prices_from_executed(winner_sol, uid_map) == {}


def test_reconstruct_skips_zero_and_nonnumeric_amounts() -> None:
    uid_map = {"0x1": {"sellToken": "0xA", "buyToken": "0xB"}}
    assert reconstruct_clearing_prices_from_executed(
        {"orders": [{"id": "0x1", "sellAmount": "0", "buyAmount": "20"}]}, uid_map
    ) == {}
    assert reconstruct_clearing_prices_from_executed(
        {"orders": [{"id": "0x1", "sellAmount": "x", "buyAmount": "20"}]}, uid_map
    ) == {}


def test_reconstruct_empty_inputs() -> None:
    assert reconstruct_clearing_prices_from_executed(None, {}) == {}
    assert reconstruct_clearing_prices_from_executed({}, {}) == {}
    assert reconstruct_clearing_prices_from_executed({"orders": []}, {}) == {}


def test_reconstruct_feeds_score_at_external_prices() -> None:
    # End-to-end: reconstructed winner prices used to re-score our trade.
    # Our solution sold 1e18 sell-token and (claims) 2900 buy-token; the winner
    # achieved 3000 for the same input → our score at winner prices is higher.
    uid = "0xabc"
    uid_map = {
        uid: {
            "sellToken": "0xsell",
            "buyToken": "0xbuy",
            "sellAmount": 1000000000000000000,
            "buyAmount": 2000,  # limit
            "kind": "sell",
        }
    }
    winner_sol = {
        "orders": [
            {"id": uid, "sellAmount": "1000000000000000000", "buyAmount": "3000"}
        ]
    }
    cp = reconstruct_clearing_prices_from_executed(winner_sol, uid_map)
    assert cp  # non-empty
    our_solution = {
        "prices": {"0xsell": 2900, "0xbuy": 1000000000000000000},
        "trades": [
            {
                "kind": "fulfillment",
                "orderUid": uid,
                "executedAmount": "1000000000000000000",
            }
        ],
    }
    native = {"0xbuy": 10**18}  # 1:1 native price for the buy token
    winner_priced = score_at_external_prices(our_solution, uid_map, native, cp)
    own_priced = score_at_external_prices(
        our_solution, uid_map, native, our_solution["prices"]
    )
    # At the winner's better price our trade yields more surplus.
    assert winner_priced > own_priced
