"""Auto-seed scam / legit labels from GoPlus Security for the RF classifier.

In Shadow mode TokenOutcome.caused_revert never becomes True organically.
This script bootstraps the classifier's binary-path with external labels:
calls GoPlus Security's token-security endpoint for tokens that have
features but no outcome label, then inserts caused_revert=True for scams
and appeared_in_winner=True for verified-clean tokens.

## Why GoPlus instead of Honeypot.is

The previous implementation used Honeypot.is, but that API does not
support Arbitrum (`chainID=42161` returns HTTP 400 "Invalid chain").
GoPlus Security supports Arbitrum, requires no auth, and — crucially —
accepts batch requests via comma-separated `contract_addresses`, which
reduces our per-token API-call count by ~50x and unlocks much higher
weekly throughput within free-tier limits.

Idempotent on (token_address): re-runs skip tokens that already have any
outcome label. Rate-limited via a semaphore over *batched* requests.

## Authentication (optional, recommended)

Anonymous tier blocks aggressively (~3 req/s ceiling, JSON code=4029). Setting
both ``GOPLUS_APP_KEY`` and ``GOPLUS_APP_SECRET`` as env vars unlocks the
authenticated tier (~10x higher allowance) — the script exchanges them for a
short-lived access_token on startup and adds ``Authorization: Bearer <token>``
to every request. If either is missing or the token exchange fails, the run
continues anonymously rather than hard-failing.

Register at https://gopluslabs.io to get a free key pair.

Usage:
    python -m scripts.auto_seed_labels [--batch-size 100] [--max-concurrent 3]
                                       [--api-batch-size 50]
                                       [--dry-run] [--chain-id 42161]

## Scheduled execution

Run weekly via cron (on the host, not in the container):

    0 5 * * 0 docker exec cow-solver python -m scripts.auto_seed_labels \
        --batch-size 500 >> /var/log/cow-solver-auto-seed.log 2>&1

500 tokens/week is comfortably within GoPlus free-tier rates: with
--api-batch-size 50 that's only ~10 HTTP requests per run.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.log import get_logger
from src.persistence.db import get_session_factory
from src.persistence.models import ShadowAuction, TokenFeatures, TokenOutcome

log = get_logger(__name__)

GOPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
GOPLUS_TOKEN_URL = "https://api.gopluslabs.io/api/v1/token"
USER_AGENT = "cow-solver-classifier/1.0"
HTTP_TIMEOUT_S = 5.0
MAX_RETRY_ATTEMPTS = 3
# GoPlus's `contract_addresses=A,B,C` syntax LOOKS like it should batch but
# empirically returns only the first address's result (verified 2026-05-24 with
# a 5-address batch on Arbitrum: code=1, but only result_map[A] populated).
# So we keep the chunking infrastructure but default to single-address calls.
# `--max-concurrent` is what gives us throughput now — at 3 concurrent + ~200 ms
# per call, 129 tokens process in ~9 s, well within free-tier rate limits.
DEFAULT_API_BATCH_SIZE = 1


@dataclass
class SeedResult:
    n_checked: int = 0
    n_scam: int = 0
    n_legit: int = 0
    n_skipped: int = 0
    n_errors: int = 0
    n_features_written: int = 0


# GoPlus security fields we persist into `token_features`.  Keep in sync with
# RAW_FEATURE_COLUMNS in edge/classifier/feature_engineering.py and the
# 3b9f1c2e0a8e Alembic migration.
_GOPLUS_BOOL_FEATURES: tuple[str, ...] = (
    "is_proxy",
    "is_mintable",
    "can_take_back_ownership",
    "hidden_owner",
    "slippage_modifiable",
    "transfer_pausable",
    "owner_change_balance",
    "external_call",
    "honeypot_with_same_creator",
    "anti_whale_modifiable",
)
_GOPLUS_FRACTION_FEATURES: tuple[str, ...] = (
    "creator_percent",
    "buy_tax",
    "sell_tax",
)


@dataclass
class TokenVerdict:
    """Classification verdict from GoPlus.

    kind in {"scam", "legit", "skip", "error"}; only "scam"/"legit" → DB write.
    """

    address: str
    kind: str


def _parse_float(value: Any) -> float:
    """Parse a GoPlus tax field. Empty/missing → 0.0 (not a red flag)."""
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        # Malformed taxes are an unknown signal — treat as 0 and let other
        # fields (is_honeypot, is_open_source) drive the verdict.
        return 0.0


def _extract_security_features(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extract the GoPlus security features we persist into token_features.

    Returns None when the entry has no usable signal at all — in that case
    we skip the UPSERT entirely so we don't blank out previously-collected
    data for the token.

    Each boolean field is normalised to True/False/None (None when missing
    or non-"0"/"1").  Each fractional field is normalised to a float clipped
    to [0, 1]; >1 values are GoPlus parser glitches and the model's clip
    handles them defensively anyway.
    """
    if not isinstance(entry, dict):
        return None

    out: dict[str, Any] = {}

    def _as_bool(v: Any) -> bool | None:
        if v == "1":
            return True
        if v == "0":
            return False
        return None

    def _as_fraction(v: Any) -> float | None:
        if v is None or v == "":
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if f < 0.0:
            return 0.0
        if f > 1.0:
            return 1.0
        return f

    for col in _GOPLUS_BOOL_FEATURES:
        out[col] = _as_bool(entry.get(col))
    for col in _GOPLUS_FRACTION_FEATURES:
        out[col] = _as_fraction(entry.get(col))

    # If literally every field is None, there's nothing worth UPSERTing —
    # the response had only an outer "code/result" shell with no actual
    # security analysis (typical for tokens GoPlus doesn't know).
    if all(v is None for v in out.values()):
        return None
    return out


