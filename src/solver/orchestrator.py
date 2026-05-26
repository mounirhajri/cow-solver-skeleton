import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from src.log import get_logger
from src.models.auction import Auction
from src.models.solution import Solution
from src.solver.base import NoSolution, SolverStrategy
from src.solver.ebbo import DEFAULT_TOLERANCE_BPS

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
        ebbo_multicall: object = None,
        ebbo_intermediates: list[str] | None = None,
        ebbo_tolerance_bps: int = DEFAULT_TOLERANCE_BPS,
    ) -> None:
        if not strategies:
            raise ValueError("at least one strategy required")
        self._strategies = list(strategies)
        self._timeout = per_strategy_timeout
        self._run_all = run_all_strategies
        self._compose = compose
        # EBBO pre-submission validator.  None for ebbo_multicall disables the
        # check — used in tests and in the public-clone path where multicall
        # plumbing might not be wired up.  See src/solver/ebbo.py for the
        # external-quote logic; the check runs on the FINAL Solution we are
        # about to return, after composition + naive-exclusion.
        self._ebbo_multicall = ebbo_multicall
        self._ebbo_intermediates = ebbo_intermediates or []
        self._ebbo_tolerance_bps = ebbo_tolerance_bps

    async def solve(
        self,
        auction: Auction,
        attempts: list[AttemptRecord] | None = None,
    ) -> tuple[Solution | NoSolution, list[AttemptRecord]]:
        # Callers may pass an attempts list to capture partial state across an
        # outer cancellation (e.g. main.py's wait_for timeout) — the list is
        # mutated in place so the caller can persist whatever was collected
        # before cancellation hit. When None, a fresh list is created (default
        # behaviour for tests + standalone use).
        if attempts is None:
            attempts = []
        winning_solutions: list[tuple[str, Solution]] = []

        # Spec §3: count smart-wallet orders once per auction, before the
        # strategy loop.  Strategies must NOT log this event — doing so would
        # triple the Loki/journal volume (once per strategy × 3 strategies).
        n_eip1271 = sum(1 for o in auction.orders if o.is_smart_wallet_signed)
        n_eoa = len(auction.orders) - n_eip1271
        log.info(
            "smart_wallet_orders_observed",
            auction_id=auction.id,
            n_eip1271=n_eip1271,
            n_eoa=n_eoa,
        )

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

        # Composer candidates exclude naive: its clearingPrices come from the
        # auction oracle (or price_refiner's V2/V3 spot quotes), which over-
        # state realised settlement prices — composing them in fabricates a
        # CIP-14 score that no production batch could execute against
        # (verified live 2026-05-24: composer routinely scored ~481 ETH per
        # win because naive's executed_amounts × oracle prices dwarfed every
        # real solver's surplus).  Naive stays in the chain as the "always
        # respond" baseline; we just never submit it as our solution.
        composable_solutions = [
            (name, sol) for name, sol in winning_solutions if name != "naive"
        ]

        if self._compose and len(composable_solutions) > 1:
            try:
                from edge.matching import CandidateSolution, compose
                candidates = [
                    CandidateSolution(strategy=name, solution=sol)
                    for name, sol in composable_solutions
                ]
                composed = compose(candidates, auction_id=int(auction.id))
                if composed is not None:
                    log.info("composer_merged",
                             n_candidates=len(composable_solutions),
                             n_trades=len(composed.trades),
                             auction_id=auction.id)
                    attempts.append(AttemptRecord(
                        strategy="composer",
                        status="solved",
                        latency_ms=None,
                        solution=composed.model_dump(mode="json", by_alias=True),
                        error=None,
                    ))
                    final = await self._ebbo_validate(composed, auction, attempts)
                    if final is not None:
                        return final, attempts
                    # EBBO violated → drop composed, try first-winner fallback
            except ImportError:
                pass  # edge not present — fall through to first-winner

        # Fallback chain — try each non-naive candidate in order; ship the
        # first one EBBO accepts.  Without iteration, an EBBO-rejected
        # composed solution would silently fall through to a known-untested
        # first-candidate solution that EBBO might also reject, and a
        # second candidate that WOULD pass never gets a chance.
        for _name, candidate in composable_solutions:
            final = await self._ebbo_validate(candidate, auction, attempts)
            if final is not None:
                return final, attempts
        return NoSolution(), attempts

    async def _ebbo_validate(
        self,
        solution: Solution,
        auction: Auction,
        attempts: list[AttemptRecord],
    ) -> Solution | None:
        """Run EBBO check on a candidate solution.

        Returns the solution untouched if EBBO passes or the validator is
        disabled (no multicall plumbed).  Returns None if EBBO rejects it —
        caller treats that as "fall through to next candidate or NoSolution".

        Records an ``AttemptRecord`` for the rejection either way so shadow
        data captures the violation rate over time.
        """
        if self._ebbo_multicall is None:
            return solution

        from src.solver.ebbo import validate_solution_ebbo
        try:
            result = await validate_solution_ebbo(
                solution,
                auction,
                self._ebbo_multicall,  # type: ignore[arg-type]
                self._ebbo_intermediates,
                tolerance_bps=self._ebbo_tolerance_bps,
            )
        except Exception as exc:  # noqa: BLE001
            # EBBO is a safety net — its failure must NOT block solver output.
            # Log loudly and pass through so we don't deny ourselves revenue
            # over an RPC blip in the validator itself.
            log.warning(
                "ebbo_validator_failed", auction_id=auction.id, error=str(exc)
            )
            return solution

        log.info(
            "ebbo_check_done",
            auction_id=auction.id,
            passes=result.passes,
            n_checked=result.n_checked,
            n_skipped=result.n_skipped,
            n_violations=len(result.violations),
        )
        if not result.passes:
            log.warning(
                "ebbo_solution_rejected",
                auction_id=auction.id,
                violations=result.violations[:5],
            )
            attempts.append(AttemptRecord(
                strategy="ebbo-rejected",
                status="rejected",
                latency_ms=None,
                solution=solution.model_dump(mode="json", by_alias=True),
                error="; ".join(result.violations[:5]),
            ))
            return None
        return solution


