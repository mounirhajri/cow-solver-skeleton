"""Tests for the EBBO (External Best Bid/Offer) pre-submission validator.

Behaviour the tests lock in:
  - Solution passes when every sell trade's user output meets/exceeds the
    external V3 quote within tolerance.
  - Solution fails when ANY trade falls short by more than tolerance.
  - Buy-kind trades and orders missing clearing prices are skipped
    (not failed) — they are not yet covered by this validator.
  - RPC errors in the quoter do NOT fail the solution; they're logged
    and treated as "no external comparison available, pass through".
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.models.auction import Auction
from src.models.order import Order
from src.models.solution import Solution, Trade
from src.solver.ebbo import (
    DEFAULT_TOLERANCE_BPS,
    EBBOResult,
    validate_solution_ebbo,
)

UID = "0x" + "a" * 112
UID2 = "0x" + "b" * 112


def _order(
    uid: str = UID,
    sell_token: str = "0xa",
    buy_token: str = "0xb",
    sell_amount: int = 1000,
    buy_amount: int = 900,
    kind: str = "sell",
) -> Order:
    return Order(
        uid=uid,
        sellToken=sell_token,
        buyToken=buy_token,
        sellAmount=sell_amount,
        buyAmount=buy_amount,
        feePolicies=[],
        validTo=99,
        kind=kind,
        owner="0x" + "1" * 40,
        partiallyFillable=False,
        **{"class": "limit"},
    )


def _auction(orders: list[Order]) -> Auction:
    return Auction(
        id="1",
        tokens={},
        orders=orders,
        liquidity=[],
        effectiveGasPrice=0,
        deadline=None,
    )


def _solution(trades: list[Trade], prices: dict[str, int]) -> Solution:
    return Solution(id=1, prices=prices, trades=trades, interactions=[])


def _hop(amount_out: int) -> Any:
    """Minimal duck for HopQuote used at end of path; only amount_out read."""

    class _H:
        def __init__(self, ao: int) -> None:
            self.amount_out = ao

    return _H(amount_out)


@pytest.fixture
def quoter(monkeypatch):
    """Replace quote_best_path with a tunable mock keyed by (sell, buy, amount)."""
    table: dict[tuple[str, str, int], int | None] = {}

    async def _stub(_mc, sell_token, buy_token, amount_in, _intermediates):
        key = (sell_token.lower(), buy_token.lower(), amount_in)
        amt = table.get(key)
        if amt is None:
            return None
        return [_hop(amt)]

    monkeypatch.setattr("src.solver.ebbo.quote_best_path", _stub)
    return table


@pytest.mark.asyncio
async def test_passes_when_our_buy_beats_external(quoter) -> None:
    o = _order(sell_amount=1000, buy_amount=900)
    auction = _auction([o])
    trade = Trade(kind="fulfillment", orderUid=UID, executedAmount=1000)
    solution = _solution([trade], prices={"0xa": 2, "0xb": 1})
    # User receives 1000 × 2 / 1 = 2000.  External returns 1900.
    quoter[("0xa", "0xb", 1000)] = 1900

    r = await validate_solution_ebbo(solution, auction, multicall=None, intermediates=[])
    assert r.passes
    assert r.n_checked == 1
    assert r.violations == []


@pytest.mark.asyncio
async def test_fails_when_our_buy_lags_beyond_tolerance(quoter) -> None:
    o = _order(sell_amount=1000, buy_amount=900)
    auction = _auction([o])
    trade = Trade(kind="fulfillment", orderUid=UID, executedAmount=1000)
    solution = _solution([trade], prices={"0xa": 1, "0xb": 2})
    # User receives 1000 × 1 / 2 = 500.  External returns 1000 (way more).
    quoter[("0xa", "0xb", 1000)] = 1000

    r = await validate_solution_ebbo(solution, auction, multicall=None, intermediates=[])
    assert not r.passes
    assert len(r.violations) == 1
    assert UID[:18] in r.violations[0]


@pytest.mark.asyncio
async def test_within_tolerance_still_passes(quoter) -> None:
    """Our output is 0.3 % below external — DEFAULT_TOLERANCE_BPS=50 allows up
    to 0.5 % below, so this should pass."""
    o = _order()
    auction = _auction([o])
    trade = Trade(kind="fulfillment", orderUid=UID, executedAmount=1000)
    # our_buy = 997.  ext_buy = 1000.  shortfall = 30 bps.
    solution = _solution([trade], prices={"0xa": 997, "0xb": 1000})
    quoter[("0xa", "0xb", 1000)] = 1000

    r = await validate_solution_ebbo(
        solution, auction, multicall=None, intermediates=[], tolerance_bps=50
    )
    assert r.passes


@pytest.mark.asyncio
async def test_buy_orders_are_skipped_not_failed(quoter) -> None:
    """Buy-kind orders are not yet covered by EBBO — they must be skipped
    rather than counted as a violation."""
    o = _order(kind="buy")
    auction = _auction([o])
    trade = Trade(kind="fulfillment", orderUid=UID, executedAmount=1000)
    solution = _solution([trade], prices={"0xa": 1, "0xb": 2})
    # No quote stubbed — wouldn't matter; buy should skip before quoter call.

    r = await validate_solution_ebbo(solution, auction, multicall=None, intermediates=[])
    assert r.passes
    assert r.n_checked == 0
    assert r.n_skipped == 1


@pytest.mark.asyncio
async def test_missing_clearing_prices_skipped(quoter) -> None:
    """Trade whose tokens are absent from solution.prices is skipped rather
    than failing — protects against composer bugs without false-rejecting."""
    o = _order()
    auction = _auction([o])
    trade = Trade(kind="fulfillment", orderUid=UID, executedAmount=1000)
    solution = _solution([trade], prices={})  # NO prices

    r = await validate_solution_ebbo(solution, auction, multicall=None, intermediates=[])
    assert r.passes  # nothing checked, nothing failed
    assert r.n_checked == 0
    assert r.n_skipped == 1


@pytest.mark.asyncio
async def test_unknown_order_uid_skipped(quoter) -> None:
    """Trade referencing a UID not in the auction (composer-bug guard) is
    skipped, not failed."""
    o = _order(uid=UID)
    auction = _auction([o])
    # Trade points to a different UID — not in auction
    trade = Trade(kind="fulfillment", orderUid=UID2, executedAmount=1000)
    solution = _solution([trade], prices={"0xa": 1, "0xb": 1})

    r = await validate_solution_ebbo(solution, auction, multicall=None, intermediates=[])
    assert r.passes
    assert r.n_skipped == 1


@pytest.mark.asyncio
async def test_no_external_route_treated_as_pass(quoter) -> None:
    """If V3 has no route for the swap (long-tail token), there's nothing to
    beat — pass."""
    o = _order(sell_token="0xa", buy_token="0xexotic")
    auction = _auction([o])
    trade = Trade(kind="fulfillment", orderUid=UID, executedAmount=1000)
    solution = _solution([trade], prices={"0xa": 1, "0xexotic": 1})
    # Quoter returns None (no table entry → mock default).

    r = await validate_solution_ebbo(solution, auction, multicall=None, intermediates=[])
    assert r.passes
    assert r.n_skipped == 1


@pytest.mark.asyncio
async def test_quoter_exception_does_not_fail(monkeypatch) -> None:
    """An exception from the quoter (RPC blip) must not reject the solution —
    EBBO is a safety net, not a single-point-of-failure."""
    o = _order()
    auction = _auction([o])
    trade = Trade(kind="fulfillment", orderUid=UID, executedAmount=1000)
    solution = _solution([trade], prices={"0xa": 1, "0xb": 1})

    async def _boom(*a, **k):
        raise RuntimeError("rpc timeout")

    monkeypatch.setattr("src.solver.ebbo.quote_best_path", _boom)

    r = await validate_solution_ebbo(solution, auction, multicall=None, intermediates=[])
    assert r.passes
    assert r.n_skipped == 1


@pytest.mark.asyncio
async def test_mixed_trades_one_violator_fails_whole_solution(quoter) -> None:
    """One bad trade is enough — composed solutions are atomic at submission
    so we can't partially settle just the good ones."""
    o1 = _order(uid=UID, sell_token="0xa", buy_token="0xb")
    o2 = _order(uid=UID2, sell_token="0xa", buy_token="0xb")
    auction = _auction([o1, o2])
    trades = [
        Trade(kind="fulfillment", orderUid=UID, executedAmount=1000),
        Trade(kind="fulfillment", orderUid=UID2, executedAmount=500),
    ]
    # Both trades use price 1:1 → our_buy = executed.
    solution = _solution(trades, prices={"0xa": 1, "0xb": 1})
    # First trade is fine (1000 == ext 1000); second is way short.
    quoter[("0xa", "0xb", 1000)] = 1000
    quoter[("0xa", "0xb", 500)] = 1000  # external returns 1000 for 500 in → impossible high

    r = await validate_solution_ebbo(solution, auction, multicall=None, intermediates=[])
    assert not r.passes
    assert len(r.violations) == 1


