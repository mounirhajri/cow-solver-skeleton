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
            # Strategies may declare their own timeout via a numeric `timeout`
            # attribute (e.g. RouterSolver needs 9 s for on-chain RPC quoting).
            # Only honour the attribute when it is an actual number; mock objects
            # expose a `timeout` as a child mock, not a float, so we fall back to
            # the orchestrator-wide default in that case.
            _strat_timeout_attr = getattr(strat, "timeout", None)
            strat_timeout = (
                _strat_timeout_attr
                if isinstance(_strat_timeout_attr, (int, float))
                else self._timeout
            )
            try:
                result = await asyncio.wait_for(strat.solve(auction), timeout=strat_timeout)
            except TimeoutError:
                latency_ms = int((time.perf_counter() - start) * 1000)
                log.warning("strategy_timeout", strategy=strat.name, auction_id=auction.id)
                attempts.append(AttemptRecord(
                    strategy=strat.name,
                    status="timeout",
                    latency_ms=latency_ms,
                    solution=None,
                    error=f"timeout after {strat_timeout}s",
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

    Order: naive first (always fast, anchors the response), then edge strategies
    (specialized, selective), then router-v2 (slow on-chain quotes, shadow data).
    """
    from src.config import settings
    from src.routing.multicall import Multicall3
    from src.routing.rpc import RpcClient
    from src.solver.naive import NaiveSolver
    from src.solver.router import RouterSolver

    strategies: list[SolverStrategy] = []

    rpc = RpcClient(settings.rpc_arbitrum)
    multicall = Multicall3(rpc)

    # Naive first: <10 ms to find trades (oracle prices as filter).
    # With multicall injected, oracle clearing prices are replaced with real
    # DEX quotes for the specific token pairs touched — typically 3-10 pairs,
    # done in parallel, adds ~200-500 ms but produces accurate CIP-14 scores.
    strategies.append(NaiveSolver(
        multicall=multicall,
        intermediates=settings.intermediate_tokens,
        refine_timeout=3.0,
    ))

    try:
        import redis.asyncio as aioredis

        from edge.classifier.predict import TokenClassifier
        from edge.matching import BipartiteMatcher, CoWMatchingSolver
        from edge.pool_indexer import LongTailRouter
        from edge.pool_indexer.pool_cache import PoolCache
        from src.persistence.db import get_session_factory

        # TokenClassifier.load() never raises — when the pickle is missing it
        # returns an instance whose `.model is None`, which the filter treats
        # as a no-op. So a missing model degrades gracefully to the old path.
        classifier = TokenClassifier.load()
        # `get_session_factory()` returns an `async_sessionmaker[AsyncSession]`
        # — itself a callable that yields an AsyncSession context. The filter
        # opens its own short-lived session via `async with session_factory()`.
        session_factory = get_session_factory()
        strategies.append(BipartiteMatcher(
            classifier=classifier,
            session_factory=session_factory,
        ))
        strategies.append(CoWMatchingSolver(
            classifier=classifier,
            session_factory=session_factory,
        ))
        # Long-tail router shares the multicall instance with NaiveSolver/RouterSolver
        # and is backed by a Redis cache (pool addresses ~7d, reserves ~60s).
        redis_client = aioredis.Redis.from_url(
            settings.redis_url, decode_responses=False
        )
        pool_cache = PoolCache(
            redis=redis_client,
            key_prefix=settings.redis_key_prefix,
            reserves_ttl=settings.pool_cache_ttl_seconds,
        )
        strategies.append(LongTailRouter(
            multicall=multicall,
            pool_cache=pool_cache,
        ))
        log.info(
            "edge_strategies_loaded",
            rf_model_loaded=classifier.model is not None,
        )
    except ImportError:
        log.info("edge_strategies_not_present", reason="public_clone_or_phase0")

    # Router-v2 last: on-chain quotes for top-N orders by sell_amount.
    # Shares the same multicall/rpc instance as NaiveSolver (no extra connections).
    strategies.append(RouterSolver(
        multicall=multicall,
        intermediates=settings.router_intermediate_tokens,
        max_orders=settings.router_max_orders,
        max_concurrent=settings.router_max_concurrent,
        strategy_timeout=settings.router_strategy_timeout,
    ))

    return strategies
