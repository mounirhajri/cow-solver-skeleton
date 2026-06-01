"""Manual smoke test: a REAL settled winner solution must validate FEASIBLE.

Run inside the cow-solver container:
    python -m scripts.smoke_feasibility

Picks the most recent shadow_winners row with a non-empty raw_solution, backfills
its order signatures, encodes settle(), and eth_calls it at latest block. A
known-good settled solution MUST come back feasible=True. If it reverts, the
encoder (flags / token indexing / interaction packing) or the solver_address
allowlist is wrong — fix before trusting any phantom verdict.
"""

import asyncio
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import select

from src.config import settings
from src.persistence.db import get_session_factory
from src.persistence.models import ShadowWinner
from src.routing.rpc import RpcClient
from src.shadow.cow_api import CowApiClient
from src.shadow.feasibility import validate_solution
from src.shadow.order_cache import OrderCache


def _normalize_winner_solution(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate a CoW competition solution into validate_solution's shape.

    The winner row is stored verbatim from the solver-competition endpoint,
    which uses camelCase ``clearingPrices`` and an ``orders`` list of
    ``{"id": <uid>, "executedAmount": ...}``. ``validate_solution`` reads
    ``prices`` / ``trades`` (fulfillment kind, ``orderUid``). Without this
    mapping the smoke test silently no-ops (no prices → UNKNOWN verdict),
    defeating its whole purpose as an encoder gate.

    Solutions already in our internal shape (``prices``/``trades`` present)
    pass through unchanged.
    """
    if raw.get("prices") or raw.get("trades"):
        return raw

    prices = raw.get("clearingPrices") or {}
    trades = [
        {
            "kind": "fulfillment",
            "orderUid": o.get("id") or o.get("orderUid") or o.get("order_uid"),
            "executedAmount": o.get("executedAmount") or o.get("executed_amount"),
        }
        for o in (raw.get("orders") or [])
        if isinstance(o, dict)
    ]
    return {
        "prices": prices,
        "trades": trades,
        "interactions": raw.get("interactions") or [],
    }


async def main() -> None:
    sf = get_session_factory()
    async with sf() as session:
        row = (await session.execute(
            select(ShadowWinner)
            .where(ShadowWinner.raw_solution.is_not(None))
            .order_by(ShadowWinner.auction_id.desc())
            .limit(1)
        )).scalars().first()
    assert row is not None, "no winner with raw_solution"
    solution = _normalize_winner_solution(row.raw_solution)
    assert solution.get("trades"), (
        f"winner {row.auction_id} normalized to zero trades — "
        "raw_solution shape unexpected, smoke test would no-op"
    )

    redis = aioredis.Redis.from_url(settings.redis_url, decode_responses=False)
    verdict = await validate_solution(
        solution,
        cache=OrderCache(redis=redis, key_prefix=settings.redis_key_prefix),
        api=CowApiClient(network="arbitrum_one"),
        rpc=RpcClient(settings.rpc_arbitrum),
        settlement_addr=settings.gpv2_settlement,
        solver_addr=settings.feasibility_solver_address,
    )
    print(f"auction {row.auction_id}: feasible={verdict.feasible} reason={verdict.reason}")
    assert verdict.feasible is True, (
        "known-good winner came back NOT feasible — encoder/allowlist bug"
    )


if __name__ == "__main__":
    asyncio.run(main())
