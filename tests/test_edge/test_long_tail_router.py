"""Tests for the lazy-indexer long-tail router."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from edge.pool_indexer.long_tail_router import LongTailRouter
from src.models.auction import Auction, Token
from src.models.order import Order
from src.models.solution import Solution
from src.routing.amm_v2 import PoolReserves
from src.solver.base import NoSolution

# ── helpers ───────────────────────────────────────────────────────────────────

_SELL = "0x" + "a" * 40
_BUY = "0x" + "b" * 40


def _make_order(**kwargs: object) -> Order:
    defaults: dict[str, object] = {
        "uid": "o1",
        "sellToken": _SELL,
        "buyToken": _BUY,
        "sellAmount": 10**18,
        "buyAmount": 1000,  # easy limit to clear in tests
        "feePolicies": [],
        "validTo": 99,
        "kind": "sell",
        "owner": "0x" + "c" * 40,
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


def _cache_mock(
    addresses: dict[str, str] | None = None,
    reserves: dict[str, PoolReserves] | None = None,
) -> AsyncMock:
    """AsyncMock pool cache pre-seeded with optional address + reserves hits."""
    cache = AsyncMock()
    cache.get_pool_addresses.return_value = addresses
    cache.set_pool_addresses.return_value = None

    async def _get_reserves(pool_addr: str) -> PoolReserves | None:
        if reserves is None:
            return None
        return reserves.get(pool_addr.lower())

    cache.get_reserves.side_effect = _get_reserves
    cache.set_reserves.return_value = None
    return cache


# ── empty / buy-only ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_orders_returns_no_solution() -> None:
    router = LongTailRouter(multicall=AsyncMock(), pool_cache=_cache_mock())
    result = await router.solve(_make_auction([]))
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_only_buy_orders_returns_no_solution() -> None:
    router = LongTailRouter(multicall=AsyncMock(), pool_cache=_cache_mock())
    result = await router.solve(_make_auction([_make_order(kind="buy")]))
    assert isinstance(result, NoSolution)


# ── cache hit path ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cache_hit_with_winning_pool_emits_trade() -> None:
    """Pool addresses + reserves cached → no RPC, trade emitted."""
    pool_addr = "0x" + "1" * 40
    cache = _cache_mock(
        addresses={"sushi": pool_addr},
        reserves={
            pool_addr: PoolReserves(
                # token0 == sell token → reserve0 is reserve_in.
                reserve0=10**24,
                reserve1=10**24,
                token0=_SELL,
                token1=_BUY,
            ),
        },
    )
    multicall = AsyncMock()
    router = LongTailRouter(multicall=multicall, pool_cache=cache)
    result = await router.solve(_make_auction([_make_order()]))
    assert isinstance(result, Solution)
    assert len(result.trades) == 1
    assert result.trades[0].order_uid == "o1"
    # No RPC calls expected on full cache hit.
    multicall.aggregate.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_hit_insufficient_amount_out_returns_no_solution() -> None:
    """Reserves are tiny → quote falls below buy_amount → no trade."""
    pool_addr = "0x" + "1" * 40
    cache = _cache_mock(
        addresses={"sushi": pool_addr},
        reserves={
            pool_addr: PoolReserves(
                # Tiny reserve_out, big amount_in → amount_out ~ 0.
                reserve0=10**24,
                reserve1=10,
                token0=_SELL,
                token1=_BUY,
            ),
        },
    )
    router = LongTailRouter(multicall=AsyncMock(), pool_cache=cache)
    result = await router.solve(_make_auction([_make_order(buyAmount=10**18)]))
    assert isinstance(result, NoSolution)


# ── cache miss path ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cache_miss_triggers_find_and_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    pool_addr = "0x" + "1" * 40
    find_calls: list[tuple[str, str]] = []
    fetch_calls: list[dict[str, str]] = []

    async def fake_find(_mc: object, a: str, b: str) -> dict[str, str]:
        find_calls.append((a, b))
        return {"sushi": pool_addr}

    async def fake_fetch(_mc: object, pools: dict[str, str]) -> dict[str, PoolReserves]:
        fetch_calls.append(pools)
        return {
            "sushi": PoolReserves(
                reserve0=10**24,
                reserve1=10**24,
                token0=_SELL,
                token1=_BUY,
            ),
        }

    monkeypatch.setattr(
        "edge.pool_indexer.long_tail_router.find_pool_addresses", fake_find
    )
    monkeypatch.setattr(
        "edge.pool_indexer.long_tail_router.fetch_reserves", fake_fetch
    )

    cache = _cache_mock(addresses=None, reserves=None)
    router = LongTailRouter(multicall=AsyncMock(), pool_cache=cache)
    result = await router.solve(_make_auction([_make_order()]))

    assert isinstance(result, Solution)
    assert find_calls == [(_SELL, _BUY)]
    assert fetch_calls == [{"sushi": pool_addr}]
    cache.set_pool_addresses.assert_awaited_once()
    cache.set_reserves.assert_awaited_once()


# ── best-of-pools selection ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_picks_best_factory_across_pools() -> None:
    """When sushi and camelot are both cached, the higher quote wins."""
    sushi_pool = "0x" + "1" * 40
    camelot_pool = "0x" + "2" * 40
    cache = _cache_mock(
        addresses={"sushi": sushi_pool, "camelot": camelot_pool},
        reserves={
            # camelot has 10% more output liquidity, so it should win.
            sushi_pool: PoolReserves(
                reserve0=10**24,
                reserve1=10**24,
                token0=_SELL,
                token1=_BUY,
            ),
            camelot_pool: PoolReserves(
                reserve0=10**24,
                reserve1=11 * 10**23,
                token0=_SELL,
                token1=_BUY,
            ),
        },
    )

    # Probe the quoter directly so we can compare to a single-pool baseline.
    router = LongTailRouter(multicall=AsyncMock(), pool_cache=cache)
    order = _make_order(sellAmount=10**18, buyAmount=1)
    best, hits, misses = await router._quote_order(order)

    # Baseline: sushi alone.
    sushi_only = _cache_mock(
        addresses={"sushi": sushi_pool},
        reserves={
            sushi_pool: PoolReserves(
                reserve0=10**24,
                reserve1=10**24,
                token0=_SELL,
                token1=_BUY,
            ),
        },
    )
    router_solo = LongTailRouter(multicall=AsyncMock(), pool_cache=sushi_only)
    sushi_quote, _, _ = await router_solo._quote_order(order)

    assert best is not None and sushi_quote is not None
    assert best > sushi_quote, "camelot's deeper reserve should beat sushi"
    # Two pool hits + one address hit = three cache hits, zero misses.
    assert misses == 0
    assert hits >= 2


# ── token0 orientation ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handles_token0_oriented_either_way() -> None:
    """Whether the sell token is token0 or token1, the quote must be the same."""
    pool_addr = "0x" + "1" * 40

    cache_sell_is_t0 = _cache_mock(
        addresses={"sushi": pool_addr},
        reserves={
            pool_addr: PoolReserves(
                reserve0=10**24,
                reserve1=5 * 10**23,
                token0=_SELL,
                token1=_BUY,
            ),
        },
    )
    cache_sell_is_t1 = _cache_mock(
        addresses={"sushi": pool_addr},
        reserves={
            pool_addr: PoolReserves(
                # Reserves swapped to mirror the orientation flip.
                reserve0=5 * 10**23,
                reserve1=10**24,
                token0=_BUY,
                token1=_SELL,
            ),
        },
    )

    order = _make_order(sellAmount=10**18, buyAmount=1)
    r1 = LongTailRouter(multicall=AsyncMock(), pool_cache=cache_sell_is_t0)
    r2 = LongTailRouter(multicall=AsyncMock(), pool_cache=cache_sell_is_t1)
    q1, _, _ = await r1._quote_order(order)
    q2, _, _ = await r2._quote_order(order)
    assert q1 is not None and q2 is not None
    assert q1 == q2


# ── order cap ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_respects_max_orders_top_n_by_eth_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With reference prices set, top-N selection ranks by ETH value, not raw amount."""
    weth = "0x" + "1" * 40
    usdc = "0x" + "2" * 40
    dai = "0x" + "3" * 40
    tokens = {
        weth: Token(decimals=18, referencePrice=10**18),         # 1 ETH/token
        usdc: Token(decimals=6, referencePrice=25 * 10**13),     # ~0.00025 ETH/token
        dai: Token(decimals=18, referencePrice=10**18),
    }
    quoted: list[str] = []

    async def fake_quote_order(self: LongTailRouter, order: Order) -> tuple[int, int, int]:
        quoted.append(order.uid)
        return 0, 0, 0

    monkeypatch.setattr(LongTailRouter, "_quote_order", fake_quote_order)

    orders = [
        _make_order(uid="weth_o", sellToken=weth, buyToken=dai, sellAmount=10**18),
        _make_order(uid="usdc_o", sellToken=usdc, buyToken=dai, sellAmount=1000 * 10**6),
    ]
    router = LongTailRouter(
        multicall=AsyncMock(), pool_cache=_cache_mock(), max_orders=1
    )
    await router.solve(_make_auction(orders, tokens=tokens))
    assert quoted == ["weth_o"], "should pick the higher-ETH-value order"


