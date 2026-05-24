import asyncio
from unittest.mock import AsyncMock

import pytest

from src.models.auction import Auction, Token
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


# ── Existing behaviour ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_router_no_orders_returns_no_solution() -> None:
    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=False)
    auction = _make_auction([])
    result = await router.solve(auction)
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_router_returns_no_solution_when_no_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def mock_quote(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("src.solver.router.quote_best_path", mock_quote)
    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=False)
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
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=False)
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
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=False)
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
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=False)
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
    router = RouterSolver(
        multicall=multicall, intermediates=[], max_orders=3, v3_only_batched=False
    )
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
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=False)
    await router.solve(_make_auction(orders))

    assert call_count == 50


# ── Expected-surplus sort (with ETH-value fallback) ───────────────────────────

@pytest.mark.asyncio
async def test_order_cap_sorts_by_eth_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """With reference prices set, sort ranks by ETH-equivalent value, not raw amount.

    USDC has 6 decimals so a 1000-USDC order has sell_amount=1e9, while a
    1-WETH order has sell_amount=1e18. Raw-amount sort would always rank WETH
    higher. With reference prices (USDC≈2.5e14, WETH=1e18 wei per token unit),
    a 1 WETH order (~1 ETH) should outrank a 1000 USDC order (~0.25 ETH).
    """
    quoted_amounts: list[int] = []

    async def mock_quote(
        _mc: object, _si: object, _bi: object, amount_in: int, _ints: object
    ) -> None:
        quoted_amounts.append(amount_in)
        return None

    monkeypatch.setattr("src.solver.router.quote_best_path", mock_quote)

    weth = "0x" + "1" * 40
    usdc = "0x" + "2" * 40
    dai = "0x" + "3" * 40
    tokens = {
        weth: Token(decimals=18, referencePrice=10**18),       # 1 WETH = 1 ETH
        usdc: Token(decimals=6, referencePrice=25 * 10**13),   # 1 USDC ≈ 0.00025 ETH
        dai:  Token(decimals=18, referencePrice=10**18),       # buy-side, irrelevant
    }
    orders = [
        _make_order(uid="weth", sellToken=weth, buyToken=dai, sellAmount=10**18, buyAmount=1),
        _make_order(uid="usdc", sellToken=usdc, buyToken=dai, sellAmount=1000 * 10**6, buyAmount=1),
    ]
    multicall = AsyncMock()
    router = RouterSolver(
        multicall=multicall, intermediates=[], max_orders=1, v3_only_batched=False
    )
    await router.solve(_make_auction(orders, tokens=tokens))

    assert quoted_amounts == [10**18], (
        "max_orders=1 should pick the WETH order (≈1 ETH) over the USDC order (≈0.25 ETH)"
    )