def _classify_one(token_address: str, entry: dict[str, Any] | None) -> TokenVerdict:
    """Map a single GoPlus result entry to a verdict.

    Asymmetric rule: ANY scam-indicator → scam, ALL legit-indicators → legit.
    Rationale: a token mis-labeled scam just gets filtered out of routing,
    but a token mis-labeled legit pollutes the classifier with spam features.
    So we err on the side of "skip" when uncertain and require unanimous
    evidence for "legit".
    """
    if not isinstance(entry, dict):
        return TokenVerdict(token_address, "skip")

    is_honeypot = entry.get("is_honeypot")
    cannot_buy = entry.get("cannot_buy")
    cannot_sell_all = entry.get("cannot_sell_all")
    is_open_source = entry.get("is_open_source")

    # Any one of these is a hard scam signal.
    if is_honeypot == "1" or cannot_buy == "1" or cannot_sell_all == "1":
        return TokenVerdict(token_address, "scam")

    # Required fields for a confident "legit" verdict.
    if is_honeypot != "0":
        # Missing / unknown honeypot status — can't promote to legit.
        return TokenVerdict(token_address, "skip")
    if is_open_source != "1":
        # Closed-source contracts could hide arbitrary behavior; we don't
        # have enough signal to call them legit.
        return TokenVerdict(token_address, "skip")

    # cannot_buy / cannot_sell_all: only "1" is bad; "0", missing, or empty
    # are all acceptable for legit (we already rejected "1" above).
    if cannot_buy not in (None, "", "0"):
        return TokenVerdict(token_address, "skip")
    if cannot_sell_all not in (None, "", "0"):
        return TokenVerdict(token_address, "skip")

    buy_tax = _parse_float(entry.get("buy_tax"))
    sell_tax = _parse_float(entry.get("sell_tax"))
    if buy_tax >= 0.05 or sell_tax >= 0.05:
        # >=5% tax is too predatory for the solver to treat as legit.
        return TokenVerdict(token_address, "skip")

    return TokenVerdict(token_address, "legit")


def _classify_batch(
    addresses: list[str], data: dict[str, Any]
) -> tuple[list[TokenVerdict], dict[str, dict[str, Any]]]:
    """Classify all tokens in a batch from one GoPlus response.

    Returns ``(verdicts, features_by_addr)``.  ``features_by_addr`` only
    contains entries for tokens where GoPlus returned usable security
    signals — caller uses that to UPSERT the per-token `token_features`
    rows alongside writing the outcome label.

    Addresses missing from `result` are returned as "skip" — GoPlus
    couldn't analyze them this run; a future run will retry.
    """
    if data.get("code") != 1:
        log.warning("goplus_response_not_ok", code=data.get("code"),
                    message=data.get("message"))
        return [TokenVerdict(a, "skip") for a in addresses], {}

    result_map = data.get("result") or {}
    if not isinstance(result_map, dict):
        return [TokenVerdict(a, "skip") for a in addresses], {}

    # GoPlus lower-cases the address keys in the response — normalize ours
    # to match so we can look up by either input casing.
    lower_map = {k.lower(): v for k, v in result_map.items()}

    verdicts: list[TokenVerdict] = []
    features: dict[str, dict[str, Any]] = {}
    for addr in addresses:
        entry = lower_map.get(addr.lower())
        v = _classify_one(addr, entry if isinstance(entry, dict) else None)
        log.info("goplus_verdict", token_address=addr, verdict=v.kind)
        verdicts.append(v)
        sec = _extract_security_features(entry if isinstance(entry, dict) else None)
        if sec is not None:
            features[addr] = sec
    return verdicts, features


