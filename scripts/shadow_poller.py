"""Shadow poller: replays recent CoW Protocol batch auctions through our /solve endpoint.

Replaces the cow-shadow-driver binary (which removed --shadow mode in recent versions).
Polls solver_competition/latest from the public CoW Orderbook API, reconstructs a
full auction payload by batch-fetching order details via POST /orders/by_uids (128
per request), calls our solver, and logs results with winner comparison — no solver
registration required.

Uses urllib (not httpx) for CoW API calls — the CoW API blocks httpx's TLS fingerprint
but accepts curl/urllib connections.
"""
import asyncio
import json
import logging
import os
import random
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from scripts.liveness import touch_liveness
from src.shadow.persist import persist_skipped_auction_safe, persist_winner_and_outcomes_safe

BASE_URL = "https://api.cow.fi/arbitrum_one/api/v1"
# Max UIDs per POST /orders/by_uids call (API limit).
BY_UIDS_BATCH_SIZE = 128
# Solver timeout — full 1200-order auctions may take longer than small batches.
SOLVER_TIMEOUT = 30.0

SOLVER_URL = os.environ.get("SOLVER_INTERNAL_URL", "http://cow-solver:8000")
SHADOW_LOG_PATH = Path(os.environ.get("SHADOW_LOG_PATH", "/data/shadow.jsonl"))
LIVENESS_PATH = Path("/data/shadow_poller.alive")

_UA = "curl/8.5.0"
_SSL_CTX = ssl.create_default_context()

log = logging.getLogger(__name__)


class RateLimitedError(Exception):
    """Raised when CoW API returns HTTP 429."""


@dataclass
class Backoff:
    """Exponential backoff with optional jitter.

    current() returns the current delay. on_rate_limit() doubles it (up to cap).
    on_success() resets to base.
    """

    base: float = 60.0
    cap: float = 600.0
    jitter: bool = True
    _level: int = field(default=0, init=False)

    def current(self) -> float:
        delay = self.base * (2**self._level)
        if self.jitter:
            delay = delay * random.uniform(0.8, 1.2)
        return min(delay, self.cap)

    def on_rate_limit(self) -> None:
        self._level += 1

    def on_success(self) -> None:
        self._level = 0


def _urllib_get(url: str) -> "dict[str, Any] | None":
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=10) as resp:
            result: dict[str, Any] = json.loads(resp.read())
            return result
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimitedError(url) from e
        raise


