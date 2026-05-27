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
                token_in="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                token_out="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
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
                token_in="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                token_out="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
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


@pytest.mark.asyncio
async def test_legacy_path_logs_warning_when_dropping_buys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §2: when v3_only_batched=False (escape hatch / RPC fallback),
    quote_best_path is exact-input only, so buy orders are silently dropped.
    Without the warning, an operator can't tell the gap exists in shadow
    logs — they'd just see lower router-v2 win counts with no explanation.

    Captures the structlog warning by monkeypatching the module logger's
    .warning method (structlog → PrintLoggerFactory writes directly to
    stdout, bypassing stdlib caplog).
    """
    warnings: list[tuple[str, dict[str, object]]] = []

    async def mock_quote(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("src.solver.router.quote_best_path", mock_quote)
    monkeypatch.setattr(
        "src.solver.router.log.warning",
        lambda event, **kw: warnings.append((event, kw)),
    )

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=False)
    # 2 buys + 1 sell — we should see n_dropped=2 in the warning.
    auction = _make_auction([
        _make_order(uid="b1", kind="buy"),
        _make_order(uid="b2", kind="buy"),
        _make_order(uid="s1", kind="sell"),
    ])
    await router.solve(auction)

    matches = [(ev, kw) for ev, kw in warnings if ev == "router_legacy_path_skips_buys"]
    assert len(matches) == 1, f"expected exactly 1 skip-warning, got {len(matches)}"
    assert matches[0][1]["n_dropped"] == 2, (
        f"expected n_dropped=2, got {matches[0][1]['n_dropped']}"
    )


@pytest.mark.asyncio
async def test_legacy_path_no_warning_when_no_buys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sell-only auction in legacy mode must NOT emit the warning (n=0)."""
    warnings: list[str] = []

    async def mock_quote(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("src.solver.router.quote_best_path", mock_quote)
    monkeypatch.setattr(
        "src.solver.router.log.warning",
        lambda event, **kw: warnings.append(event),
    )

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=False)
    await router.solve(_make_auction([_make_order(kind="sell")]))

    assert "router_legacy_path_skips_buys" not in warnings, (
        f"sell-only auction must not log the skip warning; got {warnings}"
    )


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
                token_in="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                token_out="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
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


