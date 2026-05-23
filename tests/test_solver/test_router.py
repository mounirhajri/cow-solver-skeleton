import asyncio
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


# ── Existing behaviour ────────────────────────────────────────────────────────

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


# ── Order cap ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_order_cap_limits_quotes_to_top_n(monkeypatch: pytest.MonkeyPatch) -> None:
    """With max_orders=3, only the 3 largest sell_amount orders are quoted."""
    quoted_amounts: list[int] = []

    async def mock_quote(
        _mc: object, _si: object, _bi: object, amount_in: int, _ints: object
    ) -> None:
        quoted_amounts.append(amount_in)
        return None

    monkeypatch.setattr("src.solver.router.quote_best_path", mock_quote)

    orders = [
        _make_order(uid=f"o{i}", sellAmount=i * 100, buyAmount=1)
        for i in range(1, 8)  # sell amounts: 100, 200, ..., 700
    ]
    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], max_orders=3)
    await router.solve(_make_auction(orders))

    assert sorted(quoted_amounts, reverse=True) == [700, 600, 500], (
        "should quote only the 3 largest orders"
    )


@pytest.mark.asyncio
async def test_order_cap_default_is_50(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default max_orders=50: 150 orders → only 50 quoted."""
    call_count = 0

    async def mock_quote(*args: object, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        return None

    monkeypatch.setattr("src.solver.router.quote_best_path", mock_quote)

    orders = [
        _make_order(uid=f"o{i}", sellAmount=i)
        for i in range(1, 151)  # 150 orders
    ]
    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[])
    await router.solve(_make_auction(orders))

    assert call_count == 50


# ── Parallelism ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quotes_run_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    """With 10 orders and max_concurrent=10, all quotes start before any finishes."""
    started: list[int] = []
    finished: list[int] = []
    barrier = asyncio.Event()

    async def mock_quote(
        _mc: object, _si: object, _bi: object, amount_in: int, _ints: object
    ) -> None:
        started.append(amount_in)
        await barrier.wait()  # all coroutines park here
        finished.append(amount_in)
        return None

    monkeypatch.setattr("src.solver.router.quote_best_path", mock_quote)

    orders = [_make_order(uid=f"o{i}", sellAmount=i * 10) for i in range(1, 11)]
    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], max_concurrent=10)

    solve_task = asyncio.create_task(router.solve(_make_auction(orders)))

    # Yield control so coroutines can reach the barrier
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # All 10 should have started before any finishes
    assert len(started) == 10, f"expected 10 started, got {len(started)}"
    assert len(finished) == 0, "none should have finished yet (barrier not released)"

    barrier.set()
    await solve_task


@pytest.mark.asyncio
async def test_semaphore_limits_concurrent_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    """With max_concurrent=3, at most 3 quotes are in-flight at once."""
    in_flight = 0
    max_in_flight_seen = 0
    gate = asyncio.Event()

    async def mock_quote(
        _mc: object, _si: object, _bi: object, amount_in: int, _ints: object
    ) -> None:
        nonlocal in_flight, max_in_flight_seen
        in_flight += 1
        max_in_flight_seen = max(max_in_flight_seen, in_flight)
        await gate.wait()
        in_flight -= 1
        return None

    monkeypatch.setattr("src.solver.router.quote_best_path", mock_quote)

    orders = [_make_order(uid=f"o{i}", sellAmount=i * 10) for i in range(1, 11)]
    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], max_concurrent=3)

    solve_task = asyncio.create_task(router.solve(_make_auction(orders)))

    # Let the first wave (semaphore=3) start
    for _ in range(5):
        await asyncio.sleep(0)

    assert max_in_flight_seen <= 3, (
        f"semaphore violated: {max_in_flight_seen} quotes ran simultaneously"
    )

    gate.set()
    await solve_task


# ── Config integration ────────────────────────────────────────────────────────

def test_config_has_router_settings() -> None:
    from src.config import Settings
    s = Settings()
    assert s.router_max_orders == 50
    assert s.router_max_concurrent == 20
    assert s.router_strategy_timeout == 9.0


def test_router_exposes_timeout_attribute() -> None:
    """RouterSolver must declare .timeout so the orchestrator can use it."""
    from unittest.mock import MagicMock
    multicall = MagicMock()
    router = RouterSolver(multicall=multicall, intermediates=[])
    assert hasattr(router, "timeout")
    assert router.timeout == 9.0


def test_router_timeout_overridable() -> None:
    """Custom strategy_timeout is stored on the instance."""
    from unittest.mock import MagicMock
    multicall = MagicMock()
    router = RouterSolver(multicall=multicall, intermediates=[], strategy_timeout=12.5)
    assert router.timeout == 12.5