@pytest.mark.asyncio
async def test_sort_prefers_bigger_absolute_surplus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When two orders are both ITM, the one with bigger absolute headroom
    (sell_value - buy_value) wins — even if its margin *percentage* is smaller.

    100 WETH × 1 % headroom → 1 ETH absolute surplus  (BIGGER)
      1 WETH × 50 % headroom → 0.5 ETH absolute surplus (SMALLER)

    This is economically correct: a tiny relative margin on a whale can
    still be the biggest real opportunity in the auction.
    """
    quoted_amounts: list[int] = []

    async def mock_quote(
        _mc: object, _si: object, _bi: object, amount_in: int, _ints: object
    ) -> None:
        quoted_amounts.append(amount_in)
        return None

    monkeypatch.setattr("src.solver.router.quote_best_path", mock_quote)

    weth = "0x" + "1" * 40
    weth2 = "0x" + "3" * 40  # second 1-ETH-priced token, decouple from DAI math
    tokens = {
        weth:  Token(decimals=18, referencePrice=10**18),
        weth2: Token(decimals=18, referencePrice=10**18),
    }
    # Big trade, 1 % capture: 100 WETH for 99 → +1 ETH absolute
    big_small_margin = _make_order(
        uid="big", sellToken=weth, buyToken=weth2,
        sellAmount=100 * 10**18, buyAmount=99 * 10**18,
    )
    # Small trade, 50 % capture: 1 WETH for 0.5 → +0.5 ETH absolute
    small_big_margin = _make_order(
        uid="small", sellToken=weth, buyToken=weth2,
        sellAmount=10**18, buyAmount=5 * 10**17,
    )
    multicall = AsyncMock()
    router = RouterSolver(
        multicall=multicall, intermediates=[], max_orders=1, v3_only_batched=False
    )
    await router.solve(_make_auction([big_small_margin, small_big_margin], tokens=tokens))
    assert quoted_amounts == [100 * 10**18], (
        f"expected big-absolute-surplus order quoted, got {quoted_amounts}"
    )


@pytest.mark.asyncio
async def test_sort_ranks_otm_orders_last(monkeypatch: pytest.MonkeyPatch) -> None:
    """A negative-margin (OTM at reference) order must NOT outrank an
    in-the-money one — even when its sell_amount dominates.

    OTM orders return surplus 0 (clamped) from the sort key so they pile
    up at the back; the router would lose any quote against them.
    """
    quoted_amounts: list[int] = []

    async def mock_quote(
        _mc: object, _si: object, _bi: object, amount_in: int, _ints: object
    ) -> None:
        quoted_amounts.append(amount_in)
        return None

    monkeypatch.setattr("src.solver.router.quote_best_path", mock_quote)

    weth = "0x" + "1" * 40
    weth2 = "0x" + "3" * 40
    tokens = {
        weth:  Token(decimals=18, referencePrice=10**18),
        weth2: Token(decimals=18, referencePrice=10**18),
    }
    # OTM: 100 WETH but limit demands 150 ETH-equiv back → can't be filled
    #      profitably; surplus key clamps to 0.
    otm = _make_order(
        uid="otm", sellToken=weth, buyToken=weth2,
        sellAmount=100 * 10**18, buyAmount=150 * 10**18,
    )
    # ITM: 1 WETH for 0.5 → +0.5 ETH absolute surplus.
    itm = _make_order(
        uid="itm", sellToken=weth, buyToken=weth2,
        sellAmount=10**18, buyAmount=5 * 10**17,
    )
    multicall = AsyncMock()
    router = RouterSolver(
        multicall=multicall, intermediates=[], max_orders=1, v3_only_batched=False
    )
    await router.solve(_make_auction([otm, itm], tokens=tokens))
    assert quoted_amounts == [10**18], (
        f"OTM order should NOT outrank ITM, got {quoted_amounts}"
    )


@pytest.mark.asyncio
async def test_order_cap_falls_back_to_sell_amount_when_no_reference_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When tokens lack reference prices, sort falls back to raw sell_amount."""
    quoted_amounts: list[int] = []

    async def mock_quote(
        _mc: object, _si: object, _bi: object, amount_in: int, _ints: object
    ) -> None:
        quoted_amounts.append(amount_in)
        return None

    monkeypatch.setattr("src.solver.router.quote_best_path", mock_quote)

    weth = "0x" + "1" * 40
    usdc = "0x" + "2" * 40
    # reference_price=None means fallback path
    tokens = {
        weth: Token(decimals=18, referencePrice=None),
        usdc: Token(decimals=6, referencePrice=None),
    }
    orders = [
        _make_order(uid="weth", sellToken=weth, sellAmount=10**18, buyAmount=1),
        _make_order(uid="usdc", sellToken=usdc, sellAmount=10**9, buyAmount=1),
    ]
    multicall = AsyncMock()
    router = RouterSolver(
        multicall=multicall, intermediates=[], max_orders=1, v3_only_batched=False
    )
    await router.solve(_make_auction(orders, tokens=tokens))

    assert quoted_amounts == [10**18], (
        "fallback should pick the larger raw sell_amount (10^18 > 10^9)"
    )


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
    router = RouterSolver(
        multicall=multicall, intermediates=[], max_concurrent=10, v3_only_batched=False
    )

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
    router = RouterSolver(
        multicall=multicall, intermediates=[], max_concurrent=3, v3_only_batched=False
    )

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
    assert s.router_max_orders == 9
    assert s.router_max_concurrent == 3
    assert s.router_strategy_timeout == 11.0
    # Router uses WETH-only intermediates to stay within Alchemy free-tier rate limit
    assert len(s.router_intermediate_tokens) == 1
    assert s.router_intermediate_tokens[0].lower() == "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"


def test_router_exposes_timeout_attribute() -> None:
    """RouterSolver must declare .timeout so the orchestrator can use it."""
    from unittest.mock import MagicMock
    multicall = MagicMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=False)
    assert hasattr(router, "timeout")
    assert router.timeout == 11.0


def test_router_timeout_overridable() -> None:
    """Custom strategy_timeout is stored on the instance."""
    from unittest.mock import MagicMock
    multicall = MagicMock()
    router = RouterSolver(multicall=multicall, intermediates=[], strategy_timeout=12.5)
    assert router.timeout == 12.5


# ── V3-only batched mode ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_v3_batched_mode_emits_trade_when_amount_out_beats_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.routing.v3_batched import V3BatchedQuote, V3Path

    async def mock_batched(
        _mc: object, paths: list[V3Path], **_: object
    ) -> list[V3BatchedQuote]:
        # Return amount_out=1100 (> buy_amount=900) on the FIRST path; zeros otherwise.
        out = []
        for i, p in enumerate(paths):
            out.append(V3BatchedQuote(path=p, amount_out=1100 if i == 0 else 0))
        return out

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=True)
    result = await router.solve(_make_auction([_make_order()], auction_id="7"))
    assert isinstance(result, Solution)
    assert len(result.trades) == 1
    assert result.trades[0].order_uid == "o1"


@pytest.mark.asyncio
async def test_router_v3_batched_mode_skips_orders_below_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.routing.v3_batched import V3BatchedQuote, V3Path

    async def mock_batched(
        _mc: object, paths: list[V3Path], **_: object
    ) -> list[V3BatchedQuote]:
        return [V3BatchedQuote(path=p, amount_out=800) for p in paths]

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=True)
    # buy_amount=900, all quotes return 800 → no trade
    result = await router.solve(_make_auction([_make_order()]))
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_router_falls_back_to_legacy_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag off → legacy `quote_best_path` path is used (not batched_v3_quote)."""
    from src.routing.multihop import HopQuote

    legacy_called = False
    batched_called = False

    async def mock_legacy(*args: object, **kwargs: object) -> list[HopQuote]:
        nonlocal legacy_called
        legacy_called = True
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

    async def mock_batched(*args: object, **kwargs: object) -> list[object]:
        nonlocal batched_called
        batched_called = True
        return []

    monkeypatch.setattr("src.solver.router.quote_best_path", mock_legacy)
    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=False)
    await router.solve(_make_auction([_make_order()]))
    assert legacy_called
    assert not batched_called


def test_config_has_v3_only_batched_default() -> None:
    from src.config import Settings
    s = Settings()
    assert s.router_v3_only_batched is True
