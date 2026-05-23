import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass

from src.log import get_logger
from src.models.auction import Auction
from src.models.solution import Solution
from src.solver.base import NoSolution, SolverStrategy

log = get_logger(__name__)


@dataclass(frozen=True)
class AttemptRecord:
    strategy: str
    status: str  # "solved" | "no_solution" | "error" | "timeout"
    latency_ms: int | None
    solution: dict[str, object] | None  # solution.model_dump(mode='json') or None
    error: str | None


class SolverOrchestrator:
    """Tries strategies in order; returns first non-empty solution.

    Each strategy is bounded by per_strategy_timeout. Strategies that exceed
    the timeout are cancelled and the orchestrator falls through to the next.

    When run_all_strategies=True (Phase 1 default), all strategies are tried
    even after a winner is found, to collect comparison data for shadow mode.
    When run_all_strategies=False (Phase 4), iteration stops at first winner.
    """

    def __init__(
        self,
        strategies: Sequence[SolverStrategy],
        per_strategy_timeout: float = 5.0,
        run_all_strategies: bool = True,
        compose: bool = True,
    ) -> None:
        if not strategies:
            raise ValueError("at least one strategy required")
        self._strategies = list(strategies)
        self._timeout = per_strategy_timeout
        self._run_all = run_all_strategies
        self._compose = compose

    async def solve(self, auction: Auction) -> tuple[Solution | NoSolution, list[AttemptRecord]]:
        attempts: list[AttemptRecord] = []
        winning_solutions: list[tuple[str, Solution]] = []

        for strat in self._strategies:
            start = time.perf_counter()
            try:
                result = await asyncio.wait_for(strat.solve(auction), timeout=self._timeout)
            except TimeoutError:
                latency_ms = int((time.perf_counter() - start) * 1000)
                log.warning("strategy_timeout", strategy=strat.name, auction_id=auction.id)
                attempts.append(AttemptRecord(
                    strategy=strat.name,
                    status="timeout",
                    latency_ms=latency_ms,
                    solution=None,
                    error=f"timeout after {self._timeout}s",
                ))
                continue
            except Exception as e:  # noqa: BLE001
                latency_ms = int((time.perf_counter() - start) * 1000)
                log.error(
                    "strategy_error", strategy=strat.name, error=str(e), auction_id=auction.id
                )
                attempts.append(AttemptRecord(
                    strategy=strat.name,
                    status="error",
                    latency_ms=latency_ms,
                    solution=None,
                    error=str(e),
                ))
                continue

            latency_ms = int((time.perf_counter() - start) * 1000)

            if isinstance(result, Solution):
                attempts.append(AttemptRecord(
                    strategy=strat.name,
                    status="solved",
                    latency_ms=latency_ms,
                    solution=result.model_dump(mode="json", by_alias=True),
                    error=None,
                ))
                if not winning_solutions:
                    log.info("strategy_won", strategy=strat.name, auction_id=auction.id)
                winning_solutions.append((strat.name, result))
                # If run_all is False, stop at first winner
                if not self._run_all:
                    break
            else:
                attempts.append(AttemptRecord(
                    strategy=strat.name,
                    status="no_solution",
                    latency_ms=latency_ms,
                    solution=None,
                    error=None,
                ))

        # Try composing when multiple strategies produced solutions
        if self._compose and len(winning_solutions) > 1:
            try:
                from edge.matching import CandidateSolution, compose
                candidates = [
                    CandidateSolution(strategy=name, solution=sol)
                    for name, sol in winning_solutions
                ]
                composed = compose(candidates, auction_id=int(auction.id))
                if composed is not None:
                    log.info("composer_merged",
                             n_candidates=len(winning_solutions),
                             n_trades=len(composed.trades),
                             auction_id=auction.id)
                    attempts.append(AttemptRecord(
                        strategy="composer",
                        status="solved",
                        latency_ms=None,
                        solution=composed.model_dump(mode="json", by_alias=True),
                        error=None,
                    ))
                    return composed, attempts
            except ImportError:
                pass  # edge not present — fall through to first-winner

        # First-winner fallback
        if winning_solutions:
            return winning_solutions[0][1], attempts
        return NoSolution(), attempts


def load_default_strategies() -> list[SolverStrategy]:
    """Build the strategy chain. Loads edge strategies if private submodule present.

    Order: edge strategies first (more specialized), router-v2 as workhorse,
    naive last (fallback).
    """
    from src.config import settings
    from src.routing.multicall import Multicall3
    from src.routing.rpc import RpcClient
    from src.solver.naive import NaiveSolver
    from src.solver.router import RouterSolver

    strategies: list[SolverStrategy] = []

    try:
        from edge.matching import BipartiteMatcher, CoWMatchingSolver
        from edge.pool_indexer import LongTailRouter

        strategies.append(BipartiteMatcher())  # cheap, selective
        strategies.append(CoWMatchingSolver())  # multi-party rings
        strategies.append(LongTailRouter())
        log.info("edge_strategies_loaded")
    except ImportError:
        log.info("edge_strategies_not_present", reason="public_clone_or_phase0")

    # Router-v2: primary workhorse
    rpc = RpcClient(settings.rpc_arbitrum)
    multicall = Multicall3(rpc)
    strategies.append(RouterSolver(multicall=multicall, intermediates=settings.intermediate_tokens))

    strategies.append(NaiveSolver())
    return strategies