# ── Buy-order support (V3-batched only) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_router_emits_buy_order_with_quoteexactoutput(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Buy order: quoter returns amount_in=900 ≤ sell_amount=1000 → emit trade
    with executedAmount=buy_amount and clearing-price ratio matching the AMM.

    Verifies (a) buy orders survive solve()'s sort/cap, (b) exact_output is
    set on emitted paths, (c) executedAmount equals buy_amount per CoW
    convention (matches src.shadow.scoring._score_buy_trade which reads
    executed as the buy-side exact amount).
    """
    from src.routing.v3_batched import V3BatchedQuote, V3Path

    received_paths: list[V3Path] = []

    async def mock_batched(
        _mc: object, paths: list[V3Path], **_: object
    ) -> list[V3BatchedQuote]:
        received_paths.extend(paths)
        # amount_out field carries amountIn for exact_output paths;
        # return 900 (less than sell_amount=1000) on the first one.
        return [
            V3BatchedQuote(path=p, amount_out=900 if i == 0 else 0)
            for i, p in enumerate(paths)
        ]

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=True)
    order = _make_order(kind="buy", sellAmount=1000, buyAmount=500)
    result = await router.solve(_make_auction([order], auction_id="42"))

    assert all(p.exact_output for p in received_paths), (
        "buy order paths must carry exact_output=True"
    )
    assert all(p.amount_in == 500 for p in received_paths), (
        "buy order paths must encode buy_amount as the exact amount"
    )
    assert isinstance(result, Solution)
    assert len(result.trades) == 1
    assert result.trades[0].order_uid == "o1"
    assert result.trades[0].executed_amount == 500, (
        "executedAmount for buy orders is buy_amount (the exact side)"
    )
    # Fallback clearing prices (no reference prices in this auction) preserve
    # the AMM's executed ratio: cp_sell/cp_buy = buy_amount/amount_in.
    assert result.prices["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"] == 500  # buy_amount as price of sell-token
    assert result.prices["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"] == 900  # amount_in as price of buy-token


@pytest.mark.asyncio
async def test_router_skips_buy_when_amount_in_exceeds_sell_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Buy order: quoter says we need 1100 sell-token but user only signed
    away 1000 → no trade emitted (limit violated)."""
    from src.routing.v3_batched import V3BatchedQuote, V3Path

    async def mock_batched(
        _mc: object, paths: list[V3Path], **_: object
    ) -> list[V3BatchedQuote]:
        return [V3BatchedQuote(path=p, amount_out=1100) for p in paths]

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=True)
    order = _make_order(kind="buy", sellAmount=1000, buyAmount=500)
    result = await router.solve(_make_auction([order]))
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_buy_and_sell_mixed_auction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mixed auction: 1 sell + 1 buy, both quoteable, both emitted with the
    correct executedAmount semantic per kind."""
    from src.routing.v3_batched import V3BatchedQuote, V3Path

    async def mock_batched(
        _mc: object, paths: list[V3Path], **_: object
    ) -> list[V3BatchedQuote]:
        # Sell paths (exact_output=False): return 1100 (> buy_amount=900) ⇒ fillable.
        # Buy paths (exact_output=True):   return 800  (< sell_amount=1000) ⇒ fillable.
        out: list[V3BatchedQuote] = []
        for p in paths:
            amt = 800 if p.exact_output else 1100
            out.append(V3BatchedQuote(path=p, amount_out=amt))
        return out

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    sell = _make_order(uid="s1", kind="sell", sellAmount=1000, buyAmount=900)
    buy = _make_order(uid="b1", kind="buy", sellAmount=1000, buyAmount=500)

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=True)
    result = await router.solve(_make_auction([sell, buy]))

    assert isinstance(result, Solution)
    assert len(result.trades) == 2
    by_uid = {t.order_uid: t for t in result.trades}
    assert by_uid["s1"].executed_amount == 1000  # sell: sellAmount (exact)
    assert by_uid["b1"].executed_amount == 500   # buy:  buyAmount  (exact)


@pytest.mark.asyncio
async def test_clearing_prices_track_amm_ratio_not_oracle_for_otm_buy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the phantom-surplus bug observed 2026-05-24.

    A partiallyFillable BUY order signs willingness to pay 93,262 USDC for
    1 WBTC.  Oracle reference price implies WBTC = 76,240 USDC at market.
    The AMM actually delivers 1 WBTC for ~80,000 USDC (above oracle once
    slippage on a single-trade 1-WBTC swap is included).

    Before the fix: clearing prices were set to reference prices when
    available → CIP-14 scoring computed surplus as
    (signed_sell − executed × oracle_ratio) = order's OTM headroom at
    oracle (~17,000 USDC = ~6.6 ETH), regardless of the AMM's real rate.
    134 such fills in 24 h drove an €67M/Mo phantom projection.

    After the fix: clearing prices track the AMM execution ratio so
    surplus = (signed_sell − amm_amount_in) is what the user really
    captures, bounded by 13,262 USDC instead of the phantom 17,022 USDC.

    Asserts the end-to-end score via compute_solution_score — assertions
    on result.prices alone could be satisfied by a future regression that
    routes oracle-derived prices through a different transform. The score
    is the ground truth and must reflect realised surplus only.
    """
    from src.routing.v3_batched import V3BatchedQuote, V3Path
    from src.shadow.scoring import compute_solution_score

    sell_token = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"  # USDC.e (6 dec)
    buy_token = "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f"   # WBTC   (8 dec)

    amm_amount_in = 80_000 * 10**6  # 80k USDC the AMM charges for 1 WBTC

    async def mock_batched(
        _mc: object, paths: list[V3Path], **_: object
    ) -> list[V3BatchedQuote]:
        return [V3BatchedQuote(path=p, amount_out=amm_amount_in) for p in paths]

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    # Oracle reference prices implying 76,240 USDC per WBTC at market.
    usdc_ref = 476_878_487_995_865_217_881_866_240
    wbtc_ref = 363_471_992_112_697_727_091_939_999_744
    tokens = {
        sell_token: Token(decimals=6, reference_price=usdc_ref),
        buy_token: Token(decimals=8, reference_price=wbtc_ref),
    }

    order = _make_order(
        kind="buy",
        sellToken=sell_token,
        buyToken=buy_token,
        sellAmount=93_262_719_916,  # 93,262.72 USDC limit
        buyAmount=100_000_000,      # 1 WBTC exact
        partiallyFillable=True,
    )

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=True)
    result = await router.solve(_make_auction([order], tokens=tokens))

    assert isinstance(result, Solution)
    assert result.prices[sell_token] == 100_000_000          # buy_amount
    assert result.prices[buy_token] == amm_amount_in         # amount_in
    assert result.prices[sell_token] != tokens[sell_token].reference_price
    assert result.prices[buy_token] != tokens[buy_token].reference_price

    # End-to-end check: score must reflect realised surplus only.
    # Realised surplus_sell = signed_sell − amm_amount_in
    #                       = 93,262,719,916 − 80,000,000,000 = 13,262,719,916 atom-USDC
    # In ETH-equiv via native_price_buy=wbtc_ref scaled by 1e18 numéraire,
    # this stays in the milliETH-to-low-ETH band, NOT 6.6 ETH.
    orders_by_uid = {order.uid: order.model_dump(by_alias=True)}
    native_prices = {sell_token: usdc_ref, buy_token: wbtc_ref}
    sol_dict = {
        "prices": {k: str(v) for k, v in result.prices.items()},
        "trades": [
            {
                "kind": "fulfillment",
                "orderUid": order.uid,
                "executedAmount": str(result.trades[0].executed_amount),
            }
        ],
    }
    score_wei = compute_solution_score(sol_dict, orders_by_uid, native_prices)
    # Compute the score the OLD (buggy) reference-price-as-clearing-price
    # code would have persisted, against the SAME auction inputs.
    phantom_sol_dict = {
        "prices": {sell_token: str(usdc_ref), buy_token: str(wbtc_ref)},
        "trades": sol_dict["trades"],
    }
    phantom_score = compute_solution_score(
        phantom_sol_dict, orders_by_uid, native_prices
    )
    # Phantom in this exact scenario ≈ 6.65 ETH (matches live id=6229 row).
    # Real at AMM-rate ≈ 5.17 ETH (AMM 80k > oracle 76,240 → less surplus
    # captured than oracle would have implied). Both are non-trivial because
    # the order has 22 % slack vs market — that slack is realistically
    # reduced to what the AMM actually leaves on the table.
    assert phantom_score > 6 * 10**18, (
        "sanity: oracle-clearing-price scoring on this scenario should "
        f"reproduce the ~6.6 ETH phantom, got {phantom_score}"
    )
    assert score_wei < phantom_score, (
        f"AMM-rate clearing prices must produce STRICTLY LESS surplus than "
        f"oracle-rate would: got {score_wei} vs phantom {phantom_score}"
    )
    # Belt-and-braces: realised score should reflect the AMM gap exactly.
    # signed_sell − amm_amount_in = 13,262,719,916 atom-USDC of realised
    # buy-order surplus. The CIP-14 conversion then translates that to the
    # WBTC-numéraire equivalent. The exact figure depends on integer
    # divisions in _score_buy_trade — we pin it loose to avoid brittle math.
    assert 4 * 10**18 < score_wei < 6 * 10**18, (
        f"realised score expected in 4-6 ETH band for this synthetic, "
        f"got {score_wei}"
    )


