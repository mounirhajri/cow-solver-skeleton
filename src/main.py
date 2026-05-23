import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from src.config import settings
from src.log import configure_logging, get_logger
from src.metrics import SOLVE_DURATION, SOLVE_TOTAL
from src.models.auction import Auction
from src.models.solution import Solution
from src.shadow.logger import SolutionLogger
from src.shadow.persist import persist_shadow_attempt_safe
from src.solver.base import NoSolution
from src.solver.orchestrator import SolverOrchestrator, load_default_strategies

log = get_logger(__name__)


def create_app(
    orchestrator: SolverOrchestrator | Any,
    shadow_logger: SolutionLogger | None = None,
) -> FastAPI:
    """Factory so tests can inject a mock orchestrator."""

    app = FastAPI(title="cow-solver-skeleton")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/solve")
    async def solve(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
        start = time.perf_counter()
        body = await request.json()
        auction = Auction.model_validate(body)

        try:
            result, attempts = await asyncio.wait_for(
                orchestrator.solve(auction),
                timeout=settings.solve_timeout_seconds,
            )
        except TimeoutError:
            log.warning("solve_timeout", auction_id=auction.id, timeout=settings.solve_timeout_seconds)
            SOLVE_TOTAL.labels(outcome="error").inc()
            return _empty_solution(auction.id)
        except Exception as e:  # noqa: BLE001
            log.error("solve_error", auction_id=auction.id, error=str(e))
            SOLVE_TOTAL.labels(outcome="error").inc()
            return _empty_solution(auction.id)

        # Persist shadow data in the background — never blocks the hot path
        background_tasks.add_task(persist_shadow_attempt_safe, auction, attempts, None)

        if isinstance(result, NoSolution):
            SOLVE_TOTAL.labels(outcome="no_solution").inc()
            if shadow_logger:
                shadow_logger.record(auction_id=auction.id, our_solution=None)
            return _empty_solution(auction.id)

        SOLVE_TOTAL.labels(outcome="solution").inc()
        SOLVE_DURATION.observe(time.perf_counter() - start)
        if shadow_logger:
            shadow_logger.record(auction_id=auction.id, our_solution=result)
        return result.model_dump(by_alias=True, mode="json")

    return app


def _empty_solution(auction_id: str) -> dict[str, Any]:
    return Solution(id=int(auction_id), prices={}, trades=[], interactions=[]).model_dump(
        by_alias=True, mode="json"
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    configure_logging(level=settings.log_level)
    log.info("startup", config=settings.model_dump())
    yield
    log.info("shutdown")


def build_default_app() -> FastAPI:
    """Entry point used by uvicorn in --factory mode.

    Kept as factory (not module-level `app`) so importing this module in tests
    does not trigger filesystem and network side-effects.
    """
    configure_logging(level=settings.log_level)
    strategies = load_default_strategies()
    orchestrator = SolverOrchestrator(
        strategies=strategies,
        per_strategy_timeout=settings.solve_timeout_seconds / max(1, len(strategies)),
    )
    shadow_logger = SolutionLogger(path=settings.shadow_log_path)
    return create_app(orchestrator=orchestrator, shadow_logger=shadow_logger)
