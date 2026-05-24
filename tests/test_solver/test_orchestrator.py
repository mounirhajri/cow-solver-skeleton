import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.models.auction import Auction
from src.models.solution import Solution, Trade
from src.solver.base import NoSolution
from src.solver.orchestrator import SolverOrchestrator

UID = "0x" + "a" * 112


@pytest.fixture
def auction() -> Auction:
    path = Path(__file__).parent.parent / "fixtures" / "sample_auction.json"
    return Auction.model_validate(json.loads(path.read_text()))


def _solution() -> Solution:
    return Solution(
        id=1,
        prices={},
        trades=[Trade(kind="fulfillment", order_uid=UID, executed_amount=10**18)],
        interactions=[],
    )


async def test_orchestrator_returns_first_solution(auction: Auction) -> None:
    sol = _solution()
    s1 = AsyncMock(name="s1")
    s1.name = "s1"
    s1.solve.return_value = sol
    s2 = AsyncMock(name="s2")
    s2.name = "s2"
    s2.solve.return_value = NoSolution()

    # run_all_strategies=False: stops at first winner
    orch = SolverOrchestrator(
        strategies=[s1, s2], per_strategy_timeout=1.0, run_all_strategies=False
    )
    result, attempts = await orch.solve(auction)

    assert result is sol
    s2.solve.assert_not_called()  # s1 already returned solution, stopped early
    assert len(attempts) == 1
    assert attempts[0].status == "solved"


async def test_orchestrator_falls_through_on_nosolution(auction: Auction) -> None:
    s1 = AsyncMock(name="s1")
    s1.name = "s1"
    s1.solve.return_value = NoSolution()
    sol = _solution()
    s2 = AsyncMock(name="s2")
    s2.name = "s2"
    s2.solve.return_value = sol

    orch = SolverOrchestrator(strategies=[s1, s2], per_strategy_timeout=1.0)
    result, attempts = await orch.solve(auction)
    assert result is sol
    assert len(attempts) == 2
    assert attempts[0].status == "no_solution"
    assert attempts[1].status == "solved"


async def test_orchestrator_times_out_slow_strategy(auction: Auction) -> None:
    async def slow_solve(a: Auction) -> Solution | NoSolution:
        await asyncio.sleep(10)
        raise AssertionError("should have been cancelled")

    s1 = AsyncMock(name="s1")
    s1.name = "slow"
    s1.solve.side_effect = slow_solve

    sol = _solution()
    s2 = AsyncMock(name="s2")
    s2.name = "fast"
    s2.solve.return_value = sol

    orch = SolverOrchestrator(strategies=[s1, s2], per_strategy_timeout=0.1)
    result, attempts = await orch.solve(auction)
    assert result is sol
    assert attempts[0].status == "timeout"
    assert attempts[1].status == "solved"


async def test_orchestrator_returns_nosolution_if_all_fail(auction: Auction) -> None:
    s1 = AsyncMock(name="s1")
    s1.name = "s1"
    s1.solve.return_value = NoSolution()
    orch = SolverOrchestrator(strategies=[s1], per_strategy_timeout=1.0)
    result, attempts = await orch.solve(auction)
    assert isinstance(result, NoSolution)
    assert len(attempts) == 1
    assert attempts[0].status == "no_solution"


async def test_orchestrator_uses_per_strategy_timeout_attribute(auction: Auction) -> None:
    """A strategy with a .timeout attribute uses it instead of the orchestrator default."""
    # s1 sleeps for 0.3 s — longer than the orchestrator default (0.1 s) but within
    # its own declared timeout (0.5 s).  It should NOT be cancelled.
    async def medium_solve(a: Auction) -> Solution | NoSolution:
        await asyncio.sleep(0.3)
        return _solution()

    s1 = AsyncMock(name="s1")
    s1.name = "medium"
    s1.solve.side_effect = medium_solve
    s1.timeout = 0.5  # strategy declares its own, longer timeout

    orch = SolverOrchestrator(strategies=[s1], per_strategy_timeout=0.1)
    result, attempts = await orch.solve(auction)

    # Should have succeeded, not timed out
    assert isinstance(result, Solution)
    assert attempts[0].status == "solved"


async def test_orchestrator_run_all_strategies_collects_all_attempts(auction: Auction) -> None:
    """With run_all_strategies=True, all strategies run even after a winner is found."""
    sol = _solution()
    s1 = AsyncMock(name="s1")
    s1.name = "s1"
    s1.solve.return_value = sol
    s2 = AsyncMock(name="s2")
    s2.name = "s2"
    s2.solve.return_value = NoSolution()

    orch = SolverOrchestrator(
        strategies=[s1, s2], per_strategy_timeout=1.0, run_all_strategies=True
    )
    result, attempts = await orch.solve(auction)

    assert result is sol
    s2.solve.assert_called_once()  # s2 was still called despite s1 winning
    assert len(attempts) == 2
    assert attempts[0].status == "solved"
    assert attempts[1].status == "no_solution"


async def test_naive_solution_is_never_submitted(auction: Auction) -> None:
    """The orchestrator must not return a naive solution — naive's clearingPrices
    come from oracle data, not realised market prices.  If naive is the only
    strategy that solved, fall through to NoSolution rather than ship a fantasy
    trade.  Verified live 2026-05-24: composer was emitting naive at ~481 ETH
    per win because its executed_amounts dwarfed every real solver."""
    sol = _solution()
    naive = AsyncMock(name="naive")
    naive.name = "naive"
    naive.solve.return_value = sol

    orch = SolverOrchestrator(
        strategies=[naive], per_strategy_timeout=1.0, run_all_strategies=False,
    )
    result, attempts = await orch.solve(auction)

    # The naive ATTEMPT is recorded — we still want shadow data — but the
    # final return must be NoSolution, not the naive solution itself.
    assert isinstance(result, NoSolution)
    assert any(a.strategy == "naive" and a.status == "solved" for a in attempts)


async def test_composer_excludes_naive_when_other_strategy_solves(auction: Auction) -> None:
    """When naive and a real strategy both solve, composer must merge only the
    real strategy (naive's oracle prices would dwarf the real surplus)."""
    sol_real = _solution()
    sol_naive = _solution()

    naive = AsyncMock(name="naive")
    naive.name = "naive"
    naive.solve.return_value = sol_naive
    real = AsyncMock(name="router-v2")
    real.name = "router-v2"
    real.solve.return_value = sol_real

    orch = SolverOrchestrator(
        strategies=[naive, real],
        per_strategy_timeout=1.0,
        run_all_strategies=True,
        compose=True,
    )
    result, attempts = await orch.solve(auction)

    # We get *some* Solution back — either the real solver's or a composed
    # one — but never the naive Solution object.
    assert isinstance(result, Solution)
    assert result is not sol_naive
    # If composer ran, it appears in attempts.
    composer_attempts = [a for a in attempts if a.strategy == "composer"]
    if composer_attempts:
        # n_candidates in the composer log will reflect the post-filter count.
        # Here both strategies solved, but naive should be excluded → composer
        # falls back (only 1 composable candidate left, threshold is "> 1").
        # So we expect NO composer attempt at all in this 2-strategy case.
        pytest.fail("Composer should not run with a single non-naive solver")
