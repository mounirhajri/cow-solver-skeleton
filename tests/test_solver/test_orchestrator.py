import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.models.auction import Auction
from src.models.order import Order
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


async def test_orchestrator_logs_smart_wallet_ratio_once_per_auction(
    auction: Auction,
) -> None:
    """Spec §3: smart_wallet_orders_observed fires exactly once at orchestrator
    level, not 3× (once per strategy).  Two strategies both run; the event must
    appear exactly once regardless of how many strategies are in the chain."""
    log_events: list[tuple[str, dict]] = []

    import src.solver.orchestrator as orch_mod

    original_log_info = orch_mod.log.info

    def _capture(event: str, **kw: object) -> None:
        log_events.append((event, kw))
        original_log_info(event, **kw)

    orch_mod.log.info = _capture  # type: ignore[method-assign]

    try:
        # Build an auction with a mix of EIP-1271 (smart-wallet) and EOA orders.
        base_order = auction.orders[0]
        # Patch-construct a minimal EIP-1271 order by overriding signing_scheme.
        eip1271_order = Order(
            uid="0x" + "e" * 112,
            sellToken=base_order.sell_token,
            buyToken=base_order.buy_token,
            sellAmount=str(base_order.sell_amount),
            buyAmount=str(base_order.buy_amount),
            feePolicies=[],
            validTo=base_order.valid_to,
            kind=base_order.kind,
            owner=base_order.owner,
            partiallyFillable=base_order.partially_fillable,
            **{"class": base_order.class_},
            signingScheme="eip1271",
        )
        mixed_auction = auction.model_copy(
            update={"orders": [base_order, eip1271_order]}
        )

        # Two strategies: both return NoSolution (we care only about the log).
        s1 = AsyncMock(name="s1")
        s1.name = "s1"
        s1.solve.return_value = NoSolution()
        s2 = AsyncMock(name="s2")
        s2.name = "s2"
        s2.solve.return_value = NoSolution()

        orch = SolverOrchestrator(
            strategies=[s1, s2],
            per_strategy_timeout=1.0,
            run_all_strategies=True,
        )
        await orch.solve(mixed_auction)

        smart_wallet_logs = [
            e for e in log_events if e[0] == "smart_wallet_orders_observed"
        ]
        assert len(smart_wallet_logs) == 1, (
            f"expected exactly 1 smart_wallet_orders_observed event, got "
            f"{len(smart_wallet_logs)}"
        )
        kw = smart_wallet_logs[0][1]
        assert kw["n_eip1271"] == 1, f"expected n_eip1271=1, got {kw['n_eip1271']}"
        assert kw["n_eoa"] == 1, f"expected n_eoa=1, got {kw['n_eoa']}"
        assert kw["auction_id"] == mixed_auction.id
    finally:
        orch_mod.log.info = original_log_info  # type: ignore[method-assign]


async def test_ebbo_rejection_falls_through_to_next_candidate(auction: Auction) -> None:
    """When EBBO rejects the composed solution (or first candidate), the
    orchestrator must try the NEXT candidate rather than ship NoSolution.

    Reviewer-flagged bug in PR #22's first-winner fallback: only the FIRST
    composable candidate was EBBO-checked; if rejected we shipped NoSolution
    even when a later candidate would have passed.
    """
    # Two strategies both solve; we'll configure ebbo to reject the FIRST one.
    sol_first = _solution()
    sol_second = _solution()
    sol_second.trades = [Trade(kind="fulfillment", order_uid="0x" + "c" * 112,
                                executed_amount=10**18)]

    s1 = AsyncMock(name="s1")
    s1.name = "s1"
    s1.solve.return_value = sol_first
    s2 = AsyncMock(name="s2")
    s2.name = "s2"
    s2.solve.return_value = sol_second

    # Inject an EBBO stub that rejects sol_first and accepts sol_second.
    from src.solver import ebbo as ebbo_mod
    from src.solver.ebbo import EBBOResult
    call_count = {"n": 0}

    async def fake_validate(solution, *_a, **_kw):
        call_count["n"] += 1
        if solution is sol_first:
            return EBBOResult(passes=False, violations=["mocked rejection"],
                              n_checked=1, n_skipped=0)
        return EBBOResult(passes=True, violations=[], n_checked=1, n_skipped=0)

    import src.solver.orchestrator as orch_mod
    orch_mod.validate_solution_ebbo = fake_validate  # type: ignore[attr-defined]
    # Composer needs >1 candidate to fire; with only 2 strategies it builds a
    # composed solution which itself goes through EBBO first.  For this test we
    # set compose=False so the iteration over composable_solutions is exercised.
    orch = SolverOrchestrator(
        strategies=[s1, s2],
        per_strategy_timeout=1.0,
        run_all_strategies=True,
        compose=False,
        ebbo_multicall=object(),  # non-None enables _ebbo_validate
    )
    # Re-import the symbol the orchestrator's _ebbo_validate looks up at call-time.
    import src.solver.orchestrator
    src.solver.orchestrator.validate_solution_ebbo = fake_validate  # type: ignore[attr-defined]
    # The _ebbo_validate method imports validate_solution_ebbo INSIDE the
    # function body, so monkeypatch ebbo module-level instead.
    ebbo_mod.validate_solution_ebbo = fake_validate  # type: ignore[attr-defined]

    result, attempts = await orch.solve(auction)

    # Second candidate must have been chosen since first was EBBO-rejected.
    assert result is sol_second
    # Both candidates were EBBO-checked (rejection on first, pass on second).
    assert call_count["n"] == 2
    # A rejection AttemptRecord was logged for the first candidate.
    rejected = [a for a in attempts if a.strategy == "ebbo-rejected"]
    assert len(rejected) == 1