async def _fetch_access_token(
    client: httpx.AsyncClient, app_key: str, app_secret: str
) -> str | None:
    """Exchange app_key + app_secret for a short-lived access_token.

    GoPlus signing rule (verified 2026-05-24 against
    docs.gopluslabs.io/reference/getaccesstokenusingpost):
        sign = sha1(app_key + str(time_seconds) + app_secret) → hex

    Authenticated tier raises rate-limits ~10x vs anonymous (~30/min instead
    of ~3/s blocking ceiling). Token TTL is ~1 hour, plenty for a single
    seed run. Returns None on any failure — caller should fall back to
    anonymous mode rather than hard-fail the whole run.
    """
    ts = str(int(time.time()))
    sign = hashlib.sha1((app_key + ts + app_secret).encode("utf-8")).hexdigest()
    try:
        resp = await client.post(
            GOPLUS_TOKEN_URL,
            json={"app_key": app_key, "time": int(ts), "sign": sign},
        )
    except httpx.HTTPError as exc:
        log.warning("goplus_token_network_error", error=str(exc))
        return None

    if resp.status_code != 200:
        log.warning("goplus_token_bad_status", status=resp.status_code)
        return None

    try:
        body: dict[str, Any] = resp.json()
    except ValueError:
        log.warning("goplus_token_bad_json")
        return None

    if body.get("code") != 1:
        log.warning("goplus_token_rejected", code=body.get("code"),
                    message=body.get("message"))
        return None

    result = body.get("result") or {}
    token = result.get("access_token") if isinstance(result, dict) else None
    if not isinstance(token, str) or not token:
        log.warning("goplus_token_missing_in_response")
        return None
    log.info("goplus_token_acquired", expires_in=result.get("expires_in"))
    return token


async def _fetch_goplus_batch(
    client: httpx.AsyncClient, addresses: list[str], chain_id: int
) -> tuple[list[TokenVerdict], dict[str, dict[str, Any]]]:
    """Call GoPlus for a batch of tokens with exponential backoff on 429/5xx.

    Returns ``(verdicts, features_by_addr)``.  Never raises — converts any
    failure into a list of "error" verdicts (empty features dict).
    """
    if not addresses:
        return [], {}

    url = GOPLUS_URL.format(chain_id=chain_id)
    params = {"contract_addresses": ",".join(addresses)}
    err_pair: tuple[list[TokenVerdict], dict[str, dict[str, Any]]] = (
        [TokenVerdict(a, "error") for a in addresses], {}
    )

    # GoPlus signals rate-limit as HTTP 200 + JSON `code=4029` (verified live
    # 2026-05-24 — free tier blocks aggressively at ~3 req/s). Treat that
    # JSON-level code identically to a 429 / 5xx for retry purposes.
    # Backoff is generous (5s/15s/45s) because the free-tier block lasts ~30-60s.
    backoff = 5.0
    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(url, params=params)
        except httpx.TimeoutException:
            log.debug("goplus_timeout", n=len(addresses), attempt=attempt)
            return err_pair
        except httpx.HTTPError as exc:
            log.warning("goplus_network_error", n=len(addresses), error=str(exc))
            return err_pair

        is_rate_limited = (
            resp.status_code == 429
            or 500 <= resp.status_code < 600
        )
        json_code: int | None = None
        if not is_rate_limited and resp.status_code == 200:
            try:
                data_peek: dict[str, Any] = resp.json()
                json_code = data_peek.get("code")
                if json_code == 4029:
                    is_rate_limited = True
            except ValueError:
                pass  # fall through to the regular JSON-parsing path

        if is_rate_limited:
            if attempt == MAX_RETRY_ATTEMPTS:
                log.warning(
                    "goplus_retry_exhausted",
                    n=len(addresses),
                    http_status=resp.status_code,
                    json_code=json_code,
                )
                return err_pair
            await asyncio.sleep(backoff)
            backoff *= 3
            continue

        if resp.status_code != 200:
            log.warning(
                "goplus_unexpected_status",
                n=len(addresses),
                status=resp.status_code,
            )
            return err_pair

        try:
            data: dict[str, Any] = resp.json()
        except ValueError as exc:
            log.warning("goplus_bad_json", n=len(addresses), error=str(exc))
            return err_pair

        return _classify_batch(addresses, data)

    return err_pair


