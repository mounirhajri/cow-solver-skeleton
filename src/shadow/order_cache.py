"""Redis-backed cache of CoW orders fetched for signature backfill.

An order's signature is immutable once signed, so we cache the full order JSON
under a long TTL and fetch each uid from the CoW API at most once. CloudFront
rate-limits the orderbook API aggressively (see scripts/sync_competitions.py),
so this cache is load-bearing, not just an optimisation.
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

# Signatures don't change; a week keeps the cache warm across solver restarts
# while still evicting orders nobody references anymore.
_ORDER_TTL_SECONDS = 7 * 24 * 3600


class OrderCache:
    """Redis-backed cache of CoW order dicts keyed by lower-cased uid."""

    def __init__(
        self,
        redis: aioredis.Redis[bytes],
        key_prefix: str = "solver:",
    ) -> None:
        self._redis = redis
        self._prefix = key_prefix

    def _key(self, uid: str) -> str:
        return f"{self._prefix}order:{uid.lower()}"

    async def get(self, uid: str) -> dict[str, Any] | None:
        raw = await self._redis.get(self._key(uid))
        if raw is None:
            return None
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        data: dict[str, Any] = json.loads(text)
        return data

    async def set(self, uid: str, order: dict[str, Any]) -> None:
        await self._redis.setex(self._key(uid), _ORDER_TTL_SECONDS, json.dumps(order))