@pytest.mark.asyncio
async def test_clearing_prices_zero_surplus_when_amm_at_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AMM rate exactly equals the order's limit → score must be 0.

    Guards against a sign-flip in _score_buy_trade after the clearing-price
    convention change. With cp = AMM rate and AMM rate = limit rate, the
    surplus expression collapses to 0 by construction.
    """
    from src.routing.v3_batched import V3BatchedQuote, V3Path
    from src.shadow.scoring import compute_solution_score

    sell_token = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    buy_token = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    # AMM charges exactly the full signed_sell → zero surplus
    async def mock_batched(
        _mc: object, paths: list[V3Path], **_: object
    ) -> list[V3BatchedQuote]:
        return [V3BatchedQuote(path=p, amount_out=1000) for p in paths]

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    order = _make_order(
        kind="buy",
        sellToken=sell_token,
        buyToken=buy_token,
        sellAmount=1000,
        buyAmount=500,
    )

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=True)
    result = await router.solve(_make_auction([order]))

    assert isinstance(result, Solution)
    orders_by_uid = {order.uid: order.model_dump(by_alias=True)}
    native_prices = {sell_token: 10**18, buy_token: 10**18}
    sol_dict = {
        "prices": {k: str(v) for k, v in result.prices.items()},
        "trades": [
            {
                "kind": "fulfillment",
                "orderUid": order.uid,
                "executedAmount": str(result.trades[0].executed_amount),
            }
        ],
    }
    score_wei = compute_solution_score(sol_dict, orders_by_uid, native_prices)
    assert score_wei == 0, (
        f"AMM-rate == limit-rate must score 0, got {score_wei}"
    )


# ── Phase 3: partial-fill router partial-quote search ─────────────────────────


@pytest.mark.asyncio
async def test_partial_quote_search_emits_at_75pct_when_full_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sell partial order: full(1000)→800 misses limit 900; 0.75x(750)→700
    clears 0.75×900=675; 0.5x(500)→500 clears 0.5×900=450.
    Expected: emit at 0.75x (largest feasible), executed_amount=750.
    """
    from src.routing.v3_batched import V3BatchedQuote, V3Path

    def _mock_batched(
        _mc: object, paths: list[V3Path], **_: object
    ) -> list[V3BatchedQuote]:
        out = []
        for p in paths:
            if p.amount_in == 1000:
                amt = 800   # full: misses limit 900
            elif p.amount_in == 750:
                amt = 700   # 0.75x: clears 675 (0.75×900)
            elif p.amount_in == 500:
                amt = 500   # 0.5x: clears 450 (0.5×900)
            else:
                amt = 0
            out.append(V3BatchedQuote(path=p, amount_out=amt))
        return out

    async def mock_batched(
        _mc: object, paths: list[V3Path], **_: object
    ) -> list[V3BatchedQuote]:
        return _mock_batched(_mc, paths)

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=True)
    order = _make_order(
        uid="pf1",
        sellAmount=1000,
        buyAmount=900,
        partiallyFillable=True,
    )
    result = await router.solve(_make_auction([order]))

    assert isinstance(result, Solution), "expected a Solution, got NoSolution"
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.order_uid == "pf1"
    # Emitted at 0.75x — executed sell amount is 750 (not full 1000)
    assert trade.executed_amount == 750, (
        f"expected executed_amount=750 (0.75x), got {trade.executed_amount}"
    )
    # Clearing prices reflect AMM output at partial amount
    assert result.prices["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"] == 700, (
        f"expected cp[sell_token]=700 (amm output at 0.75x), got {result.prices['0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa']}"
    )
    assert result.prices["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"] == 750, (
        f"expected cp[buy_token]=750 (partial sell), got {result.prices['0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb']}"
    )


