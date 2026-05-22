import json
from pathlib import Path

import pytest

from src.models.auction import Auction
from src.models.solution import Solution
from src.solver.base import NoSolution
from src.solver.naive import NaiveSolver


@pytest.fixture
def sample_auction() -> Auction:
    path = Path(__file__).parent.parent / "fixtures" / "sample_auction.json"
    return Auction.model_validate(json.loads(path.read_text()))


async def test_naive_solver_returns_solution(sample_auction: Auction) -> None:
    # sample_auction: sell 1e18 WETH (ref=3500e18), want 3.45e9 units (ref=1e18)
    # buy_at_ref = 1e18 * 3500e18 // 1e18 = 3500e18 >= 3.45e9 → fillable
    solver = NaiveSolver()
    result = await solver.solve(sample_auction)

    assert isinstance(result, Solution)
    assert len(result.trades) == 1
    assert result.trades[0].executed_amount == 10**18
    assert len(result.prices) == 2


async def test_naive_solver_returns_nosolution_when_below_limit(
    sample_auction: Auction,
) -> None:
    # buy_at_ref = 1e18 * 3500e18 // 1e18 = 3500e18
    # Set buy_amount above that so the order is unfillable
    sample_auction.orders[0].buy_amount = 4_000 * 10**18  # want 4000e18 units, ref gives 3500e18
    solver = NaiveSolver()
    result = await solver.solve(sample_auction)
    assert isinstance(result, NoSolution)


async def test_naive_solver_skips_buy_orders(sample_auction: Auction) -> None:
    sample_auction.orders[0].kind = "buy"
    solver = NaiveSolver()
    result = await solver.solve(sample_auction)
    assert isinstance(result, NoSolution)


async def test_naive_solver_skips_missing_reference_price(sample_auction: Auction) -> None:
    # Remove reference price from sell token
    sample_auction.tokens[sample_auction.orders[0].sell_token].reference_price = None
    solver = NaiveSolver()
    result = await solver.solve(sample_auction)
    assert isinstance(result, NoSolution)
