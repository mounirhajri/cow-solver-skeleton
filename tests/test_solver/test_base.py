from typing import get_type_hints

from src.models.auction import Auction
from src.models.solution import Solution
from src.solver.base import NoSolution, SolverStrategy


def test_solver_strategy_is_protocol() -> None:
    # Protocols are runtime_checkable when decorated; we just assert callable
    assert hasattr(SolverStrategy, "solve")


def test_no_solution_is_falsy() -> None:
    assert not NoSolution()


def test_solver_strategy_signature() -> None:
    hints = get_type_hints(SolverStrategy.solve)
    assert hints["auction"] is Auction
    assert hints["return"] == Solution | NoSolution
