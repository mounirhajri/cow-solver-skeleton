from unittest.mock import AsyncMock

import pytest

from src.models.auction import Auction
from src.models.order import Order
from src.models.solution import Solution
from src.solver.base import NoSolution
from src.solver.router import RouterSolver


def _make_order(**kwargs: object) -> Order:
    defaults: dict[str, object] = {
        "uid": "o1",
        "sellToken": "0xa",
        "buyToken": "0xb",
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


def _make_auction(orders: list[Order], auction_id: str = "1") -> Auction:
    return Auction(
        id=auction_id,
        tokens={},
        orders=orders,
        liquidity=[],
        effectiveGasPrice=0,
        deadline=None,
    )


@pytest.mark.asyncio
async def test_router_no_orders_returns_no_solution() -> None:
    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[])
    auction = _make_auction([])
    result = await router.solve(auction)
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_router_returns_no_solution_when_no_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def mock_quote(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("src.solver.router.quote_best_path", mock_quote)
    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[])
    auction = _make_auction([_make_order()])
    result = await router.solve(auction)
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_router_emits_trade_when_path_beats_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.routing.multihop import HopQuote

    async def mock_quote(*args: object, **kwargs: object) -> list[HopQuote]:
        return [
            HopQuote(
                factory="sushi",
                pool="0x" + "0" * 40,
                token_in="0xa",
                token_out="0xb",
                amount_in=1000,
                amount_out=1100,
            )
        ]

    monkeypatch.setattr("src.solver.router.quote_best_path", mock_quote)

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[])
    auction = _make_auction([_make_order()], auction_id="42")
    result = await router.solve(auction)
    assert isinstance(result, Solution)
    assert len(result.trades) == 1
    assert result.trades[0].order_uid == "o1"


@pytest.mark.asyncio
async def test_router_skips_order_below_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.routing.multihop import HopQuote

    async def mock_quote(*args: object, **kwargs: object) -> list[HopQuote]:
        return [
            HopQuote(
                factory="sushi",
                pool="0x" + "0" * 40,
                token_in="0xa",
                token_out="0xb",
                amount_in=1000,
                amount_out=800,  # below buy_amount=900
            )
        ]

    monkeypatch.setattr("src.solver.router.quote_best_path", mock_quote)

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[])
    auction = _make_auction([_make_order()])
    result = await router.solve(auction)
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_router_skips_buy_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    async def mock_quote(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True
        return None

    monkeypatch.setattr("src.solver.router.quote_best_path", mock_quote)

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[])
    auction = _make_auction([_make_order(kind="buy")])
    result = await router.solve(auction)
    assert isinstance(result, NoSolution)
    assert not called