def load_default_strategies() -> list[SolverStrategy]:
    """Build the strategy chain. Loads edge strategies if private submodule present.

    Order: naive first (always fast, anchors the response), then edge strategies
    (specialized, selective), then router-v2 (slow on-chain quotes, shadow data).

    Delegates to ``_load_default_strategies_with_multicall`` so the strategy
    construction logic lives in exactly one place.
    """
    from src.routing.multicall import Multicall3
    from src.routing.rpc import RpcClient
    from src.config import settings

    rpc = RpcClient(settings.rpc_arbitrum)
    multicall = Multicall3(rpc)
    return _load_default_strategies_with_multicall(multicall)


def load_default_orchestrator() -> SolverOrchestrator:
    """Build the production orchestrator with strategies + EBBO wired.

    Splits out the multicall instance so the EBBO validator can reuse the
    same RPC connection budget as NaiveSolver / RouterSolver — no extra
    concurrent slots.  Used by ``src/main.py``; unit tests still construct
    ``SolverOrchestrator`` directly with mock strategies + no EBBO.
    """
    from src.config import settings
    from src.routing.multicall import Multicall3
    from src.routing.rpc import RpcClient

    rpc = RpcClient(settings.rpc_arbitrum)
    multicall = Multicall3(rpc)
    strategies = _load_default_strategies_with_multicall(multicall)

    ebbo_multicall = multicall if settings.ebbo_enabled else None
    return SolverOrchestrator(
        strategies=strategies,
        per_strategy_timeout=settings.solve_timeout_seconds / max(1, len(strategies)),
        ebbo_multicall=ebbo_multicall,
        ebbo_intermediates=settings.router_intermediate_tokens,
        ebbo_tolerance_bps=settings.ebbo_tolerance_bps,
    )


def _load_default_strategies_with_multicall(multicall: Any) -> list[SolverStrategy]:
    """Build the strategy chain around an injected multicall instance.

    Single source of truth for strategy construction — both
    ``load_default_strategies`` (standalone / test use) and
    ``load_default_orchestrator`` (production) delegate here so changes
    only need to be made in one place.
    """
    from src.config import settings
    from src.solver.naive import NaiveSolver
    from src.solver.router import RouterSolver

    strategies: list[SolverStrategy] = []
    strategies.append(NaiveSolver(
        multicall=multicall,
        intermediates=settings.intermediate_tokens,
        refine_timeout=3.0,
    ))

    try:
        import redis.asyncio as aioredis

        from edge.classifier.predict import TokenClassifier
        from edge.matching import BipartiteMatcher, CoWMatchingSolver
        from edge.matching.ghost_detector import DynamicGhostDetector
        from edge.pool_indexer import LongTailRouter
        from edge.pool_indexer.pool_cache import PoolCache
        from src.persistence.db import get_session_factory

        classifier = TokenClassifier.load()
        session_factory = get_session_factory()
        ghost_detector = DynamicGhostDetector(session_factory=session_factory)
        strategies.append(BipartiteMatcher(
            classifier=classifier,
            session_factory=session_factory,
            ghost_detector=ghost_detector,
        ))
        strategies.append(CoWMatchingSolver(
            classifier=classifier,
            session_factory=session_factory,
            otm_tolerance_bps=settings.multi_party_otm_tolerance_bps,
            ring_cooldown_seconds=settings.multi_party_ring_cooldown_seconds,
            ghost_detector=ghost_detector,
        ))
        if settings.long_tail_enabled:
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
            long_tail_enabled=settings.long_tail_enabled,
        )
    except (ImportError, TypeError):
        # ImportError  — edge submodule not present (public clone / phase-0).
        # TypeError    — edge submodule present but at a mismatched version where
        #                a strategy constructor doesn't accept a new keyword arg
        #                (e.g. ghost_detector) yet.  Degrade gracefully instead of
        #                crashing the server on startup.
        log.info("edge_strategies_not_present", reason="public_clone_or_phase0")

    strategies.append(RouterSolver(
        multicall=multicall,
        intermediates=settings.router_intermediate_tokens,
        max_orders=settings.router_max_orders,
        max_concurrent=settings.router_max_concurrent,
        strategy_timeout=settings.router_strategy_timeout,
    ))
    return strategies
