"""Tests for the Redis-backed PoolCache.

Implementation note: `fakeredis` is not listed in pyproject.toml dev deps and
adding a new dependency is out of scope for this change, so we mock the Redis
async client with a tiny in-process dict-backed stub that mirrors the subset of
the API PoolCache actually touches (get / setex). This keeps the tests fast and
free of network deps.
"""

from __future__ import annotations

import json

import pytest

from edge.pool_indexer.pool_cache import PoolCache
from src.routing.amm_v2 import PoolReserves


class _FakeRedis:
    """Minimal async-Redis stub: just get + setex over an in-memory dict."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        # Captured (key, ttl) pairs so tests can assert TTL was applied.
        self.setex_calls: list[tuple[str, int]] = []

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str | bytes) -> None:
        self.setex_calls.append((key, ttl))
        if isinstance(value, str):
            value = value.encode("utf-8")
        self.store[key] = value


@pytest.mark.asyncio
async def test_get_pool_addresses_miss_returns_none() -> None:
    cache = PoolCache(redis=_FakeRedis())
    assert await cache.get_pool_addresses("0xAa", "0xBb") is None


@pytest.mark.asyncio
async def test_pool_addresses_roundtrip_lowercased() -> None:
    cache = PoolCache(redis=_FakeRedis())
    await cache.set_pool_addresses(
        "0xAa", "0xBb", {"sushi": "0xCC", "camelot": "0xDD"}
    )
    got = await cache.get_pool_addresses("0xAa", "0xBb")
    assert got == {"sushi": "0xcc", "camelot": "0xdd"}


@pytest.mark.asyncio
async def test_pool_addresses_order_invariant() -> None:
    cache = PoolCache(redis=_FakeRedis())
    await cache.set_pool_addresses("0xAa", "0xBb", {"sushi": "0xCC"})
    # Reversed order must hit the same slot.
    got = await cache.get_pool_addresses("0xBb", "0xAa")
    assert got == {"sushi": "0xcc"}


@pytest.mark.asyncio
async def test_pool_addresses_uses_7d_ttl() -> None:
    fake = _FakeRedis()
    cache = PoolCache(redis=fake)
    await cache.set_pool_addresses("0xa", "0xb", {})
    assert fake.setex_calls
    _, ttl = fake.setex_calls[-1]
    assert ttl == 7 * 24 * 3600


@pytest.mark.asyncio
async def test_get_reserves_miss_returns_none() -> None:
    cache = PoolCache(redis=_FakeRedis())
    assert await cache.get_reserves("0xpool") is None


@pytest.mark.asyncio
async def test_set_reserves_uses_configured_ttl() -> None:
    fake = _FakeRedis()
    cache = PoolCache(redis=fake, reserves_ttl=123)
    await cache.set_reserves(
        "0xPOOL",
        PoolReserves(reserve0=10, reserve1=20, token0="0xt0", token1="0xt1"),
    )
    assert fake.setex_calls
    key, ttl = fake.setex_calls[-1]
    assert ttl == 123
    assert key.endswith(":0xpool")  # pool address lower-cased into the key


@pytest.mark.asyncio
async def test_reserves_roundtrip_preserves_big_ints() -> None:
    """uint112 reserves exceed JS Number precision; must survive serialisation."""
    fake = _FakeRedis()
    cache = PoolCache(redis=fake)
    big = 2**110
    await cache.set_reserves(
        "0xpool",
        PoolReserves(reserve0=big, reserve1=big + 1, token0="0xt0", token1="0xt1"),
    )
    got = await cache.get_reserves("0xpool")
    assert got is not None
    assert got.reserve0 == big
    assert got.reserve1 == big + 1
    assert got.token0 == "0xt0"
    # And the on-the-wire value is a JSON string, not a JSON int.
    raw = fake.store[next(iter(fake.store))]
    parsed = json.loads(raw)
    assert isinstance(parsed["reserve0"], str)


@pytest.mark.asyncio
async def test_key_prefix_propagates() -> None:
    fake = _FakeRedis()
    cache = PoolCache(redis=fake, key_prefix="testprefix:")
    await cache.set_pool_addresses("0xa", "0xb", {})
    await cache.set_reserves(
        "0xpool",
        PoolReserves(reserve0=1, reserve1=1, token0="0xt0", token1=""),
    )
    assert all(k.startswith("testprefix:pool:") for k in fake.store)
