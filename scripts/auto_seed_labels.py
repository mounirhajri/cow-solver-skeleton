"""Auto-seed scam / legit labels from Honeypot.is for the RF classifier.

In Shadow mode TokenOutcome.caused_revert never becomes True organically.
This script bootstraps the classifier's binary-path with external labels:
calls Honeypot.is for each token that has features but no outcome label,
inserts caused_revert=True for honeypots and appeared_in_winner=True for
verified-clean tokens.

Idempotent on (token_address): re-runs skip tokens that already have any
outcome label. Rate-limited to stay within Honeypot.is free-tier limits.

Usage:
    python -m scripts.auto_seed_labels [--batch-size 100] [--max-concurrent 5]
                                       [--dry-run] [--chain-id 42161]

## Scheduled execution

Run weekly via cron (on the host, not in the container):

    0 5 * * 0 docker exec cow-solver python -m scripts.auto_seed_labels \
        --batch-size 200 >> /var/log/cow-solver-auto-seed.log 2>&1

This drips ~200 tokens/week into the classifier's label set. Combined
with `extract_features.py` (which populates `token_features`) and
`train_classifier.py` (which auto-promotes if AUC improves), the RF
pipeline becomes self-improving.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.log import get_logger
from src.persistence.db import get_session_factory
from src.persistence.models import ShadowAuction, TokenFeatures, TokenOutcome

log = get_logger(__name__)

HONEYPOT_URL = "https://api.honeypot.is/v2/IsHoneypot"
USER_AGENT = "cow-solver-classifier/1.0"
HTTP_TIMEOUT_S = 5.0
MAX_RETRY_ATTEMPTS = 3


@dataclass
class SeedResult:
    n_checked: int = 0
    n_scam: int = 0
    n_legit: int = 0
    n_skipped: int = 0
    n_errors: int = 0


@dataclass
class TokenVerdict:
    """Classification verdict from Honeypot.is.

    kind in {"scam", "legit", "skip", "error"}; only "scam"/"legit" → DB write.
    """

    address: str
    kind: str


async def _fetch_honeypot(
    client: httpx.AsyncClient, token_address: str, chain_id: int
) -> TokenVerdict:
    """Call Honeypot.is for one token with exponential backoff on 429/5xx.

    Never raises — converts any failure into a TokenVerdict(kind="error").
    """
    # Exponential backoff: 1s, 2s, 4s. We retry only on 429 / 5xx — other
    # errors (timeouts, transport) are short-circuited because the API is
    # cheap to skip; a future run will pick the token up again.
    backoff = 1.0
    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(
                HONEYPOT_URL,
                params={"address": token_address, "chainID": chain_id},
            )
        except httpx.TimeoutException:
            log.debug("honeypot_timeout", token=token_address, attempt=attempt)
            return TokenVerdict(token_address, "error")
        except httpx.HTTPError as exc:
            log.warning("honeypot_network_error", token=token_address, error=str(exc))
            return TokenVerdict(token_address, "error")

        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if attempt == MAX_RETRY_ATTEMPTS:
                log.warning(
                    "honeypot_retry_exhausted",
                    token=token_address,
                    status=resp.status_code,
                )
                return TokenVerdict(token_address, "error")
            await asyncio.sleep(backoff)
            backoff *= 2
            continue

        if resp.status_code != 200:
            log.warning(
                "honeypot_unexpected_status",
                token=token_address,
                status=resp.status_code,
            )
            return TokenVerdict(token_address, "error")

        try:
            data: dict[str, Any] = resp.json()
        except ValueError as exc:
            log.warning("honeypot_bad_json", token=token_address, error=str(exc))
            return TokenVerdict(token_address, "error")

        return _classify(token_address, data)

    return TokenVerdict(token_address, "error")


def _classify(token_address: str, data: dict[str, Any]) -> TokenVerdict:
    """Map a Honeypot.is JSON response to a verdict.

    Missing `honeypotResult` or failed simulation → skip (we don't know).
    """
    honeypot_result = data.get("honeypotResult")
    simulation_success = data.get("simulationSuccess")
    if not isinstance(honeypot_result, dict):
        return TokenVerdict(token_address, "skip")
    is_honeypot = honeypot_result.get("isHoneypot")
    if is_honeypot is True:
        # Honeypots are flagged even if simulation didn't fully succeed —
        # the detection is authoritative for the True case.
        return TokenVerdict(token_address, "scam")
    if is_honeypot is False and simulation_success is True:
        return TokenVerdict(token_address, "legit")
    return TokenVerdict(token_address, "skip")


async def _fetch_unlabeled_tokens(
    session: AsyncSession, batch_size: int
) -> list[str]:
    """Tokens that have features but no token_outcomes row yet."""
    stmt = (
        select(TokenFeatures.token_address)
        .outerjoin(
            TokenOutcome,
            TokenOutcome.token_address == TokenFeatures.token_address,
        )
        .where(TokenOutcome.token_address.is_(None))
        .limit(batch_size)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def _get_anchor_auction_id(session: AsyncSession) -> int | None:
    """Pick the newest ShadowAuction.auction_id as FK anchor for synthetic labels.

    TokenOutcome.auction_id is a required FK, but a Honeypot.is verdict is
    *independent* of any specific auction — it's a property of the token.
    Using the most-recent auction lets the row exist without inventing a
    fake auction. If no auctions exist yet, return None and skip the run.
    """
    stmt = select(ShadowAuction.auction_id).order_by(ShadowAuction.auction_id.desc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _process_token(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    token_address: str,
    chain_id: int,
) -> TokenVerdict:
    async with semaphore:
        return await _fetch_honeypot(client, token_address, chain_id)


async def _seed(
    *,
    batch_size: int,
    max_concurrent: int,
    dry_run: bool,
    chain_id: int,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> SeedResult:
    started = time.monotonic()
    result = SeedResult()
    factory = session_factory or get_session_factory()

    async with factory() as session:
        tokens = await _fetch_unlabeled_tokens(session, batch_size)
        anchor_auction_id = await _get_anchor_auction_id(session)

    if not tokens:
        log.info("honeypot_seed_no_tokens")
        return result

    if anchor_auction_id is None and not dry_run:
        # Without any ShadowAuction row the FK can't be satisfied. A future
        # run after the poller has captured at least one auction will work.
        log.warning("honeypot_seed_no_anchor_auction", n_unlabeled=len(tokens))
        return result

    semaphore = asyncio.Semaphore(max_concurrent)
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=HTTP_TIMEOUT_S,
    ) as client:
        tasks = [
            _process_token(client, semaphore, addr, chain_id) for addr in tokens
        ]
        verdicts = await asyncio.gather(*tasks)

    to_insert: list[TokenOutcome] = []
    for v in verdicts:
        result.n_checked += 1
        if v.kind == "scam":
            result.n_scam += 1
            to_insert.append(
                TokenOutcome(
                    token_address=v.address,
                    auction_id=anchor_auction_id,
                    appeared_in_winner=False,
                    appeared_in_ours=False,
                    caused_revert=True,
                )
            )
        elif v.kind == "legit":
            result.n_legit += 1
            to_insert.append(
                TokenOutcome(
                    token_address=v.address,
                    auction_id=anchor_auction_id,
                    appeared_in_winner=True,
                    appeared_in_ours=False,
                    caused_revert=False,
                )
            )
        elif v.kind == "skip":
            result.n_skipped += 1
        else:
            result.n_errors += 1

    if to_insert and not dry_run:
        async with factory() as session:
            session.add_all(to_insert)
            await session.commit()

    elapsed_s = round(time.monotonic() - started, 2)
    log.info(
        "honeypot_seed_done",
        n_checked=result.n_checked,
        n_scam=result.n_scam,
        n_legit=result.n_legit,
        n_skipped=result.n_skipped,
        n_errors=result.n_errors,
        elapsed_s=elapsed_s,
        dry_run=dry_run,
    )
    return result


async def main_async(
    batch_size: int, max_concurrent: int, dry_run: bool, chain_id: int
) -> SeedResult:
    try:
        return await _seed(
            batch_size=batch_size,
            max_concurrent=max_concurrent,
            dry_run=dry_run,
            chain_id=chain_id,
        )
    except Exception as exc:  # never raise from a cron entry-point
        log.error("honeypot_seed_unhandled", error=str(exc), error_type=type(exc).__name__)
        return SeedResult()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-seed scam/legit labels from Honeypot.is.",
    )
    parser.add_argument("--batch-size", type=int, default=100,
                        help="Number of tokens to process this run (default: 100).")
    parser.add_argument("--max-concurrent", type=int, default=5,
                        help="Max parallel Honeypot.is calls (default: 5).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print verdicts but skip DB writes.")
    parser.add_argument("--chain-id", type=int, default=42161,
                        help="EVM chain ID (default: 42161 = Arbitrum One).")
    args = parser.parse_args()
    asyncio.run(
        main_async(
            batch_size=args.batch_size,
            max_concurrent=args.max_concurrent,
            dry_run=args.dry_run,
            chain_id=args.chain_id,
        )
    )


if __name__ == "__main__":
    main()
