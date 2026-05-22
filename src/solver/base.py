from typing import Protocol, runtime_checkable

from src.models.auction import Auction
from src.models.solution import Solution


class NoSolution:
    """Sentinel returned when a strategy has no solution for this auction."""

    def __bool__(self) -> bool:
        return False


@runtime_checkable
class SolverStrategy(Protocol):
    """Interface every solver strategy must implement."""

    name: str

    async def solve(self, auction: Auction) -> Solution | NoSolution:
        ...