async def test_composer_receives_cip14_surplus_estimate(auction: Auction) -> None:
    """The orchestrator must compute CIP-14 surplus for each composer candidate.

    Regression for the edge composer's decimals-blind _estimate_surplus fallback:
    sum-of-raw-executed-amounts inflates WETH-emitting wins over higher-value
    USDC-emitting wins. Pre-computing surplus_estimate at orchestrator level
    bypasses that fallback.

    Injects a fake edge.matching module so the test runs even without the
    private submodule. Captures the candidates list compose() receives and
    asserts surplus_estimate is set per candidate (= compute_solution_score
    against auction.tokens.reference_price).
    """
    import sys
    import types
    from dataclasses import dataclass

    # Build two real-looking solutions that both fulfill the fixture's order
    # (1 WETH → 3450 USDC) at different clearing prices. The second has
    # bought=3500 USDC (matches reference), the first bought=3460 (small win).
    # We don't care about the exact scores — only that they're > 0 and ranked.
    base_uid = auction.orders[0].uid
    weth = auction.orders[0].sell_token.lower()
    usdc = auction.orders[0].buy_token.lower()
    sol_small = Solution(
        id=1,
        prices={weth: 3460_000000, usdc: 1_000000000000000000},
        trades=[Trade(kind="fulfillment", orderUid=base_uid,
                      executedAmount=10**18)],
        interactions=[],
    )
    sol_big = Solution(
        id=1,
        prices={weth: 3500_000000, usdc: 1_000000000000000000},
        trades=[Trade(kind="fulfillment", orderUid=base_uid,
                      executedAmount=10**18)],
        interactions=[],
    )

    s_a = AsyncMock(name="bipartite")
    s_a.name = "cow-matching-bipartite"
    s_a.solve.return_value = sol_small
    s_b = AsyncMock(name="router-v2")
    s_b.name = "router-v2"
    s_b.solve.return_value = sol_big

    # Fake edge.matching with a CandidateSolution dataclass + a compose() that
    # records what it was called with.
    @dataclass
    class FakeCandidateSolution:
        strategy: str
        solution: Solution
        surplus_estimate: int = 0

    captured: list[FakeCandidateSolution] = []

    def fake_compose(candidates, auction_id):  # noqa: ANN001
        captured.extend(candidates)
        return None  # falls through to first-winner path; we don't care

    fake_mod = types.ModuleType("edge.matching")
    fake_mod.CandidateSolution = FakeCandidateSolution  # type: ignore[attr-defined]
    fake_mod.compose = fake_compose  # type: ignore[attr-defined]
    fake_edge = types.ModuleType("edge")
    fake_edge.matching = fake_mod  # type: ignore[attr-defined]

    saved_modules = {k: sys.modules.get(k) for k in ("edge", "edge.matching")}
    sys.modules["edge"] = fake_edge
    sys.modules["edge.matching"] = fake_mod
    try:
        orch = SolverOrchestrator(
            strategies=[s_a, s_b],
            per_strategy_timeout=1.0,
            run_all_strategies=True,
            compose=True,
        )
        await orch.solve(auction)
    finally:
        for k, v in saved_modules.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    # Two candidates passed to compose, both with positive surplus_estimate.
    assert len(captured) == 2
    for c in captured:
        assert c.surplus_estimate > 0, (
            f"orchestrator must compute CIP-14 surplus for {c.strategy}; "
            f"composer would otherwise fall back to decimals-blind heuristic"
        )
    # The bigger-clearing-price solution must rank higher.
    by_strategy = {c.strategy: c.surplus_estimate for c in captured}
    assert by_strategy["router-v2"] > by_strategy["cow-matching-bipartite"], (
        "router-v2 clears at higher price → must rank above bipartite"
    )