async def _fetch_unlabeled_tokens(
    session: AsyncSession, batch_size: int
) -> list[str]:
    """Tokens that have features but no confirmed-scam outcome yet.

    The naive "no outcome row at all" check was wrong: every token that
    has appeared in any auction picks up an `appeared_in_winner=True`
    row via the normal solver flow (`persist_winner_and_outcomes`), so
    >99 % of tokens look "already labeled" even though we never asked
    an external service whether they're scams.

    The right question is "has this token been confirmed as scam?".
    If yes — skip, no need to re-query GoPlus. If no (could be unknown
    or legit-only) — re-query and let GoPlus give us a verdict.

    Trade-off: a token that GoPlus repeatedly classifies as legit will
    pick up one extra `appeared_in_winner=True` row per run, which is
    fine for the classifier (it aggregates per-token) and keeps the
    code simple. A confirmed-scam token never gets re-queried.
    """
    confirmed_scam = (
        select(TokenOutcome.token_address)
        .where(TokenOutcome.caused_revert.is_(True))
        .distinct()
    )
    stmt = (
        select(TokenFeatures.token_address)
        .where(TokenFeatures.token_address.not_in(confirmed_scam))
        .limit(batch_size)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def _get_anchor_auction_id(session: AsyncSession) -> int | None:
    """Pick the newest ShadowAuction.auction_id as FK anchor for synthetic labels.

    TokenOutcome.auction_id is a required FK, but a GoPlus verdict is
    *independent* of any specific auction — it's a property of the token.
    Using the most-recent auction lets the row exist without inventing a
    fake auction. If no auctions exist yet, return None and skip the run.
    """
    stmt = select(ShadowAuction.auction_id).order_by(ShadowAuction.auction_id.desc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _process_chunk(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    chunk: list[str],
    chain_id: int,
) -> tuple[list[TokenVerdict], dict[str, dict[str, Any]]]:
    async with semaphore:
        return await _fetch_goplus_batch(client, chunk, chain_id)


async def _seed(
    *,
    batch_size: int,
    max_concurrent: int,
    dry_run: bool,
    chain_id: int,
    api_batch_size: int = DEFAULT_API_BATCH_SIZE,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> SeedResult:
    started = time.monotonic()
    result = SeedResult()
    factory = session_factory or get_session_factory()

    async with factory() as session:
        tokens = await _fetch_unlabeled_tokens(session, batch_size)
        anchor_auction_id = await _get_anchor_auction_id(session)

    if not tokens:
        log.info("goplus_seed_no_tokens")
        return result

    if anchor_auction_id is None and not dry_run:
        # Without any ShadowAuction row the FK can't be satisfied. A future
        # run after the poller has captured at least one auction will work.
        log.warning("goplus_seed_no_anchor_auction", n_unlabeled=len(tokens))
        return result

    # Chunk into batched API calls. GoPlus has no published hard limit, but
    # ~50 addresses per call is a conservative middle ground that keeps
    # individual responses small enough to parse quickly.
    chunks = [
        tokens[i : i + api_batch_size] for i in range(0, len(tokens), api_batch_size)
    ]

    semaphore = asyncio.Semaphore(max_concurrent)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    async with httpx.AsyncClient(headers=headers, timeout=HTTP_TIMEOUT_S) as client:
        # Optional auth: if both env vars are set, exchange them for a
        # short-lived access token. Failure here is non-fatal — anonymous
        # mode still works, just with the harsher rate-limit.
        app_key = os.environ.get("GOPLUS_APP_KEY")
        app_secret = os.environ.get("GOPLUS_APP_SECRET")
        if app_key and app_secret:
            token = await _fetch_access_token(client, app_key, app_secret)
            if token is not None:
                # GoPlus expects the raw access_token in Authorization — NO
                # "Bearer " prefix. Verified live 2026-05-24: with prefix the
                # token_security endpoint returns code=4012 'signature
                # verification failure'; without prefix it returns code=1.
                # The docs page that suggests "Bearer" formatting refers to
                # a different (AVS) endpoint family.
                client.headers["Authorization"] = token

        tasks = [_process_chunk(client, semaphore, c, chain_id) for c in chunks]
        chunk_results = await asyncio.gather(*tasks)

    verdicts: list[TokenVerdict] = []
    features_by_addr: dict[str, dict[str, Any]] = {}
    for verdict_list, feature_map in chunk_results:
        verdicts.extend(verdict_list)
        features_by_addr.update(feature_map)

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

    # Persist security features via UPSERT.  Distinct from the outcome write
    # so feature collection survives the rare case where the outcome row hits
    # a unique-key conflict (it shouldn't, but if it does we keep the data).
    if features_by_addr and not dry_run:
        await _upsert_security_features(factory, features_by_addr)
        result.n_features_written = len(features_by_addr)

    elapsed_s = round(time.monotonic() - started, 2)
    log.info(
        "goplus_seed_done",
        n_checked=result.n_checked,
        n_scam=result.n_scam,
        n_legit=result.n_legit,
        n_skipped=result.n_skipped,
        n_errors=result.n_errors,
        n_features_written=result.n_features_written,
        n_chunks=len(chunks),
        elapsed_s=elapsed_s,
        dry_run=dry_run,
    )
    return result


async def _upsert_security_features(
    factory: async_sessionmaker[AsyncSession],
    features_by_addr: dict[str, dict[str, Any]],
) -> None:
    """UPSERT GoPlus security features into token_features.

    On conflict (token_address PK), each non-null field overwrites the
    existing column.  Null fields in the new data are NOT used to wipe
    existing values — preserves info from earlier runs / other feature
    sources.  Also bumps ``last_refreshed`` so we know when a token was
    last touched by the GoPlus seeder.

    Uses dialect-specific INSERT … ON CONFLICT for Postgres in prod and
    SQLite in tests; both have an equivalent ``on_conflict_do_update``
    syntax via the dialect-specific ``insert`` helpers.
    """
    now = datetime.now(UTC)
    async with factory() as session:
        dialect_name = session.bind.dialect.name if session.bind else "postgresql"
        insert_helper = sqlite_insert if dialect_name == "sqlite" else pg_insert
        for addr, sec in features_by_addr.items():
            insert_payload: dict[str, Any] = {
                "token_address": addr.lower(),
                "last_refreshed": now,
            }
            # Only carry non-null fields into the insert/update so
            # missing values don't blank out existing data.
            for k, v in sec.items():
                if v is not None:
                    insert_payload[k] = v

            stmt = insert_helper(TokenFeatures).values(**insert_payload)
            update_fields = {
                k: stmt.excluded[k]
                for k in insert_payload
                if k != "token_address"
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=[TokenFeatures.token_address],
                set_=update_fields,
            )
            await session.execute(stmt)
        await session.commit()


async def main_async(
    batch_size: int,
    max_concurrent: int,
    dry_run: bool,
    chain_id: int,
    api_batch_size: int = DEFAULT_API_BATCH_SIZE,
) -> SeedResult:
    try:
        return await _seed(
            batch_size=batch_size,
            max_concurrent=max_concurrent,
            dry_run=dry_run,
            chain_id=chain_id,
            api_batch_size=api_batch_size,
        )
    except Exception as exc:  # never raise from a cron entry-point
        log.error("goplus_seed_unhandled", error=str(exc), error_type=type(exc).__name__)
        return SeedResult()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-seed scam/legit labels from GoPlus Security.",
    )
    parser.add_argument("--batch-size", type=int, default=100,
                        help="Number of tokens to process this run (default: 100).")
    parser.add_argument("--max-concurrent", type=int, default=1,
                        help="Max parallel GoPlus calls (default: 1). "
                             "Free tier blocks at ~3 req/s; sequential is the safe default.")
    parser.add_argument("--api-batch-size", type=int, default=DEFAULT_API_BATCH_SIZE,
                        help="Addresses per GoPlus call (default: 1, batching not supported).")
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
            api_batch_size=args.api_batch_size,
        )
    )


if __name__ == "__main__":
    main()
