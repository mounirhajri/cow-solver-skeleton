import asyncio
import os
import re
import time
from contextlib import asynccontextmanager
from contextlib import suppress as _suppress
from datetime import UTC, datetime
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError

from src.config import settings
from src.log import configure_logging, get_logger
from src.metrics import SOLVE_DURATION, SOLVE_TOTAL
from src.models.auction import Auction
from src.models.solution import Solution
from src.routing.rpc import gate_stats
from src.shadow.logger import SolutionLogger
from src.shadow.persist import persist_shadow_attempt_safe
from src.solver.base import NoSolution
from src.solver.orchestrator import AttemptRecord, SolverOrchestrator, load_default_orchestrator

log = get_logger(__name__)

# CoW timestamps carry NANOsecond precision ("…T13:11:54.833852274Z");
# datetime.fromisoformat only accepts up to microseconds → trim to 6 digits.
_FRACTION_TRIM = re.compile(r"\.(\d{6})\d+")


def _deadline_budget_seconds(deadline: str | None) -> float | None:
    """Seconds remaining until the driver's deadline; None when unknown.

    Fail-open: an absent or unparseable deadline returns None and the caller
    falls back to the configured solve timeout — a malformed timestamp must
    never reject an auction.
    """
    if not deadline:
        return None
    try:
        s = _FRACTION_TRIM.sub(r".\1", deadline.strip())
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (dt - datetime.now(UTC)).total_seconds()
    except Exception:  # noqa: BLE001
        log.warning("solve_deadline_unparseable", deadline=deadline)
        return None


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
        # Response shape mirrors solver-engine OpenAPI: {"solutions": [...]}.
        # The driver picks among multiple solutions by simulating them and
        # selecting the highest-scoring one. We currently emit at most one
        # solution per auction; the array form keeps us spec-compliant and
        # leaves room for future multi-solution emission (e.g. variants
        # exploring different fee tiers).
        start = time.perf_counter()
        # Liveness watchdog: record that traffic is arriving. The watchdog task
        # (below) restarts the process if traffic keeps coming but no solve
        # makes progress — bounding the 2026-06-13 silent-death degradation to
        # minutes instead of days. Lazy-start it on the serving loop (create_app
        # has no lifespan wiring; first request is the earliest loop-safe point).
        app.state.last_request_ts = time.monotonic()
        if getattr(app.state, "diag_task", None) is None:
            app.state.diag_task = asyncio.create_task(_loop_diagnostics_pulse())
            app.state.watchdog_task = asyncio.create_task(_liveness_watchdog(app))
        # Epoch timestamp of request arrival — persisted alongside the attempt
        # so the feasibility validation can reconstruct the auction-time block
        # for driver auctions (which carry no simulationBlock).
        received_at = time.time()
        # A malformed request is the driver's input, not a solver failure —
        # answer 400/422 (not an unhandled 500) so the cause is legible during
        # onboarding rather than looking like the endpoint crashed.
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("solve_invalid_json", error=str(exc))
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        try:
            auction = Auction.model_validate(body)
        except ValidationError as exc:
            log.warning("solve_invalid_auction", n_errors=exc.error_count())
            raise HTTPException(
                status_code=422, detail="auction failed schema validation"
            ) from exc

        # Quote-only requests: spec allows ``id=null`` when the driver asks
        # the solver to price tokens without running an auction. Our
        # solver does not implement quoting; return empty solutions early
        # so downstream code paths (persist, naive, orchestrator) never
        # encounter the unexpected None and need to defensively handle it.
        if auction.id is None:
            SOLVE_TOTAL.labels(outcome="no_solution").inc()
            return _empty_solutions()

        # Pre-allocate the attempts list so the orchestrator can mutate it in
        # place; this preserves partial shadow data even when the outer
        # wait_for cancels mid-strategy (e.g. multi-party LP exceeding the
        # solve_timeout). Without this, every timeout left shadow_solutions
        # un-written (verified outage 2026-05-24 → 2026-05-25).
        # Respect the driver's deadline: the CoW driver aborts the request at
        # its own cutoff (~5.8 s measured live), so a solution returned later
        # never competes — it only shows up as kind=timeout in /notify. Bound
        # the solve to (deadline - now - margin) and ALWAYS answer in time;
        # auctions without a deadline (internal poller) keep the full budget.
        timeout = settings.solve_timeout_seconds
        budget = _deadline_budget_seconds(auction.deadline)
        if budget is not None:
            timeout = min(timeout, budget - settings.solve_deadline_margin_seconds)
            if timeout <= 0:
                log.warning(
                    "solve_deadline_already_passed",
                    auction_id=auction.id,
                    budget_seconds=round(budget, 3),
                )
                SOLVE_TOTAL.labels(outcome="no_solution").inc()
                return _empty_solutions()

        attempts: list[AttemptRecord] = []
        try:
            result, _ = await asyncio.wait_for(
                orchestrator.solve(auction, attempts),
                timeout=timeout,
            )
            # Progress signal for the liveness watchdog: the orchestrator
            # completed within budget (solution OR no_solution — both mean the
            # solve machinery works). Only the TimeoutError path below is
            # NON-progress; sustained non-progress under traffic = the dead
            # state the watchdog restarts on.
            app.state.last_progress_ts = time.monotonic()
        except TimeoutError:
            # Degradation forensics (2026-06-12): WHERE did the budget die
            # (last strategy that got to run), and is the RPC gate starved
            # (slots_free 0 + waiters piling = lost-slot leak) or healthy
            # (stall lives elsewhere)? n_tasks catches background-task pileup.
            log.warning(
                "solve_timeout",
                auction_id=auction.id,
                timeout=round(timeout, 3),
                last_strategy=attempts[-1].strategy if attempts else None,
                n_tasks=len(asyncio.all_tasks()),
                **gate_stats(),
            )
            background_tasks.add_task(
                persist_shadow_attempt_safe, auction, attempts, None, received_at
            )
            # Best-so-far salvage: completed strategies live in `attempts`
            # even when a later one overran the deadline. Throwing away a
            # finished router solution because multi-party was still chewing
            # cost ~93% of all answers on 2026-06-12 — return the best
            # completed solution instead of an empty response.
            salvaged = _best_completed_solution(attempts)
            if salvaged is not None:
                SOLVE_TOTAL.labels(outcome="solution").inc()
                if shadow_logger:
                    shadow_logger.record(auction_id=auction.id, our_solution=salvaged)
                return {"solutions": [salvaged.model_dump(by_alias=True, mode="json")]}
            SOLVE_TOTAL.labels(outcome="error").inc()
            return _empty_solutions()
        except Exception as e:  # noqa: BLE001
            log.error("solve_error", auction_id=auction.id, error=str(e))
            SOLVE_TOTAL.labels(outcome="error").inc()
            background_tasks.add_task(
                persist_shadow_attempt_safe, auction, attempts, None, received_at
            )
            return _empty_solutions()

        # Persist shadow data in the background — never blocks the hot path
        background_tasks.add_task(persist_shadow_attempt_safe, auction, attempts, None, received_at)

        if isinstance(result, NoSolution):
            SOLVE_TOTAL.labels(outcome="no_solution").inc()
            if shadow_logger:
                shadow_logger.record(auction_id=auction.id, our_solution=None)
            return _empty_solutions()

        SOLVE_TOTAL.labels(outcome="solution").inc()
        SOLVE_DURATION.observe(time.perf_counter() - start)
        if shadow_logger:
            shadow_logger.record(auction_id=auction.id, our_solution=result)
        return {"solutions": [result.model_dump(by_alias=True, mode="json")]}

    @app.post("/notify")
    async def notify(request: Request) -> dict[str, Any]:
        # Per OpenAPI, the driver POSTs a status notification after each
        # auction with the outcome of the solution we submitted. The spec
        # accepts an opaque JSON payload (auctionId, solutionId, kind, plus
        # type-specific metadata). For now we log and acknowledge; future
        # work persists these into shadow_solutions to correlate emission
        # vs. on-chain settlement outcome.
        with _suppress(Exception):
            body = await request.json()
            log.info("driver_notification", **(body if isinstance(body, dict) else {"raw": body}))
        return {}

    return app