@pytest.mark.asyncio
async def test_partial_quote_search_emits_at_75pct_when_50_clears_75_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both partial fractions clear; pick the larger (0.75x).
    full(1000)→800 miss, 0.75x(750)→690 clears (≥675), 0.5x(500)→460 clears (≥450).
    Expected: emit at 0.75x, executed_amount=750.
    """
    from src.routing.v3_batched import V3BatchedQuote, V3Path

    async def mock_batched(
        _mc: object, paths: list[V3Path], **_: object
    ) -> list[V3BatchedQuote]:
        out = []
        for p in paths:
            if p.amount_in == 1000:
                amt = 800   # full: misses 900
            elif p.amount_in == 750:
                amt = 690   # 0.75x: clears 675
            elif p.amount_in == 500:
                amt = 460   # 0.5x: clears 450
            else:
                amt = 0
            out.append(V3BatchedQuote(path=p, amount_out=amt))
        return out

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=True)
    order = _make_order(
        uid="pf2",
        sellAmount=1000,
        buyAmount=900,
        partiallyFillable=True,
    )
    result = await router.solve(_make_auction([order]))

    assert isinstance(result, Solution)
    trade = result.trades[0]
    assert trade.executed_amount == 750, (
        f"should pick 0.75x (larger feasible), got {trade.executed_amount}"
    )


@pytest.mark.asyncio
async def test_partial_quote_search_no_op_when_full_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full-amount quote clears: emit at full amount, partial paths requested
    in batch but their output is unused.
    """
    from src.routing.v3_batched import V3BatchedQuote, V3Path

    received_paths: list[V3Path] = []

    async def mock_batched(
        _mc: object, paths: list[V3Path], **_: object
    ) -> list[V3BatchedQuote]:
        received_paths.extend(paths)
        out = []
        for p in paths:
            # Full (1000) clears; partials would also clear but should be unused
            amt = 1000 if p.amount_in == 1000 else 700
            out.append(V3BatchedQuote(path=p, amount_out=amt))
        return out

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=True)
    order = _make_order(
        uid="pf3",
        sellAmount=1000,
        buyAmount=900,
        partiallyFillable=True,
    )
    result = await router.solve(_make_auction([order]))

    assert isinstance(result, Solution)
    trade = result.trades[0]
    # Must emit at FULL amount, not partial
    assert trade.executed_amount == 1000, (
        f"expected full executed_amount=1000, got {trade.executed_amount}"
    )
    # Verify partial paths WERE sent in the batch (batched-upfront design)
    half_amount_paths = [p for p in received_paths if p.amount_in == 500]
    assert len(half_amount_paths) > 0, (
        "partial (0.5x) paths should still be included in the batch for partial orders"
    )
    three_quarter_paths = [p for p in received_paths if p.amount_in == 750]
    assert len(three_quarter_paths) > 0, (
        "partial (0.75x) paths should still be included in the batch for partial orders"
    )


