"""Shadow poller: replays recent CoW Protocol batch auctions through our /solve endpoint.

Replaces the cow-shadow-driver binary (which removed --shadow mode in recent versions).
Polls solver_competition/latest from the public CoW Orderbook API, reconstructs a
minimal auction payload from order UIDs, calls our solver, and logs results with
winner comparison — no solver registration required.
"""
import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx

BASE_URL = "https://api.cow.fi/arbitrum_one/api/v1"
MAX_ORDERS = 40
POLL_INTERVAL = 60  # seconds — stays well within CoW API rate limits

SOLVER_URL = os.environ.get("SOLVER_INTERNAL_URL", "http://cow-solver:8000")
SHADOW_LOG_PATH = Path(os.environ.get("SHADOW_LOG_PATH", "/data/shadow.jsonl"))

log = logging.getLogger(__name__)


async def _fetch_order(client: httpx.AsyncClient, uid: str) -> dict | None:
    try:
        r = await client.get(f"{BASE_URL}/orders/{uid}", timeout=5.0)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


async def poll_once(
    client: httpx.AsyncClient,
    seen: set[int],
) -> None:
    r = await client.get(f"{BASE_URL}/solver_competition/latest", timeout=10.0)
    if r.status_code == 429:
        log.warning("rate_limited_skipping_poll")
        return
    r.raise_for_status()
    comp = r.json()

    auction_id = int(comp["auctionId"])
    if auction_id in seen:
        return
    seen.add(auction_id)

    auction_data = comp.get("auction", {})
    uids: list[str] = auction_data.get("orders", [])[:MAX_ORDERS]
    prices: dict[str, str] = auction_data.get("prices", {})

    orders = [
        o
        for o in await asyncio.gather(*[_fetch_order(client, uid) for uid in uids])
        if o is not None
    ]

    tokens = {
        addr: {
            "decimals": 18,
            "referencePrice": price,
            "availableBalance": "0",
            "trusted": True,
        }
        for addr, price in prices.items()
    }

    auction_payload = {
        "id": str(auction_id),
        "tokens": tokens,
        "orders": orders,
        "liquidity": [],
        "effectiveGasPrice": "0",
        "deadline": None,
    }

    our_solution = None
    try:
        resp = await client.post(
            f"{SOLVER_URL}/solve", json=auction_payload, timeout=14.0
        )
        if resp.status_code == 200:
            our_solution = resp.json()
    except Exception as exc:
        log.warning("solver_call_failed", extra={"auction_id": auction_id, "error": str(exc)})

    solutions: list[dict] = comp.get("solutions", [])
    winner = next(
        (s for s in solutions if s.get("isWinner") or s.get("ranking") == 1), None
    )

    entry = {
        "auction_id": str(auction_id),
        "timestamp": datetime.now(UTC).isoformat(),
        "orders_sampled": len(orders),
        "our_solution": our_solution,
        "winner_solution": (
            {"solver": winner["solver"], "score": winner.get("score")}
            if winner
            else None
        ),
    }

    SHADOW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SHADOW_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")

    log.info(
        "auction_processed",
        extra={
            "auction_id": auction_id,
            "orders_sampled": len(orders),
            "solved": our_solution is not None,
            "winner": winner["solver"] if winner else None,
        },
    )


async def main() -> None:
    seen: set[int] = set()
    headers = {"User-Agent": "cow-solver-shadow-poller/0.1 (github.com/mounirhajri/cow-solver-skeleton)"}
    async with httpx.AsyncClient(headers=headers) as client:
        while True:
            try:
                await poll_once(client, seen)
            except Exception as exc:
                log.exception("poll_error: %s", exc)
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main())
