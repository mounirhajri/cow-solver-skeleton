"""V4 wiring integration tests.

V4 quotes join the V3 selection pool (best amount_out per order wins) and the
winning path's venue picks the encoder: V3Path → [approve, SwapRouter swap],
V4Path → [erc20→Permit2 approve, Permit2→UR approve, UR.execute(V4_SWAP)].
These tests prove the wiring through mocked quote functions — no network, no
real Multicall3.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.models.auction import Auction, Token
from src.models.order import Order
from src.models.solution import Solution
from src.routing.v3_batched import V3BatchedQuote, V3Path
from src.routing.v4_quoter import V4BatchedQuote, V4Path
from src.solver.base import NoSolution
from src.solver.router import RouterSolver

UR_ARBITRUM = "0xA51afAFe0263b40EdaEf0Df8781eA9aa03E381a3"

# ── Helpers (mirrors test_partial_fills_integration.py) ──────────────────────


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


def _mock_v3(amount_out_by_amount_in: dict[int, int]):
    """V3 quote mock: every path gets the amount_out mapped from its
    amount_in (0 when unmapped) — fee-tier agnostic, selection takes max."""

    async def mock(_mc: object, paths: list[V3Path], **_: object) -> list[V3BatchedQuote]:
        return [
            V3BatchedQuote(path=p, amount_out=amount_out_by_amount_in.get(p.amount_in, 0))
            for p in paths
        ]

    return mock


def _mock_v4(amount_out_by_amount_in: dict[int, int]):
    async def mock(_mc: object, paths: list[V4Path], **_: object) -> list[V4BatchedQuote]:
        return [
            V4BatchedQuote(
                path=p,
                amount_out=amount_out_by_amount_in.get(p.amount_in, 0),
                gas_estimate=21_000,
            )
            for p in paths
        ]

    return mock


def _router() -> RouterSolver:
    # order_validity_filter=False disables both the candidate pre-filter and
    # the post-solve funding gate (same flag) so no Multicall is needed.
    return RouterSolver(
        multicall=AsyncMock(),
        intermediates=[],
        v3_only_batched=True,
        order_validity_filter=False,
        v4_enabled=True,
    )


def _ur_interactions(solution: Solution) -> list[dict[str, object]]:
    return [
        ix
        for ix in solution.interactions
        if str(ix.get("target", "")).lower() == UR_ARBITRUM.lower()
    ]


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v4_wins_when_quote_is_better(monkeypatch: pytest.MonkeyPatch) -> None:
    """V4 quotes 980 vs V3's 950 → the V4 path wins and the solution carries
    the 3-interaction Universal-Router encoding."""
    monkeypatch.setattr("src.solver.router.batched_v3_quote", _mock_v3({1000: 950}))
    monkeypatch.setattr("src.solver.router.batched_v4_quote", _mock_v4({1000: 980}))

    result = await _router().solve(_make_auction([_make_order()]))

    assert isinstance(result, Solution)
    assert len(result.trades) == 1
    assert result.trades[0].executed_amount == 1000
    assert len(result.interactions) == 3  # approve(Permit2), Permit2.approve, UR.execute
    assert len(_ur_interactions(result)) == 1


@pytest.mark.asyncio
async def test_v3_wins_when_quote_is_better(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.solver.router.batched_v3_quote", _mock_v3({1000: 990}))
    monkeypatch.setattr("src.solver.router.batched_v4_quote", _mock_v4({1000: 980}))

    result = await _router().solve(_make_auction([_make_order()]))

    assert isinstance(result, Solution)
    assert len(result.interactions) == 2  # approve + SwapRouter swap
    assert _ur_interactions(result) == []


@pytest.mark.asyncio
async def test_v4_disabled_skips_v4_quoting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.solver.router.batched_v3_quote", _mock_v3({1000: 950}))
    v4_spy = AsyncMock()
    monkeypatch.setattr("src.solver.router.batched_v4_quote", v4_spy)

    router = RouterSolver(
        multicall=AsyncMock(),
        intermediates=[],
        v3_only_batched=True,
        order_validity_filter=False,
        v4_enabled=False,
    )
    result = await router.solve(_make_auction([_make_order()]))

    assert isinstance(result, Solution)
    v4_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_v4_quoting_failure_fails_open_to_v3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A V4 quoting error must never cost us the V3 result."""
    monkeypatch.setattr("src.solver.router.batched_v3_quote", _mock_v3({1000: 950}))

    async def boom(*_: object, **__: object) -> list[V4BatchedQuote]:
        raise RuntimeError("RPC error 429")

    monkeypatch.setattr("src.solver.router.batched_v4_quote", boom)

    result = await _router().solve(_make_auction([_make_order()]))

    assert isinstance(result, Solution)
    assert len(result.interactions) == 2
    assert _ur_interactions(result) == []


