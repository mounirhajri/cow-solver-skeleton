import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.models.auction import Auction
from src.models.solution import Solution, Trade
from src.solver.base import NoSolution
from src.solver.orchestrator import SolverOrchestrator


@pytest.fixture
def auction() -> Auction:
    path = Path(__file__).parent.parent / "fixtures" / "sample_auction.json"
    return Auction.model_validate(json.loads(path.read_text()))


async def test_orchestrator_returns_first_solution(auction: Auction) -> None:
    sol = Solution(id=1, prices={}, trades=[Trade(kind="fulfillment", order_uid="0x" + "a"*112, executed_amount=10**18)], interactions=[])
    s1 = AsyncMock(name="s1")
    s1.name = "s1"
    s1.solve.return_value = sol
    s2 = AsyncMock(name="s2")
    s2.name = "s2"

    orch = SolverOrchestrator(strategies=[s1, s2], per_strategy_timeout=1.0)
    result = await orch.solve(auction)

    assert result is sol
    s2.solve.assert_not_called()  # s1 already returned solution


async def test_orchestrator_falls_through_on_nosolution(auction: Auction) -> None:
    s1 = AsyncMock(name="s1")
    s1.name = "s1"
    s1.solve.return_value = NoSolution()
    sol = Solution(id=1, prices={}, trades=[Trade(kind="fulfillment", order_uid="0x" + "a"*112, executed_amount=10**18)], interactions=[])
    s2 = AsyncMock(name="s2")
    s2.name = "s2"
    s2.solve.return_value = sol

    orch = SolverOrchestrator(strategies=[s1, s2], per_strategy_timeout=1.0)
    result = await orch.solve(auction)
    assert result is sol


async def test_orchestrator_times_out_slow_strategy(auction: Auction) -> None:
    async def slow_solve(a: Auction) -> Solution | NoSolution:
        await asyncio.sleep(10)
        raise AssertionError("should have been cancelled")

    s1 = AsyncMock(name="s1")
    s1.name = "slow"
    s1.solve.side_effect = slow_solve

    sol = Solution(id=1, prices={}, trades=[Trade(kind="fulfillment", order_uid="0x" + "a"*112, executed_amount=10**18)], interactions=[])
    s2 = AsyncMock(name="s2")
    s2.name = "fast"
    s2.solve.return_value = sol

    orch = SolverOrchestrator(strategies=[s1, s2], per_strategy_timeout=0.1)
    result = await orch.solve(auction)
    assert result is sol


async def test_orchestrator_returns_nosolution_if_all_fail(auction: Auction) -> None:
    s1 = AsyncMock(name="s1")
    s1.name = "s1"
    s1.solve.return_value = NoSolution()
    orch = SolverOrchestrator(strategies=[s1], per_strategy_timeout=1.0)
    result = await orch.solve(auction)
    assert isinstance(result, NoSolution)
