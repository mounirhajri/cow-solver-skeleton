"""Shadow poller: replays recent CoW Protocol batch auctions through our /solve endpoint.

Replaces the cow-shadow-driver binary (which removed --shadow mode in recent versions).
Polls solver_competition/latest from the public CoW Orderbook API, reconstructs a
minimal auction payload from order UIDs, calls our solver, and logs results with
winner comparison — no solver registration required.

Uses urllib (not httpx) for CoW API calls — the CoW API blocks httpx's TLS fingerprint
but accepts curl/urllib connections.
"""
import asyncio
import json
import logging
import os
import ssl
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from scripts.liveness import touch_liveness

BASE_URL = "https://api.cow.fi/arbitrum_one/api/v1"
MAX_ORDERS = 40
POLL_INTERVAL = 60  # seconds

SOLVER_URL = os.environ.get("SOLVER_INTERNAL_URL", "http://cow-solver:8000")
SHADOW_LOG_PATH = Path(os.environ.get("SHADOW_LOG_PATH", "/data/shadow.jsonl"))
LIVENESS_PATH = Path("/data/shadow_poller.alive")

_UA = "curl/8.5.0"
_SSL_CTX = ssl.create_default_context()

log = logging.getLogger(__name__)


def _urllib_get(url: str) -> "dict[str, Any] | None":
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=10) as resp:
            result: dict[str, Any] = json.loads(resp.read())
            return result
    except urllib.error.HTTPError as e:
        if e.code == 429:
            log.warning("rate_limited_skipping_poll")
            return None
        raise


async def _cow_get(url: str) -> "dict[str, Any] | None":
    return await asyncio.to_thread(_urllib_get, url)


async def _fetch_order(uid: str) -> "dict[str, Any] | None":
    try:
        return await _cow_get(f"{BASE_URL}/orders/{uid}")
    except Exception:
        return None


async def poll_once(solver: httpx.AsyncClient, seen: set[int]) -> None:
    comp = await _cow_get(f"{BASE_URL}/solver_competition/latest")
    if comp is None:
        return

    auction_id = int(str(comp["auctionId"]))
    if auction_id in seen:
        return
    seen.add(auction_id)

    auction_data: dict[str, Any] = comp.get("auction") or {}
    uids: list[str] = [str(u) for u in (auction_data.get("orders") or [])][:MAX_ORDERS]
    prices: dict[str, str] = {str(k): str(v) for k, v in (auction_data.get("prices") or {}).items()}

    orders = [
        o
        for o in await asyncio.gather(*[_fetch_order(uid) for uid in uids])
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
        resp = await solver.post(
            f"{SOLVER_URL}/solve", json=auction_payload, timeout=14.0
        )
        if resp.status_code == 200:
            our_solution = resp.json()
    except Exception as exc:
        log.warning("solver_call_failed", extra={"auction_id": auction_id, "error": str(exc)})

    solutions: list[dict[str, Any]] = [s for s in (comp.get("solutions") or [])]
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
    touch_liveness(LIVENESS_PATH)


async def main() -> None:
    seen: set[int] = set()
    async with httpx.AsyncClient() as solver:
        while True:
            touch_liveness(LIVENESS_PATH)
            try:
                await poll_once(solver, seen)
            except Exception as exc:
                log.exception("poll_error: %s", exc)
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main())
