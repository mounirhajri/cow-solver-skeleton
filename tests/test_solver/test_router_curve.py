"""Curve wiring integration tests: Curve quotes join the V3/V4 selection pool;
a winning CurvePath encodes [approve(pool), exchange] targeting the pool."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.models.auction import Auction
from src.models.order import Order
from src.models.solution import Solution
from src.routing.curve_quoter import CURVE_POOLS, CurvePath, CurveQuote
from src.routing.v3_batched import V3BatchedQuote, V3Path
from src.solver.router import RouterSolver

POOL = CURVE_POOLS[0]
USDCE, USDT = POOL.coins[0], POOL.coins[1]


def _make_order(**kwargs: object) -> Order:
    defaults: dict[str, object] = {
        "uid": "o1",
        "sellToken": USDT,
        "buyToken": USDCE,
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


def _make_auction(orders: list[Order]) -> Auction:
    return Auction(
        id="1", tokens={}, orders=orders, liquidity=[],
        effectiveGasPrice=0, deadline=None,
    )


def _mock_v3(amount_out: int):
    async def mock(_mc: object, paths: list[V3Path], **_: object) -> list[V3BatchedQuote]:
        return [V3BatchedQuote(path=p, amount_out=amount_out) for p in paths]
    return mock


def _mock_curve(amount_out: int):
    async def mock(_mc: object, paths: list[CurvePath], **_: object) -> list[CurveQuote]:
        return [CurveQuote(path=p, amount_out=amount_out) for p in paths]
    return mock


def _router(**kw: object) -> RouterSolver:
    return RouterSolver(
        multicall=AsyncMock(), intermediates=[],
        v3_only_batched=True, order_validity_filter=False,
        v4_enabled=False, v4_native_probe=False, curve_enabled=True,
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_curve_wins_when_quote_is_better(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.solver.router.batched_v3_quote", _mock_v3(950))
    monkeypatch.setattr("src.solver.router.batched_curve_quote", _mock_curve(990))

    result = await _router().solve(_make_auction([_make_order()]))

    assert isinstance(result, Solution)
    assert len(result.interactions) == 2  # approve(pool) + exchange
    targets = [str(ix["target"]).lower() for ix in result.interactions]
    assert POOL.address.lower() in targets  # the exchange call
    assert USDT.lower() in targets  # the approve targets the sell token


@pytest.mark.asyncio
async def test_v3_wins_over_curve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.solver.router.batched_v3_quote", _mock_v3(990))
    monkeypatch.setattr("src.solver.router.batched_curve_quote", _mock_curve(960))

    result = await _router().solve(_make_auction([_make_order()]))

    assert isinstance(result, Solution)
    targets = [str(ix["target"]).lower() for ix in result.interactions]
    assert POOL.address.lower() not in targets


@pytest.mark.asyncio
async def test_curve_disabled_not_called(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.solver.router.batched_v3_quote", _mock_v3(950))
    spy = AsyncMock(return_value=[])
    monkeypatch.setattr("src.solver.router.batched_curve_quote", spy)

    router = RouterSolver(
        multicall=AsyncMock(), intermediates=[],
        v3_only_batched=True, order_validity_filter=False,
        v4_enabled=False, v4_native_probe=False, curve_enabled=False,
    )
    result = await router.solve(_make_auction([_make_order()]))
    assert isinstance(result, Solution)
    spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_curve_failure_fails_open_to_v3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.solver.router.batched_v3_quote", _mock_v3(950))

    async def boom(*_: object, **__: object) -> list[CurveQuote]:
        raise RuntimeError("RPC error 429")

    monkeypatch.setattr("src.solver.router.batched_curve_quote", boom)

    result = await _router().solve(_make_auction([_make_order()]))
    assert isinstance(result, Solution)  # V3 result survives


@pytest.mark.asyncio
async def test_non_curve_pair_builds_no_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.solver.router.batched_v3_quote", _mock_v3(950))
    spy = AsyncMock(return_value=[])
    monkeypatch.setattr("src.solver.router.batched_curve_quote", spy)

    exotic = _make_order(
        sellToken="0x" + "aa" * 20, buyToken="0x" + "bb" * 20
    )
    result = await _router().solve(_make_auction([exotic]))
    assert isinstance(result, Solution)
    spy.assert_not_awaited()  # no curve pool for the pair → no call
