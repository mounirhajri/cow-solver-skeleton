"""Tests for the Redis-backed OrderCache.

Mirrors tests/test_edge/test_pool_cache.py: fakeredis isn't a dev dep, so we
stub the async Redis client with a tiny dict-backed object exposing get/setex.
"""

from __future__ import annotations

import pytest

from src.shadow.order_cache import OrderCache


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.setex_calls: list[tuple[str, int]] = []

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str | bytes) -> None:
        self.setex_calls.append((key, ttl))
        if isinstance(value, str):
            value = value.encode("utf-8")
        self.store[key] = value


@pytest.mark.asyncio
async def test_get_miss_returns_none() -> None:
    cache = OrderCache(redis=_FakeRedis())
    assert await cache.get("0xabc") is None


@pytest.mark.asyncio
async def test_roundtrip_preserves_order() -> None:
    cache = OrderCache(redis=_FakeRedis())
    order = {"uid": "0xABC", "signature": "0xdead", "sellAmount": "1000"}
    await cache.set("0xABC", order)
    got = await cache.get("0xABC")
    assert got == order


@pytest.mark.asyncio
async def test_key_is_lowercased_and_prefixed() -> None:
    fake = _FakeRedis()
    cache = OrderCache(redis=fake, key_prefix="solver:")
    await cache.set("0xABC", {"uid": "0xABC"})
    assert "solver:order:0xabc" in fake.store


@pytest.mark.asyncio
async def test_set_applies_7d_ttl() -> None:
    fake = _FakeRedis()
    cache = OrderCache(redis=fake)
    await cache.set("0xabc", {"uid": "0xabc"})
    assert fake.setex_calls[0][1] == 7 * 24 * 3600
