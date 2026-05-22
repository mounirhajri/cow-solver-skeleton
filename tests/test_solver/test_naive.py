import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.models.auction import Auction
from src.models.solution import Solution
from src.routing.oneinch import OneInchQuote
from src.solver.base import NoSolution
from src.solver.naive import NaiveSolver


@pytest.fixture
def sample_auction() -> Auction:
    path = Path(__file__).parent.parent / "fixtures" / "sample_auction.json"
    return Auction.model_validate(json.loads(path.read_text()))


async def test_naive_solver_returns_solution(sample_auction: Auction) -> None:
    oneinch = AsyncMock()
    oneinch.quote.return_value = OneInchQuote(dst_amount=3_460_000_000, gas=200_000)
    solver = NaiveSolver(oneinch=oneinch)

    result = await solver.solve(sample_auction)

    assert isinstance(result, Solution)
    assert len(result.trades) == 1
    assert result.trades[0].executed_amount == 10**18
    # prices include both tokens
    assert len(result.prices) == 2


async def test_naive_solver_returns_nosolution_when_below_limit(
    sample_auction: Auction,
) -> None:
    # User wants min 3.45e9 USDC, quote returns 3.4e9 → cannot satisfy
    oneinch = AsyncMock()
    oneinch.quote.return_value = OneInchQuote(dst_amount=3_400_000_000, gas=200_000)
    solver = NaiveSolver(oneinch=oneinch)

    result = await solver.solve(sample_auction)
    assert isinstance(result, NoSolution)


async def test_naive_solver_skips_buy_orders(sample_auction: Auction) -> None:
    sample_auction.orders[0].kind = "buy"
    oneinch = AsyncMock()
    solver = NaiveSolver(oneinch=oneinch)

    result = await solver.solve(sample_auction)
    # naive v1 only handles sell orders
    assert isinstance(result, NoSolution)
    oneinch.quote.assert_not_called()
