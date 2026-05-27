"""Tests for the EBBO (External Best Bid/Offer) pre-submission validator.

Behaviour the tests lock in:
  - Solution passes when every sell trade's user output meets/exceeds the
    external V3 quote within tolerance.
  - Solution fails when ANY trade falls short by more than tolerance.
  - Buy-kind trades are now validated: user must not pay more sell-token
    than external V3 quoteExactOutput would charge (plus tolerance).
  - Orders missing clearing prices are skipped (not failed).
  - RPC errors in the quoter do NOT fail the solution; they're logged
    and treated as "no external comparison available, pass through".
  - Solutions with more than _MAX_TRADES_PER_CHECK trades are rejected
    outright rather than silently truncated.
"""

from __future__ import annotations

import pytest

from src.models.auction import Auction
from src.models.order import Order
from src.models.solution import Solution, Trade
from src.solver.ebbo import (
    DEFAULT_TOLERANCE_BPS,
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


@pytest.fixture
def quoter(monkeypatch):
    """Replace _quote_best_exact_input with a tunable mock keyed by
    (sell, buy, amount_in). Returns the buy-token output for the given
    sell-token input, or None when no route exists.

    Mirrors the V3-only EBBO sell-branch helper introduced when EBBO's
    scope was tightened to match Router's actual venue set.
    """
    table: dict[tuple[str, str, int], int | None] = {}

    async def _stub(_mc, sell_token, buy_token, amount_in, _intermediates):
        key = (sell_token.lower(), buy_token.lower(), amount_in)
        return table.get(key)

    monkeypatch.setattr("src.solver.ebbo._quote_best_exact_input", _stub)
    return table


@pytest.fixture
def quoter_buy(monkeypatch):
    """Replace _quote_best_exact_output with a mock keyed by (sell, buy, amount_out).

    Returns the sell-token cost (amount_in) for an exact-output quote, or None
    when no route exists (key absent from table).
    """
    table: dict[tuple[str, str, int], int | None] = {}

    async def _stub(_mc, sell_token, buy_token, amount_out, _intermediates):
        key = (sell_token.lower(), buy_token.lower(), amount_out)
        return table.get(key)

    monkeypatch.setattr("src.solver.ebbo._quote_best_exact_output", _stub)
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
async def test_buy_orders_are_now_validated(quoter, quoter_buy) -> None:
    """Buy-kind orders are now validated against quoteExactOutput.

    Setup:
      - order: buy, sellAmount=1000, buyAmount=500
        (user wants 500 buy-token, willing to pay up to 1000 sell-token)
      - executed_amount = 500 (exact buy side)
      - clearing prices: cp_sell=2, cp_buy=1
        => our_sell_amount = floor(500 * 1 / 2) = 250
        Wait — let's make it clearly violating.
      - clearing prices: cp_buy=10, cp_sell=1
        => our_sell_amount = floor(500 * 10 / 1) = 5000
      - External quote: 800 sell-token for 500 buy-token (much cheaper)
      - our_sell=5000 >> threshold=800*(10050/10000)=804 → FAIL
    """
    o = _order(kind="buy", sell_token="0xa", buy_token="0xb",
               sell_amount=1000, buy_amount=500)
    auction = _auction([o])
    trade = Trade(kind="fulfillment", orderUid=UID, executedAmount=500)
    # cp_buy=10, cp_sell=1 → our_sell = 500*10//1 = 5000, ext=800
    solution = _solution([trade], prices={"0xa": 1, "0xb": 10})
    quoter_buy[("0xa", "0xb", 500)] = 800

    r = await validate_solution_ebbo(solution, auction, multicall=None, intermediates=[])
    assert not r.passes
    assert r.n_checked == 1
    assert len(r.violations) == 1
    assert UID[:18] in r.violations[0]


@pytest.mark.asyncio
async def test_buy_order_within_tolerance_passes(quoter, quoter_buy) -> None:
    """Buy trade that pays only slightly more than external (within 50 bps) passes.

    our_sell = floor(500 * 1 / 1) = 500.
    ext_sell = 499.  overpay = 1/499 ≈ 20 bps < 50 bps tolerance → PASS.
    threshold = 499 * 10050 // 10000 = 501
    our_sell=500 <= 501 → passes.
    """
    o = _order(kind="buy", sell_token="0xa", buy_token="0xb",
               sell_amount=1000, buy_amount=500)
    auction = _auction([o])
    trade = Trade(kind="fulfillment", orderUid=UID, executedAmount=500)
    # cp_buy=1, cp_sell=1 → our_sell = 500*1//1 = 500
    solution = _solution([trade], prices={"0xa": 1, "0xb": 1})
    quoter_buy[("0xa", "0xb", 500)] = 499  # ext cheaper by ~20 bps

    r = await validate_solution_ebbo(solution, auction, multicall=None, intermediates=[])
    assert r.passes
    assert r.n_checked == 1


@pytest.mark.asyncio
async def test_buy_order_no_route_skipped_not_failed(quoter, quoter_buy) -> None:
    """Buy trade with no V3 route (exotic token) is skipped, not failed."""
    o = _order(kind="buy", sell_token="0xa", buy_token="0xexotic",
               sell_amount=1000, buy_amount=500)
    auction = _auction([o])
    trade = Trade(kind="fulfillment", orderUid=UID, executedAmount=500)
    solution = _solution([trade], prices={"0xa": 1, "0xexotic": 1})
    # No entry in quoter_buy → mock returns None

    r = await validate_solution_ebbo(solution, auction, multicall=None, intermediates=[])
    assert r.passes
    assert r.n_checked == 0
    assert r.n_skipped == 1


@pytest.mark.asyncio
async def test_buy_order_quote_exception_skipped(quoter, monkeypatch) -> None:
    """RPC error during buy-side quoting skips the trade, not fails the solution."""
    o = _order(kind="buy", sell_token="0xa", buy_token="0xb",
               sell_amount=1000, buy_amount=500)
    auction = _auction([o])
    trade = Trade(kind="fulfillment", orderUid=UID, executedAmount=500)
    solution = _solution([trade], prices={"0xa": 1, "0xb": 1})

    async def _boom(*a, **k):
        raise RuntimeError("rpc timeout")

    monkeypatch.setattr("src.solver.ebbo._quote_best_exact_output", _boom)

    r = await validate_solution_ebbo(solution, auction, multicall=None, intermediates=[])
    assert r.passes
    assert r.n_skipped == 1


@pytest.mark.asyncio
async def test_mixed_sell_and_buy_trades(quoter, quoter_buy) -> None:
    """One passing sell + one failing buy in the same solution → passes=False,
    only the failing buy appears in violations."""
    o_sell = _order(uid=UID, sell_token="0xa", buy_token="0xb",
                    sell_amount=1000, buy_amount=900, kind="sell")
    o_buy = _order(uid=UID2, sell_token="0xa", buy_token="0xb",
                   sell_amount=1000, buy_amount=500, kind="buy")
    auction = _auction([o_sell, o_buy])

    trades = [
        Trade(kind="fulfillment", orderUid=UID, executedAmount=1000),   # sell
        Trade(kind="fulfillment", orderUid=UID2, executedAmount=500),   # buy
    ]
    # Prices 1:1 → sell: our_buy = ceil(1000*1/1)=1000; buy: our_sell=500*1//1=500
    solution = _solution(trades, prices={"0xa": 1, "0xb": 1})

    # Sell trade: external 1000 == our 1000 → passes
    quoter[("0xa", "0xb", 1000)] = 1000
    # Buy trade: external ext_sell=400, our_sell=500, threshold=400*10050//10000=402
    # 500 > 402 → fails
    quoter_buy[("0xa", "0xb", 500)] = 400

    r = await validate_solution_ebbo(solution, auction, multicall=None, intermediates=[])
    assert not r.passes
    assert len(r.violations) == 1
    assert UID2[:18] in r.violations[0]
    assert r.n_checked == 2


@pytest.mark.asyncio
async def test_truncation_rejects_solution(monkeypatch) -> None:
    """Solution with more trades than _MAX_TRADES_PER_CHECK is rejected outright."""
    from src.solver import ebbo as _ebbo
    monkeypatch.setattr(_ebbo, "_MAX_TRADES_PER_CHECK", 20)

    orders = [_order(uid=f"0x{i:0112x}") for i in range(25)]
    auction = _auction(orders)
    trades = [
        Trade(kind="fulfillment", orderUid=f"0x{i:0112x}", executedAmount=1000)
        for i in range(25)
    ]
    solution = _solution(trades, prices={"0xa": 1, "0xb": 1})

    r = await validate_solution_ebbo(solution, auction, multicall=None, intermediates=[])
    assert not r.passes
    assert r.n_truncated == 5
    assert r.n_checked == 0
    assert len(r.violations) == 1
    assert "truncation" in r.violations[0]


@pytest.mark.asyncio
async def test_truncation_boundary_exactly_max(quoter) -> None:
    """Solution with exactly _MAX_TRADES_PER_CHECK (20) trades is NOT truncated."""
    orders = [_order(uid=f"0x{i:0112x}") for i in range(20)]
    auction = _auction(orders)
    trades = [
        Trade(kind="fulfillment", orderUid=f"0x{i:0112x}", executedAmount=1000)
        for i in range(20)
    ]
    solution = _solution(trades, prices={"0xa": 1, "0xb": 1})
    quoter[("0xa", "0xb", 1000)] = 1000  # all pass

    r = await validate_solution_ebbo(solution, auction, multicall=None, intermediates=[])
    assert r.passes
    assert r.n_truncated == 0
    assert r.n_checked == 20


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

    monkeypatch.setattr("src.solver.ebbo._quote_best_exact_input", _boom)

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
    """Pathological composer output > _MAX_TRADES_PER_CHECK is rejected outright.

    Previous behaviour was: truncate and pass the first N trades.  New
    behaviour: return passes=False immediately — unvalidated trades must
    never reach submission.  n_truncated is still populated for telemetry.
    """
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
    assert not r.passes                   # NEW: rejects rather than passing
    assert r.n_checked == 0               # nothing checked — rejected early
    assert r.n_truncated == 2             # 5 trades − 3 limit
    assert len(r.violations) == 1
    assert "truncation" in r.violations[0]


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