# Salvage preference at deadline: the router is the scored value driver;
# matching strategies are validated but rarer. naive is deliberately ABSENT:
# the orchestrator never submits it (oracle prices fabricate phantom CIP-14
# scores, verified live 2026-05-24) and the salvage path must not reintroduce
# exactly that class of solution behind the composer's back.
_SALVAGE_ORDER = ("router-v2", "cow-matching-bipartite", "cow-matching-multi-party")


def _best_completed_solution(attempts: list[AttemptRecord]) -> Solution | None:
    """Pick the best already-completed solution from a cancelled solve.

    AttemptRecord.solution holds ``model_dump(mode="json", by_alias=True)``
    (see orchestrator's record append) — re-validate through the Solution
    model (aliases accept both forms) and return the OBJECT so the caller
    can both log it to the shadow JSONL and dump it ``by_alias`` for the
    wire, identical to the normal return path. Any validation hiccup falls
    through to the next candidate (never raise on the salvage path).
    """
    by_strategy = {
        a.strategy: a.solution
        for a in attempts
        if a.status == "solved" and a.solution is not None
    }
    for name in _SALVAGE_ORDER:
        raw = by_strategy.get(name)
        if raw is None:
            continue
        try:
            return Solution.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("salvage_solution_invalid", strategy=name, error=str(exc))
    return None