@pytest.mark.asyncio
async def test_partial_quote_search_does_not_search_for_non_partial_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-partial sell order misses limit: emit NoSolution; no partial paths
    are added to the batch for this order.
    """
    from src.routing.v3_batched import V3BatchedQuote, V3Path

    received_paths: list[V3Path] = []

    async def mock_batched(
        _mc: object, paths: list[V3Path], **_: object
    ) -> list[V3BatchedQuote]:
        received_paths.extend(paths)
        return [V3BatchedQuote(path=p, amount_out=800) for p in paths]

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=True)
    order = _make_order(
        uid="np1",
        sellAmount=1000,
        buyAmount=900,
        partiallyFillable=False,  # NOT partial
    )
    result = await router.solve(_make_auction([order]))

    # Full amount misses limit → NoSolution (existing behaviour preserved)
    assert isinstance(result, NoSolution), (
        "non-partial order that misses limit must still yield NoSolution"
    )
    # No partial paths should have been added to the batch for this order
    half_paths = [p for p in received_paths if p.amount_in == 500]
    assert len(half_paths) == 0, (
        f"partial 0.5x paths must NOT be generated for non-partial orders, "
        f"got {len(half_paths)}"
    )
    three_quarter_paths = [p for p in received_paths if p.amount_in == 750]
    assert len(three_quarter_paths) == 0, (
        f"partial 0.75x paths must NOT be generated for non-partial orders, "
        f"got {len(three_quarter_paths)}"
    )


@pytest.mark.asyncio
async def test_partial_quote_search_path_count_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One partial sell + one non-partial sell.
    Partial order: 3 × FEE_TIERS paths (full + 0.5x + 0.75x).
    Non-partial order: 1 × FEE_TIERS paths (full only).
    Verifies that amount_in == sell_amount//2 appears exactly len(FEE_TIERS)
    times in the batch (once per fee tier for the partial order only).
    """
    from src.routing.amm_v3 import FEE_TIERS
    from src.routing.v3_batched import V3BatchedQuote, V3Path

    received_paths: list[V3Path] = []

    async def mock_batched(
        _mc: object, paths: list[V3Path], **_: object
    ) -> list[V3BatchedQuote]:
        received_paths.extend(paths)
        return [V3BatchedQuote(path=p, amount_out=0) for p in paths]

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    multicall = AsyncMock()
    # No intermediates: only direct paths (1 path per fee tier per fraction)
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=True)

    partial_order = _make_order(
        uid="partial",
        sellAmount=1000,
        buyAmount=900,
        partiallyFillable=True,
    )
    non_partial_order = _make_order(
        uid="nonpartial",
        sellAmount=2000,
        buyAmount=1800,
        partiallyFillable=False,
    )
    await router.solve(_make_auction([partial_order, non_partial_order]))

    n_fee_tiers = len(FEE_TIERS)

    # partial order: sell_amount // 2 = 500
    half_paths_partial = [
        p for p in received_paths
        if p.order_uid == "partial" and p.amount_in == 500
    ]
    assert len(half_paths_partial) == n_fee_tiers, (
        f"partial order must produce {n_fee_tiers} paths at 0.5x, "
        f"got {len(half_paths_partial)}"
    )

    # non-partial order must have zero 0.5x paths
    half_paths_non_partial = [
        p for p in received_paths
        if p.order_uid == "nonpartial" and p.amount_in == 1000  # 2000 // 2
    ]
    assert len(half_paths_non_partial) == 0, (
        "non-partial order must produce 0 paths at 0.5x (sell_amount//2=1000), "
        "but amount_in=1000 also equals full-amount for a 1000-sell order... "
        "checking by uid: non-partial order full paths only"
    )
    # More precisely: non-partial uid should only have amount_in == 2000 paths
    non_partial_paths_all = [p for p in received_paths if p.order_uid == "nonpartial"]
    assert all(p.amount_in == 2000 for p in non_partial_paths_all), (
        f"non-partial order must only have full-amount (2000) paths, "
        f"got amounts: {sorted({p.amount_in for p in non_partial_paths_all})}"
    )


