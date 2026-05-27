"""Phase 4 — end-to-end integration tests for the partial-fill path.

These tests exercise RouterSolver (Phase 3's partial-quote-search path)
end-to-end through a mocked AMM, proving the partial-fill wiring is correct
without requiring a real Multicall3 or the edge submodule.

Bipartite / multi-party integration tests are intentionally NOT included here:
those strategies depend on the `edge` submodule (LP solver + ring LP) which
requires a real edge install. A separate integration suite should cover them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.models.auction import Auction, Token
from src.models.order import Order
from src.solver.base import NoSolution  # noqa: F401 (used via isinstance in tests)

# ── Helpers (mirrors test_router.py so this file is self-contained) ──────────


def _make_order(**kwargs: object) -> Order:
    defaults: dict[str, object] = {
        "uid": "o1",
        "sellToken": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "buyToken": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "sellAmount": 1000,
        "buyAmount": 900,
        "feePolicies": [],
        "validTo": 99,
        "kind": "sell",
        "owner": "0x" + "a" * 40,
        "partiallyFillable": False,
        "class": "limit",
    }
    defaults.update(kwargs)
    return Order(**defaults)  # type: ignore[arg-type]


def _make_auction(
    orders: list[Order],
    auction_id: str = "1",
    tokens: dict[str, Token] | None = None,
) -> Auction:
    return Auction(
        id=auction_id,
        tokens=tokens or {},
        orders=orders,
        liquidity=[],
        effectiveGasPrice=0,
        deadline=None,
    )


# ── Integration tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_partial_fillable_sell_emits_partial_solution_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a partiallyFillable=True sell order whose full quote misses
    limit emits a Solution with executedAmount < sellAmount via the Phase 3
    partial-quote-search path.

    Scenario:
      - sell 1000 X for ≥900 Y, partial=True
      - Mock V3 quotes: full(1000)→800 miss, 0.75x(750)→700 clears (≥675),
        0.5x(500)→460 clears (≥450)
      - Expected Solution: 1 Trade with executed_amount=750 (NOT 1000)
    """
    from src.models.solution import Solution
    from src.routing.v3_batched import V3BatchedQuote, V3Path
    from src.solver.router import RouterSolver

    async def mock_batched(
        _mc: object, paths: list[V3Path], **_: object
    ) -> list[V3BatchedQuote]:
        # Simulate AMM: map each amount_in to its expected output; unknown → 0.
        out = []
        for p in paths:
            if p.exact_output:
                # Buy orders: defensive; not exercised in this test.
                out.append(V3BatchedQuote(path=p, amount_out=0))
            elif p.amount_in == 1000:
                out.append(V3BatchedQuote(path=p, amount_out=800))   # full: MISS
            elif p.amount_in == 750:
                out.append(V3BatchedQuote(path=p, amount_out=700))   # 0.75x: clears ≥675
            elif p.amount_in == 500:
                out.append(V3BatchedQuote(path=p, amount_out=460))   # 0.5x: clears ≥450
            else:
                out.append(V3BatchedQuote(path=p, amount_out=0))
        return out

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=True)

    order = _make_order(
        uid="pf_e2e",
        sellAmount=1000,
        buyAmount=900,
        partiallyFillable=True,
    )
    auction = _make_auction([order], auction_id="101")

    result = await router.solve(auction)

    assert isinstance(result, Solution), (
        f"expected Solution, got {type(result).__name__}"
    )
    assert len(result.trades) == 1, (
        f"expected exactly 1 trade, got {len(result.trades)}"
    )
    t = result.trades[0]
    assert t.order_uid == "pf_e2e"
    assert t.executed_amount == 750, (
        f"expected 0.75x fill (750), got {t.executed_amount}"
    )
    assert t.executed_amount < 1000, "must be a partial fill, not full"


@pytest.mark.asyncio
async def test_non_partial_sell_full_miss_emits_no_solution_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a sell order with partially_fillable=False whose full quote
    misses limit must emit NoSolution. Phase 3's partial-quote search must
    NOT trigger for non-partial orders (regression guard).

    Uses the same AMM mock as the positive test — the only difference is the
    order's partiallyFillable flag.  This ensures any future refactor that
    accidentally enables partial-quote-search for all orders is caught
    immediately.
    """
    from src.routing.v3_batched import V3BatchedQuote, V3Path
    from src.solver.router import RouterSolver

    async def mock_batched(
        _mc: object, paths: list[V3Path], **_: object
    ) -> list[V3BatchedQuote]:
        # Same AMM mock as the positive test.
        out = []
        for p in paths:
            if p.exact_output:
                out.append(V3BatchedQuote(path=p, amount_out=0))
            elif p.amount_in == 1000:
                out.append(V3BatchedQuote(path=p, amount_out=800))   # full: MISS
            elif p.amount_in == 750:
                out.append(V3BatchedQuote(path=p, amount_out=700))   # would clear if partial
            elif p.amount_in == 500:
                out.append(V3BatchedQuote(path=p, amount_out=460))   # would clear if partial
            else:
                out.append(V3BatchedQuote(path=p, amount_out=0))
        return out

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=True)

    # Identical scenario but partiallyFillable=False
    order = _make_order(
        uid="np_e2e",
        sellAmount=1000,
        buyAmount=900,
        partiallyFillable=False,
    )
    auction = _make_auction([order], auction_id="102")

    result = await router.solve(auction)

    assert isinstance(result, NoSolution), (
        f"non-partial order with full-miss must yield NoSolution, got {type(result).__name__}"
    )
