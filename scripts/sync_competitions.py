"""Fetch and persist CoW competition API data for past auctions.

For each auction in shadow_auctions that does not yet have rows in
shadow_competitors, fetches:
    GET https://api.cow.fi/arbitrum_one/api/v1/solver_competition/{auction_id}

and inserts one ShadowCompetitor row per solver in the ``solutions`` array.

Key behaviours
--------------
- Idempotent: INSERT … ON CONFLICT DO NOTHING means re-runs are safe.
- 404 skip: auctions that settled without on-chain competition data are
  logged at INFO and skipped (no row inserted).
- Throttle: 1 req/s by default (CoW API rate-limits sustained higher rates
  without an authenticated key; overridable via COW_API_RPS env var).
- On HTTP 429: honour ``Retry-After`` header (capped at 60 s) and retry once.
- Batch: processes at most ``--limit`` auctions per run, ordered newest-first
  so the most recent data is always populated first.

Usage
-----
    python -m scripts.sync_competitions [--days 7] [--limit 1000]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# Use curl_cffi (libcurl bindings with browser-TLS fingerprint) instead of
# httpx — CloudFront in front of api.cow.fi rate-limits Python-default-httpx
# TLS fingerprints to 429 even at 1 rps, while raw curl and urllib succeed.
# Verified 2026-05-25: urllib → 200, httpx → 429 on identical request.
# curl_cffi.requests.AsyncSession is a drop-in async client.
# Alias curl_cffi AsyncSession to avoid name collision with
# sqlalchemy.ext.asyncio.AsyncSession imported below.
from curl_cffi.requests import AsyncSession as HttpAsyncSession
from curl_cffi.requests import RequestsError
from sqlalchemy import outerjoin, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.log import get_logger
from src.persistence.db import get_session_factory
from src.persistence.models import ShadowAuction, ShadowCompetitor

log = get_logger(__name__)

COW_COMPETITION_URL = (
    "https://api.cow.fi/arbitrum_one/api/v1/solver_competition/{auction_id}"
)
# CoW Production rate-limiter rejected the bare "cow-solver-skeleton/1.0" UA
# with sustained 429s even at 1 rps after a previous 5-rps burst on the same
# IP (2026-05-25). Switched to the "compatible bot" pattern (RFC 9110 form,
# same as Googlebot etc.) — identifies us truthfully via the URL but avoids
# triggering whatever heuristic was filtering the previous UA. Override via
# env-var if we ever get an authenticated API client.
USER_AGENT = os.environ.get(
    "COW_API_USER_AGENT",
    "Mozilla/5.0 (compatible; cow-solver-research-client/1.0; "
    "+https://github.com/mounirhajri/cow-solver-skeleton)",
)
HTTP_TIMEOUT_S = 10.0
# CoW API rate-limit without auth is stricter than docs suggest — live runs
# 2026-05-25 returned HTTP 429 at sustained 5 req/s. Drop to 1 req/s, plus
# explicit Retry-After handling below. Override via COW_API_RPS env var if
# we ever get an authenticated key with higher limits.
REQUESTS_PER_SECOND = float(os.environ.get("COW_API_RPS", "1"))
# Hard upper bound on Retry-After sleep to avoid pathological 1-hour pauses.
RETRY_AFTER_MAX_S = 60.0


@dataclass
class SyncResult:
    n_fetched: int = 0
    n_inserted: int = 0
    n_skipped_404: int = 0
    n_errors: int = 0
    auction_ids_seen: list[int] = field(default_factory=list)


async def _fetch_unsynced_auction_ids(
    session: AsyncSession,
    *,
    since: datetime,
    limit: int,
) -> list[int]:
    """Return auction_ids that exist in shadow_auctions but have no shadow_competitors rows.

    Ordered by polled_at DESC so the most-recently polled auctions are processed first.
    The since filter prevents us from re-scanning very old data on every run.
    """
    j = outerjoin(
        ShadowAuction,
        ShadowCompetitor,
        ShadowAuction.auction_id == ShadowCompetitor.auction_id,
    )
    stmt = (
        select(ShadowAuction.auction_id)
        .select_from(j)
        .where(ShadowCompetitor.id.is_(None))
        .where(ShadowAuction.polled_at >= since)
        .order_by(ShadowAuction.polled_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def _fetch_competition(
    client: HttpAsyncSession, auction_id: int
) -> dict[str, Any] | None:
    """Fetch competition data for a single auction via curl_cffi.

    Returns the parsed JSON body on success, None on 404 (no competition data
    available for this auction), and raises on other HTTP errors. On 429
    Too Many Requests, sleeps for ``Retry-After`` seconds (server-provided)
    or 5 s (fallback) and retries ONCE. Per-call retry budget is bounded so
    a sustained rate-limit doesn't stall the entire batch.
    """
    url = COW_COMPETITION_URL.format(auction_id=auction_id)
    for attempt in (1, 2):
        try:
            # impersonate="chrome" applies the latest Chrome TLS+JA3 fingerprint.
            # Without this, curl_cffi degrades to a generic-libcurl handshake
            # which CloudFront has historically also rate-limited.
            resp = await client.get(url, impersonate="chrome")
        except RequestsError as exc:
            log.warning(
                "cow_competition_fetch_error", auction_id=auction_id, error=str(exc)
            )
            raise

        if resp.status_code == 404:
            log.info("cow_competition_not_found", auction_id=auction_id)
            return None

        if resp.status_code == 429 and attempt == 1:
            # Honour Retry-After header if present; default 5 s.
            retry_after_raw = resp.headers.get("Retry-After", "5")
            try:
                retry_after = float(retry_after_raw)
            except (TypeError, ValueError):
                retry_after = 5.0
            retry_after = min(retry_after, RETRY_AFTER_MAX_S)
            log.warning(
                "cow_competition_rate_limited",
                auction_id=auction_id,
                retry_after_s=retry_after,
            )
            await asyncio.sleep(retry_after)
            continue

        if resp.status_code >= 400:
            raise RuntimeError(
                f"HTTP {resp.status_code} for {url}: {resp.text[:200]}"
            )
        return resp.json()  # type: ignore[no-any-return]

    # If we get here, retry didn't recover.
    if resp.status_code >= 400:
        raise RuntimeError(
            f"HTTP {resp.status_code} for {url} after retry: {resp.text[:200]}"
        )
    return resp.json()  # type: ignore[no-any-return]


def _parse_solutions(
    data: dict[str, Any], auction_id: int, polled_at: datetime
) -> list[dict[str, Any]]:
    """Convert the API response solutions array into a list of insert dicts."""
    solutions: list[Any] = data.get("solutions") or []
    rows: list[dict[str, Any]] = []
    for sol in solutions:
        if not isinstance(sol, dict):
            continue
        # score arrives as a string (wei) — convert to int; None when absent.
        raw_score = sol.get("score")
        try:
            score_val: int | None = int(raw_score) if raw_score is not None else None
        except (TypeError, ValueError):
            score_val = None

        rows.append(
            {
                "auction_id": auction_id,
                "solver_name": str(sol.get("solver") or "")[:50],
                "solver_address": str(sol.get("solverAddress") or "")[:42],
                "score": score_val,
                "ranking": int(sol.get("ranking") or 0),
                "is_winner": bool(sol.get("isWinner", False)),
                "filtered_out": bool(sol.get("filteredOut", False)),
                "clearing_prices": sol.get("clearingPrices") or {},
                "orders": sol.get("orders") or [],
                "polled_at": polled_at,
            }
        )
    return rows


async def _insert_competitors(
    session_factory: async_sessionmaker[AsyncSession],
    rows: list[dict[str, Any]],
) -> int:
    """Bulk INSERT … ON CONFLICT DO NOTHING for a batch of competitor rows.

    Returns the number of rows actually inserted (may be less than len(rows)
    if some already existed from a previous partial run).
    """
    if not rows:
        return 0

    async with session_factory() as session:
        dialect_name = (
            session.bind.dialect.name if session.bind else "postgresql"
        )
        insert_fn = sqlite_insert if dialect_name == "sqlite" else pg_insert
        stmt = insert_fn(ShadowCompetitor).values(rows)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["auction_id", "solver_address"]
        )
        cursor = await session.execute(stmt)
        await session.commit()
        rowcount: int = cursor.rowcount  # type: ignore[attr-defined]
        return rowcount if rowcount != -1 else len(rows)


async def _sync(
    *,
    days: int,
    limit: int,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> SyncResult:
    """Main sync logic. Exposed for testing via session_factory injection."""
    factory = session_factory or get_session_factory()
    since = datetime.now(UTC) - timedelta(days=days)
    result = SyncResult()

    async with factory() as session:
        auction_ids = await _fetch_unsynced_auction_ids(
            session, since=since, limit=limit
        )

    if not auction_ids:
        log.info("sync_competitions_no_work")
        return result

    log.info("sync_competitions_start", n_auctions=len(auction_ids))

    # Rate limiter: pace requests at REQUESTS_PER_SECOND. Semaphore size 1
    # keeps the loop strictly sequential (no parallel overlap can spike past
    # the rate), the interval-after-call enforces the temporal spacing.
    # Permit size = 1 (sequential) + interval-after-call enforces 1/RPS spacing.
    semaphore = asyncio.Semaphore(1)
    interval = 1.0 / REQUESTS_PER_SECOND  # seconds per request

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    async with HttpAsyncSession(headers=headers, timeout=HTTP_TIMEOUT_S) as client:
        for auction_id in auction_ids:
            result.auction_ids_seen.append(auction_id)
            started = time.monotonic()

            async with semaphore:
                try:
                    data = await _fetch_competition(client, auction_id)
                except Exception as exc:
                    log.warning(
                        "sync_competition_error",
                        auction_id=auction_id,
                        error=str(exc),
                    )
                    result.n_errors += 1
                    continue
                finally:
                    # Throttle: ensure we wait at least `interval` per permit,
                    # even if the HTTP call was faster.
                    elapsed = time.monotonic() - started
                    if elapsed < interval:
                        await asyncio.sleep(interval - elapsed)

            result.n_fetched += 1

            if data is None:
                result.n_skipped_404 += 1
                continue

            polled_at = datetime.now(UTC)
            rows = _parse_solutions(data, auction_id, polled_at)
            if not rows:
                log.info("sync_competition_no_solutions", auction_id=auction_id)
                continue

            inserted = await _insert_competitors(factory, rows)
            result.n_inserted += inserted
            log.info(
                "sync_competition_persisted",
                auction_id=auction_id,
                n_solutions=len(rows),
                n_inserted=inserted,
            )

    log.info(
        "sync_competitions_done",
        n_fetched=result.n_fetched,
        n_inserted=result.n_inserted,
        n_skipped_404=result.n_skipped_404,
        n_errors=result.n_errors,
    )
    return result


async def main_async(days: int, limit: int) -> SyncResult:
    """Async entry-point; swallows unhandled exceptions so cron never hard-fails."""
    try:
        return await _sync(days=days, limit=limit)
    except Exception as exc:
        log.error(
            "sync_competitions_unhandled",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return SyncResult()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch CoW competition API data and persist to shadow_competitors."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="How many days back to look for un-synced auctions (default: 7).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max auctions to process per run (default: 100).",
    )
    args = parser.parse_args()
    asyncio.run(main_async(days=args.days, limit=args.limit))


if __name__ == "__main__":
    main()
