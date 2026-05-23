"""Tests for bipartite CoW matcher."""
import pytest

from edge.matching.bipartite import BipartiteMatcher
from src.models.auction import Auction
from src.models.order import Order
from src.models.solution import Solution
from src.solver.base import NoSolution


def _mk_order(
    uid: str, sell_token: str, buy_token: str, sell_amount: int, buy_amount: int
) -> Order:
    return Order(
        uid=uid,
        sellToken=sell_token,
        buyToken=buy_token,
        sellAmount=sell_amount,
        buyAmount=buy_amount,
        feePolicies=[],
        validTo=999999,
        kind="sell",
        owner="0x" + "a" * 40,
        partiallyFillable=False,
        **{"class": "limit"},
    )


def _mk_auction(orders: list[Order]) -> Auction:
    return Auction(
        id="1",
        tokens={},
        orders=orders,
        liquidity=[],
        effectiveGasPrice=0,
        deadline=None,
    )


@pytest.mark.asyncio
async def test_empty_auction_returns_no_solution():
    m = BipartiteMatcher()
    result = await m.solve(_mk_auction([]))
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_single_order_returns_no_solution():
    m = BipartiteMatcher()
    auction = _mk_auction([_mk_order("o1", "0xa", "0xb", 1000, 800)])
    result = await m.solve(auction)
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_no_counterpart_returns_no_solution():
    """Two orders A→B but no B→A."""
    m = BipartiteMatcher()
    auction = _mk_auction([
        _mk_order("o1", "0xa", "0xb", 1000, 800),
        _mk_order("o2", "0xa", "0xb", 500, 400),
    ])
    result = await m.solve(auction)
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_simple_match_emits_two_trades():
    """A: 1000 X for >=800 Y. B: 1000 Y for >=800 X. Feasible (1e6 >= 640e3)."""
    m = BipartiteMatcher()
    auction = _mk_auction([
        _mk_order("oA", "0xa", "0xb", 1000, 800),
        _mk_order("oB", "0xb", "0xa", 1000, 800),
    ])
    result = await m.solve(auction)
    assert isinstance(result, Solution)
    assert len(result.trades) == 2
    uids = {t.order_uid for t in result.trades}
    assert uids == {"oA", "oB"}


@pytest.mark.asyncio
async def test_infeasible_limits_returns_no_solution():
    """A wants too much: A sells 100 for >=200. B sells 100 for >=200. 1e4 < 4e4."""
    m = BipartiteMatcher()
    auction = _mk_auction([
        _mk_order("oA", "0xa", "0xb", 100, 200),
        _mk_order("oB", "0xb", "0xa", 100, 200),
    ])
    result = await m.solve(auction)
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_greedy_matches_largest_first():
    """Two B→A orders, both should match their respective A→B counterpart."""
    m = BipartiteMatcher()
    auction = _mk_auction([
        _mk_order("oA1", "0xa", "0xb", 1000, 800),
        _mk_order("oA2", "0xa", "0xb", 100, 80),
        _mk_order("oB1", "0xb", "0xa", 1000, 800),
        _mk_order("oB2", "0xb", "0xa", 100, 80),
    ])
    result = await m.solve(auction)
    assert isinstance(result, Solution)
    # Should match 4 orders (greedy pairs largest-first then next)
    assert len(result.trades) == 4


@pytest.mark.asyncio
async def test_non_sell_orders_ignored():
    m = BipartiteMatcher()
    buy_order = Order(
        uid="o_buy", sellToken="0xa", buyToken="0xb",
        sellAmount=1000, buyAmount=800, feePolicies=[],
        validTo=999, kind="buy",
        owner="0x" + "a" * 40, partiallyFillable=False,
        **{"class": "limit"},
    )
    sell_order = _mk_order("o_sell", "0xb", "0xa", 1000, 800)
    auction = _mk_auction([buy_order, sell_order])
    result = await m.solve(auction)
    # Only sell orders are considered → no bipartite match
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_executed_amounts_within_limits():
    """Verify the executed amounts satisfy order limits."""
    m = BipartiteMatcher()
    # A: sells 1000 X, wants >= 800 Y
    # B: sells 900 Y, wants >= 700 X
    # Feasibility: 1000 * 900 = 900000 >= 800 * 700 = 560000 ✓
    auction = _mk_auction([
        _mk_order("oA", "0xa", "0xb", 1000, 800),
        _mk_order("oB", "0xb", "0xa", 900, 700),
    ])
    result = await m.solve(auction)
    assert isinstance(result, Solution)
    assert len(result.trades) == 2
    trade_a = next(t for t in result.trades if t.order_uid == "oA")
    trade_b = next(t for t in result.trades if t.order_uid == "oB")
    # A's executed sell must not exceed sell_amount
    assert trade_a.executed_amount <= 1000
    assert trade_a.executed_amount > 0
    # B's executed sell must not exceed sell_amount
    assert trade_b.executed_amount <= 900
    assert trade_b.executed_amount > 0


@pytest.mark.asyncio
async def test_prices_in_solution():
    """Clearing prices for both tokens should be in the solution."""
    m = BipartiteMatcher()
    auction = _mk_auction([
        _mk_order("oA", "0xa", "0xb", 1000, 800),
        _mk_order("oB", "0xb", "0xa", 1000, 800),
    ])
    result = await m.solve(auction)
    assert isinstance(result, Solution)
    assert "0xa" in result.prices
    assert "0xb" in result.prices
    assert result.prices["0xa"] > 0
    assert result.prices["0xb"] > 0


@pytest.mark.asyncio
async def test_rf_filter_invoked_when_classifier_passed(monkeypatch):
    """When classifier+model wired, the filter is called on sell_orders."""

    class _DummyClassifier:
        model = "loaded"

        def score(self, features: dict) -> float:  # noqa: ARG002
            return 1.0

    captured: dict[str, object] = {}

    async def fake_filter(orders, session_factory, classifier, threshold=0.4):
        captured["called"] = True
        captured["n_in"] = len(orders)
        captured["threshold"] = threshold
        return orders  # pass through unchanged

    monkeypatch.setattr(
        "edge.matching.rf_filter.filter_orders_by_token_quality", fake_filter
    )

    m = BipartiteMatcher(
        classifier=_DummyClassifier(),
        session_factory=lambda: None,
        rf_threshold=0.4,
    )
    auction = _mk_auction([
        _mk_order("oA", "0xa", "0xb", 1000, 800),
        _mk_order("oB", "0xb", "0xa", 1000, 800),
    ])
    result = await m.solve(auction)
    assert captured.get("called") is True
    assert captured.get("n_in") == 2
    assert isinstance(result, Solution)


@pytest.mark.asyncio
async def test_exact_match_symmetric():
    """Exact match: A sells 1000 for 1000, B sells 1000 for 1000."""
    m = BipartiteMatcher()
    auction = _mk_auction([
        _mk_order("oA", "0xa", "0xb", 1000, 1000),
        _mk_order("oB", "0xb", "0xa", 1000, 1000),
    ])
    result = await m.solve(auction)
    assert isinstance(result, Solution)
    assert len(result.trades) == 2