def _urllib_post_json(url: str, body: list[str]) -> "list[Any]":
    """POST a JSON body and return the parsed response list."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": _UA, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=30) as resp:
            result: list[Any] = json.loads(resp.read())
            return result
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimitedError(url) from e
        raise


async def _cow_get(url: str) -> "dict[str, Any] | None":
    return await asyncio.to_thread(_urllib_get, url)


async def _fetch_orders_by_uids(uids: list[str]) -> list[dict[str, Any]]:
    """Batch-fetch full order objects for a list of UIDs.

    Uses POST /orders/by_uids with batches of BY_UIDS_BATCH_SIZE (128).
    Returns only successfully fetched orders (skips error entries).
    Raises RateLimitedError if any batch hits HTTP 429.
    """
    if not uids:
        return []

    url = f"{BASE_URL}/orders/by_uids"
    chunks = [uids[i : i + BY_UIDS_BATCH_SIZE] for i in range(0, len(uids), BY_UIDS_BATCH_SIZE)]

    # Fetch all chunks concurrently
    batch_results: list[list[Any]] = await asyncio.gather(
        *[asyncio.to_thread(_urllib_post_json, url, chunk) for chunk in chunks]
    )

    orders: list[dict[str, Any]] = []
    for batch in batch_results:
        for item in batch:
            if "order" in item:
                orders.append(item["order"])
            # items with "error" key are silently skipped (expired/unknown UIDs)

    return orders


async def poll_once(solver: httpx.AsyncClient, seen: set[int]) -> str:
    """Poll for the latest auction, batch-fetch all orders, and call /solve.

    Always persists: shadow_auction row + winner + per-token outcomes derived from
    the competition response.

    Fetches all orders via POST /orders/by_uids (128 UIDs per request, concurrent
    batches) — eliminates the former MAX_ORDERS cap that blocked all Arbitrum
    auctions (~1200 orders each).

    Returns ``"ok"`` on success and ``"rate_limited"`` if CoW API returned HTTP 429.
    """
    try:
        comp = await _cow_get(f"{BASE_URL}/solver_competition/latest")
    except RateLimitedError as exc:
        log.warning("rate_limited", extra={"url": str(exc)})
        return "rate_limited"

    if comp is None:
        return "ok"

    auction_id = int(str(comp["auctionId"]))
    if auction_id in seen:
        return "ok"
    seen.add(auction_id)

    auction_data: dict[str, Any] = comp.get("auction") or {}
    uids: list[str] = [str(u) for u in (auction_data.get("orders") or [])]
    token_prices: dict[str, str] = {
        str(k): str(v) for k, v in (auction_data.get("prices") or {}).items()
    }

    tokens = {
        addr: {
            "decimals": 18,
            "referencePrice": price,
            "availableBalance": "0",
            "trusted": True,
        }
        for addr, price in token_prices.items()
    }
    auction_payload: dict[str, Any] = {
        "id": str(auction_id),
        "tokens": tokens,
        "orders": [],
        "liquidity": [],
        "effectiveGasPrice": "0",
        "deadline": None,
    }

    our_solution = None

    if uids:
        try:
            orders = await _fetch_orders_by_uids(uids)
        except RateLimitedError as exc:
            log.warning("rate_limited", extra={"url": str(exc)})
            # Persist what we have (metadata only) and record as skipped
            await persist_winner_and_outcomes_safe(
                auction_id=auction_id,
                raw_competition=comp,
                auction_payload=auction_payload,
                our_solution=None,
            )
            await persist_skipped_auction_safe(
                auction_id=auction_id,
                auction_payload=auction_payload,
                raw_competition=comp,
                n_orders=len(uids),
            )
            return "rate_limited"
        except Exception as exc:
            log.warning(
                "order_fetch_failed",
                extra={"auction_id": auction_id, "n_uids": len(uids), "error": str(exc)},
            )
            orders = []

        auction_payload["orders"] = orders

        if orders:
            try:
                resp = await solver.post(
                    f"{SOLVER_URL}/solve", json=auction_payload, timeout=SOLVER_TIMEOUT
                )
                if resp.status_code == 200:
                    our_solution = resp.json()
            except Exception as exc:
                log.warning(
                    "solver_call_failed",
                    extra={"auction_id": auction_id, "error": str(exc)},
                )

    log.info(
        "auction_processed",
        extra={
            "auction_id": auction_id,
            "n_uids": len(uids),
            "orders_fetched": len(auction_payload["orders"]),
            "solved": our_solution is not None,
        },
    )

    # Always persist winner + token outcomes from the competition response
    await persist_winner_and_outcomes_safe(
        auction_id=auction_id,
        raw_competition=comp,
        auction_payload=auction_payload,
        our_solution=our_solution,
    )

    # JSONL backup
    solutions: list[dict[str, Any]] = list(comp.get("solutions") or [])
    winner = next(
        (s for s in solutions if s.get("isWinner") or s.get("ranking") == 1), None
    )
    entry = {
        "auction_id": str(auction_id),
        "timestamp": datetime.now(UTC).isoformat(),
        "n_uids": len(uids),
        "orders_fetched": len(auction_payload["orders"]),
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

    touch_liveness(LIVENESS_PATH)
    return "ok"


async def main() -> None:
    seen: set[int] = set()
    backoff = Backoff(base=60.0, cap=600.0)
    async with httpx.AsyncClient() as solver:
        while True:
            try:
                result = await poll_once(solver, seen)
                if result == "rate_limited":
                    backoff.on_rate_limit()
                    delay = backoff.current()
                    log.warning(
                        "backoff_extended",
                        extra={"current": round(delay, 1), "reason": "rate_limited"},
                    )
                else:
                    backoff.on_success()
                    delay = backoff.current()
            except Exception:
                log.exception("poll_error")
                backoff.on_rate_limit()
                delay = backoff.current()
                log.warning(
                    "backoff_extended",
                    extra={"current": round(delay, 1), "reason": "exception"},
                )
            # Touch liveness AFTER the cycle — proves we got through poll_once
            # without infinite-hanging.
            touch_liveness(LIVENESS_PATH)
            await asyncio.sleep(delay)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main())