@pytest.mark.asyncio
async def test_partial_quote_search_emits_at_75pct_when_50pct_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asymmetric AMM curve: 0.75x clears the pro-rata limit, 0.5x does not.
    Documents the deviation from the spec's literal `if 0.5x clears` gate.
    A concentrated-liquidity tick boundary can produce this.

    full(1000) -> 800, miss (limit 900)
    0.75x(750) -> 690, clears (limit 675)
    0.5x(500) -> 400, MISS (limit 450)
    Expected: emit at 0.75x, executed_amount=750.
    """
    from src.routing.v3_batched import V3BatchedQuote, V3Path

    async def mock_batched(
        _mc: object, paths: list[V3Path], **_: object
    ) -> list[V3BatchedQuote]:
        out = []
        for p in paths:
            if p.amount_in == 1000:
                amt = 800   # full: misses 900
            elif p.amount_in == 750:
                amt = 690   # 0.75x: clears 675
            elif p.amount_in == 500:
                amt = 400   # 0.5x: MISSES 450 (asymmetric AMM curve)
            else:
                amt = 0
            out.append(V3BatchedQuote(path=p, amount_out=amt))
        return out

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    multicall = AsyncMock()
    router = RouterSolver(multicall=multicall, intermediates=[], v3_only_batched=True)
    order = _make_order(
        uid="pf_asym",
        sellAmount=1000,
        buyAmount=900,
        partiallyFillable=True,
    )
    result = await router.solve(_make_auction([order]))

    assert isinstance(result, Solution), (
        "spec-deviation case: 0.75x clears, 0.5x misses → should still emit at 0.75x"
    )
    trade = result.trades[0]
    assert trade.executed_amount == 750, (
        f"expected executed_amount=750 (0.75x), got {trade.executed_amount}"
    )