@pytest.mark.asyncio
async def test_buy_orders_build_no_v4_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Buy orders are V3-only (MVP scope): the V4 quoter is not even called
    for a buy-only auction."""
    # Buy order: exact-output quote returns amount_in (sell side) — 500 ≤ 1000.
    monkeypatch.setattr("src.solver.router.batched_v3_quote", _mock_v3({900: 500}))
    v4_spy = AsyncMock(return_value=[])
    monkeypatch.setattr("src.solver.router.batched_v4_quote", v4_spy)

    result = await _router().solve(_make_auction([_make_order(kind="buy")]))

    assert isinstance(result, Solution)
    v4_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_v4_wins_partial_fraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partial sell: full amount misses limit on both venues, V4 clears the
    0.75x fraction → partial fill routed through the Universal Router."""
    # sell 1000 for ≥900, partial → fractions 750 (limit 675) and 500 (450).
    monkeypatch.setattr(
        "src.solver.router.batched_v3_quote",
        _mock_v3({1000: 800, 750: 0, 500: 0}),  # full misses, fractions no pool
    )
    monkeypatch.setattr(
        "src.solver.router.batched_v4_quote",
        _mock_v4({1000: 850, 750: 700, 500: 0}),  # full misses, 0.75x clears ≥675
    )

    result = await _router().solve(_make_auction([_make_order(partiallyFillable=True)]))

    assert isinstance(result, Solution)
    assert len(result.trades) == 1
    assert result.trades[0].executed_amount == 750
    assert len(_ur_interactions(result)) == 1


@pytest.mark.asyncio
async def test_no_quotes_anywhere_is_no_solution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.solver.router.batched_v3_quote", _mock_v3({}))
    monkeypatch.setattr("src.solver.router.batched_v4_quote", _mock_v4({}))

    result = await _router().solve(_make_auction([_make_order()]))

    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_v4_quote_above_uint128_is_filtered_v3_ships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A V4 quote whose amount_out exceeds uint128 must be dropped BEFORE
    selection — otherwise encode-time bounds errors would abort the whole
    strategy and cost the V3 trades too (reviewer finding I1)."""
    monkeypatch.setattr("src.solver.router.batched_v3_quote", _mock_v3({1000: 950}))
    monkeypatch.setattr(
        "src.solver.router.batched_v4_quote", _mock_v4({1000: 2**128})
    )

    result = await _router().solve(_make_auction([_make_order()]))

    assert isinstance(result, Solution)
    assert len(result.interactions) == 2  # V3 encoding — V4 quote filtered
    assert _ur_interactions(result) == []


@pytest.mark.asyncio
async def test_huge_sell_amount_builds_no_v4_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An order with amount_in >= 2^128 must not poison V4 call building for
    the auction (reviewer finding I2): no V4 paths for it, V3 still quotes."""
    monkeypatch.setattr(
        "src.solver.router.batched_v3_quote", _mock_v3({2**130: 2**131})
    )
    v4_spy = AsyncMock(return_value=[])
    monkeypatch.setattr("src.solver.router.batched_v4_quote", v4_spy)

    huge = _make_order(sellAmount=2**130, buyAmount=2**129)
    result = await _router().solve(_make_auction([huge]))

    assert isinstance(result, Solution)  # V3 fills it
    v4_spy.assert_not_awaited()  # no V4 paths were built at all


@pytest.mark.asyncio
async def test_v4_declared_output_is_promised_executed_buy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driver solvency accounting: the UR interaction must DECLARE the
    promised executed_buy, not the slippage-reduced on-chain minimum
    (reviewer finding C1 — the 2026-06-08 non-conformance failure mode)."""
    monkeypatch.setattr("src.solver.router.batched_v3_quote", _mock_v3({1000: 0}))
    monkeypatch.setattr("src.solver.router.batched_v4_quote", _mock_v4({1000: 980}))

    result = await _router().solve(_make_auction([_make_order()]))

    assert isinstance(result, Solution)
    (ur,) = _ur_interactions(result)
    outputs = ur.get("outputs")
    assert outputs is not None
    (out,) = outputs  # type: ignore[misc]
    assert int(out["amount"]) == 980  # the promise — NOT 980 minus slippage
