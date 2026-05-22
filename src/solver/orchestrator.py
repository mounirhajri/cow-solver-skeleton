import asyncio
from collections.abc import Sequence

from src.log import get_logger
from src.models.auction import Auction
from src.models.solution import Solution
from src.solver.base import NoSolution, SolverStrategy

log = get_logger(__name__)


class SolverOrchestrator:
    """Tries strategies in order; returns first non-empty solution.

    Each strategy is bounded by per_strategy_timeout. Strategies that exceed
    the timeout are cancelled and the orchestrator falls through to the next.
    """

    def __init__(
        self,
        strategies: Sequence[SolverStrategy],
        per_strategy_timeout: float = 5.0,
    ) -> None:
        if not strategies:
            raise ValueError("at least one strategy required")
        self._strategies = list(strategies)
        self._timeout = per_strategy_timeout

    async def solve(self, auction: Auction) -> Solution | NoSolution:
        for strat in self._strategies:
            try:
                result = await asyncio.wait_for(strat.solve(auction), timeout=self._timeout)
            except asyncio.TimeoutError:
                log.warning("strategy_timeout", strategy=strat.name, auction_id=auction.id)
                continue
            except Exception as e:  # noqa: BLE001
                log.error("strategy_error", strategy=strat.name, error=str(e), auction_id=auction.id)
                continue

            if isinstance(result, Solution):
                log.info("strategy_won", strategy=strat.name, auction_id=auction.id)
                return result

        return NoSolution()


def load_default_strategies(oneinch_api_key: str) -> list[SolverStrategy]:
    """Build the strategy chain. Loads edge strategies if private submodule present.

    Order: edge strategies first (more specialized), naive last (fallback).
    """
    from src.routing.oneinch import OneInchClient
    from src.solver.naive import NaiveSolver

    strategies: list[SolverStrategy] = []

    # Try to load edge submodule
    try:
        from edge.matching import CoWMatchingSolver  # type: ignore[import-untyped]
        from edge.pool_indexer import LongTailRouter  # type: ignore[import-untyped]

        strategies.append(CoWMatchingSolver())
        strategies.append(LongTailRouter())
        log.info("edge_strategies_loaded")
    except ImportError:
        log.info("edge_strategies_not_present", reason="public_clone_or_phase0")

    # Always include naive as last resort
    oneinch = OneInchClient(api_key=oneinch_api_key, chain_id=42161)
    strategies.append(NaiveSolver(oneinch=oneinch))

    return strategies