async def _liveness_watchdog(app: FastAPI) -> None:
    """Restart the process if it stops solving while traffic keeps arriving.

    The 2026-06-13 degradation killed the solver SILENTLY for ~2 days: every
    /solve hit the deadline and returned empty, while /health stayed "ok"
    (it only proves the loop runs, not that solves complete). A 3-day-old
    process accumulates some state — gate-, DB-pool- and Redis-clean in every
    check we ran — that hangs each solve before any strategy finishes; a
    fresh process solves the same auction in ~2 s. We could not pin the leak,
    so this bounds it: if requests are still arriving (last_request within the
    traffic window) but NO solve has made progress for longer than the stall
    window, exit non-zero. ``restart: unless-stopped`` then brings the process
    back fresh — turning "dead for days" into "down for the restart".

    Runs on the serving loop. During the degradation the loop itself stays
    healthy (the diagnostics pulse fired throughout), so this task keeps
    ticking and can act. Disabled when ``watchdog_enabled`` is False.
    """
    if not settings.watchdog_enabled:
        return
    traffic_window = settings.watchdog_traffic_window_seconds
    stall_window = settings.watchdog_stall_seconds
    # Seed progress at startup so a quiet boot period is never mistaken for a
    # stall (no traffic → no restart regardless).
    now = time.monotonic()
    app.state.last_progress_ts = now
    app.state.last_request_ts = now
    while True:
        await asyncio.sleep(settings.watchdog_poll_seconds)
        now = time.monotonic()
        secs_since_request = now - getattr(app.state, "last_request_ts", now)
        secs_since_progress = now - getattr(app.state, "last_progress_ts", now)
        receiving_traffic = secs_since_request <= traffic_window
        stalled = secs_since_progress >= stall_window
        if receiving_traffic and stalled:
            log.error(
                "liveness_watchdog_restart",
                secs_since_progress=round(secs_since_progress, 1),
                secs_since_request=round(secs_since_request, 1),
                stall_window=stall_window,
                reason="receiving traffic but no solve progressed — restarting",
            )
            # os._exit bypasses the asyncio/uvicorn shutdown path on purpose:
            # the process is wedged, a graceful shutdown could hang too. The
            # container restart policy gives us a clean process.
            os._exit(1)


async def _loop_diagnostics_pulse() -> None:
    """Minute heartbeat: RPC-gate slots + waiter queue + asyncio task count.

    Observability for the 2026-06-12 degradation (all-timeouts ~1h after
    every restart, idle CPU/memory/FDs). The pulse gives the decay a time
    series: slots_free trending to 0 → gate-slot leak; n_tasks climbing →
    background-task pileup; both flat while solves still time out → the
    stall lives below (httpx pool / DNS). Remove once the hunt is over.
    """
    while True:
        with _suppress(Exception):
            log.info(
                "loop_diagnostics",
                n_tasks=len(asyncio.all_tasks()),
                **gate_stats(),
            )
        await asyncio.sleep(60)


def _empty_solutions() -> dict[str, Any]:
    """OpenAPI-compliant empty response — used for timeouts, errors, and
    no-solution outcomes. Distinct from a Solution with empty trades, which
    the driver would interpret as a valid (but empty) settlement attempt."""
    return {"solutions": []}


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
    # load_default_orchestrator wires EBBO + the multicall shared across
    # naive/router into a single SolverOrchestrator. Tests construct
    # SolverOrchestrator directly with mock strategies + no EBBO.
    orchestrator = load_default_orchestrator()
    shadow_logger = SolutionLogger(path=settings.shadow_log_path)
    return create_app(orchestrator=orchestrator, shadow_logger=shadow_logger)