# ── concurrency ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_semaphore_limits_concurrent_quotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With max_concurrent=2, at most 2 quotes are in-flight at once."""
    in_flight = 0
    max_seen = 0
    gate = asyncio.Event()

    async def fake_quote_order(
        self: LongTailRouter, order: Order
    ) -> tuple[int, int, int]:
        nonlocal in_flight, max_seen
        in_flight += 1
        max_seen = max(max_seen, in_flight)
        await gate.wait()
        in_flight -= 1
        return 0, 0, 0

    monkeypatch.setattr(LongTailRouter, "_quote_order", fake_quote_order)

    orders = [_make_order(uid=f"o{i}", sellAmount=i * 10) for i in range(1, 11)]
    router = LongTailRouter(
        multicall=AsyncMock(),
        pool_cache=_cache_mock(),
        max_concurrent=2,
    )
    task = asyncio.create_task(router.solve(_make_auction(orders)))

    # Let the first wave start.
    for _ in range(5):
        await asyncio.sleep(0)

    assert max_seen <= 2, f"semaphore violated: {max_seen} in flight"
    gate.set()
    await task


# ── exposes timeout ───────────────────────────────────────────────────────────

def test_exposes_timeout_attribute() -> None:
    router = LongTailRouter(multicall=AsyncMock(), pool_cache=_cache_mock())
    assert hasattr(router, "timeout")
    assert router.timeout == 9.0


def test_timeout_overridable() -> None:
    router = LongTailRouter(
        multicall=AsyncMock(), pool_cache=_cache_mock(), strategy_timeout=4.5
    )
    assert router.timeout == 4.5