def test_default_tolerance_is_documented() -> None:
    """Tripwire: tolerance constant is 50 bps.  Adjust this test if you
    deliberately change the production default."""
    assert DEFAULT_TOLERANCE_BPS == 50


@pytest.mark.asyncio
async def test_truncates_at_max_trades(quoter, monkeypatch) -> None:
    """Pathological composer output > _MAX_TRADES_PER_CHECK is truncated and
    counted in n_truncated, not silently dropped."""
    from src.solver import ebbo as _ebbo
    monkeypatch.setattr(_ebbo, "_MAX_TRADES_PER_CHECK", 3)
    orders = [_order(uid=f"0x{i:0112x}") for i in range(5)]
    auction = _auction(orders)
    trades = [
        Trade(kind="fulfillment", orderUid=f"0x{i:0112x}", executedAmount=1000)
        for i in range(5)
    ]
    solution = _solution(trades, prices={"0xa": 1, "0xb": 1})
    quoter[("0xa", "0xb", 1000)] = 1000

    r = await validate_solution_ebbo(solution, auction, multicall=None, intermediates=[])
    assert r.passes
    assert r.n_checked == 3      # _MAX_TRADES_PER_CHECK
    assert r.n_truncated == 2    # 5 trades − 3 checked


@pytest.mark.asyncio
async def test_uses_ceil_div_to_mirror_scoring(quoter) -> None:
    """`our_buy_amount` rounding must match `_score_sell_trade`'s _ceil_div,
    otherwise EBBO could reject a solution by 1 wei that scoring would accept.

    With executed=7, cp_sell=10, cp_buy=3: floor((7*10)/3)=23, ceil=24.
    External returns 24 → with tolerance=0 we should PASS at 24, not fail."""
    o = _order(sell_amount=7, buy_amount=20)
    auction = _auction([o])
    trade = Trade(kind="fulfillment", orderUid=UID, executedAmount=7)
    solution = _solution([trade], prices={"0xa": 10, "0xb": 3})
    quoter[("0xa", "0xb", 7)] = 24

    r = await validate_solution_ebbo(
        solution, auction, multicall=None, intermediates=[], tolerance_bps=0
    )
    assert r.passes, "ceil_div must round up so our_buy=24 matches ext=24 at tolerance=0"
