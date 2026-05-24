"""Tests for scripts/auto_seed_labels.py — GoPlus auto-seeding pipeline."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.auto_seed_labels import _seed, main_async
from src.persistence.models import ShadowAuction, TokenFeatures, TokenOutcome

# Match any chain_id path so individual tests don't have to thread it through.
GOPLUS_URL_RE = re.compile(
    r"https://api\.gopluslabs\.io/api/v1/token_security/\d+.*"
)

TOKEN_A = "0x" + "a" * 40
TOKEN_B = "0x" + "b" * 40
TOKEN_C = "0x" + "c" * 40
TOKEN_D = "0x" + "d" * 40


@pytest.fixture
async def session_factory():
    """In-memory sqlite session factory with the tables our script touches.

    BIGINT autoincrement isn't supported by sqlite, so we hand-roll DDL
    that uses INTEGER for ids — same approach as tests/test_shadow/test_persist.py.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE shadow_auctions (
                auction_id INTEGER PRIMARY KEY,
                chain TEXT NOT NULL DEFAULT 'arbitrum_one',
                polled_at TEXT NOT NULL,
                deadline TEXT,
                n_orders INTEGER NOT NULL,
                raw_competition TEXT NOT NULL,
                raw_auction TEXT NOT NULL
            )
        """))
        await conn.execute(text("""
            CREATE TABLE token_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_address TEXT NOT NULL,
                auction_id INTEGER NOT NULL REFERENCES shadow_auctions(auction_id),
                appeared_in_winner INTEGER NOT NULL,
                appeared_in_ours INTEGER NOT NULL,
                caused_revert INTEGER NOT NULL DEFAULT 0,
                observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        await conn.execute(text("""
            CREATE TABLE token_features (
                token_address TEXT PRIMARY KEY,
                decimals INTEGER,
                contract_verified INTEGER,
                has_transfer_tax INTEGER,
                bridge_canonical INTEGER,
                tvl_usd NUMERIC,
                volume_24h_usd NUMERIC,
                pool_count_v2 INTEGER,
                pool_count_v3 INTEGER,
                pool_count_camelot INTEGER,
                holder_count INTEGER,
                top10_concentration NUMERIC,
                age_blocks INTEGER,
                on_arbitrum_token_list INTEGER,
                on_coingecko INTEGER,
                last_refreshed TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_db(
    factory,
    *,
    tokens: list[str],
    with_outcomes: list[str] | None = None,
    with_scam_outcomes: list[str] | None = None,
) -> None:
    """Insert an anchor auction, token_features rows, and optional outcomes.

    `with_outcomes` adds plain `appeared_in_winner=True` rows (auction-derived).
    `with_scam_outcomes` adds `caused_revert=True` rows (confirmed scam) —
    these are the only ones that should make the auto-seeder skip a token.
    """
    with_outcomes = with_outcomes or []
    with_scam_outcomes = with_scam_outcomes or []
    async with factory() as session:
        session.add(ShadowAuction(
            auction_id=42, polled_at=datetime.now(UTC), n_orders=0,
            raw_competition={}, raw_auction={},
        ))
        for addr in tokens:
            session.add(TokenFeatures(token_address=addr, decimals=18))
        for addr in with_outcomes:
            session.add(TokenOutcome(
                token_address=addr, auction_id=42,
                appeared_in_winner=True, appeared_in_ours=False, caused_revert=False,
            ))
        for addr in with_scam_outcomes:
            session.add(TokenOutcome(
                token_address=addr, auction_id=42,
                appeared_in_winner=False, appeared_in_ours=False, caused_revert=True,
            ))
        await session.commit()


def _gp_entry(
    *,
    is_honeypot: str = "0",
    cannot_buy: str = "0",
    cannot_sell_all: str = "0",
    is_open_source: str = "1",
    buy_tax: str = "0",
    sell_tax: str = "0",
) -> dict:
    return {
        "is_honeypot": is_honeypot,
        "cannot_buy": cannot_buy,
        "cannot_sell_all": cannot_sell_all,
        "is_open_source": is_open_source,
        "buy_tax": buy_tax,
        "sell_tax": sell_tax,
    }


def _gp_response(entries: dict[str, dict] | None, *, code: int = 1) -> dict:
    """Build a GoPlus response body. Keys are lower-cased like the real API."""
    body: dict = {"code": code, "message": "OK" if code == 1 else "ERR"}
    if entries is not None:
        body["result"] = {k.lower(): v for k, v in entries.items()}
    return body


@respx.mock
async def test_honeypot_classified_as_scam(session_factory) -> None:
    await _seed_db(session_factory, tokens=[TOKEN_A])
    respx.get(GOPLUS_URL_RE).mock(
        return_value=httpx.Response(
            200, json=_gp_response({TOKEN_A: _gp_entry(is_honeypot="1")})
        )
    )

    result = await _seed(
        batch_size=10, max_concurrent=2, dry_run=False, chain_id=42161,
        session_factory=session_factory,
    )

    assert result.n_scam == 1
    assert result.n_legit == 0
    async with session_factory() as s:
        rows = (await s.execute(select(TokenOutcome))).scalars().all()
    assert len(rows) == 1
    assert rows[0].token_address == TOKEN_A
    assert bool(rows[0].caused_revert) is True
    assert bool(rows[0].appeared_in_winner) is False


@respx.mock
async def test_clean_classified_as_legit(session_factory) -> None:
    await _seed_db(session_factory, tokens=[TOKEN_A])
    respx.get(GOPLUS_URL_RE).mock(
        return_value=httpx.Response(
            200, json=_gp_response({TOKEN_A: _gp_entry()})
        )
    )

    result = await _seed(
        batch_size=10, max_concurrent=2, dry_run=False, chain_id=42161,
        session_factory=session_factory,
    )

    assert result.n_legit == 1
    assert result.n_scam == 0
    async with session_factory() as s:
        rows = (await s.execute(select(TokenOutcome))).scalars().all()
    assert len(rows) == 1
    assert bool(rows[0].appeared_in_winner) is True
    assert bool(rows[0].caused_revert) is False


@respx.mock
async def test_response_code_not_ok_skipped(session_factory) -> None:
    """code != 1 and != 4029 → GoPlus couldn't analyze → skip, no DB writes.

    code=4029 is treated as rate-limit (retried), so use a different non-OK
    code here to test the "non-retryable, just skip" path.
    """
    await _seed_db(session_factory, tokens=[TOKEN_A])
    respx.get(GOPLUS_URL_RE).mock(
        return_value=httpx.Response(200, json=_gp_response(None, code=4023))
    )

    result = await _seed(
        batch_size=10, max_concurrent=2, dry_run=False, chain_id=42161,
        session_factory=session_factory,
    )

    assert result.n_skipped == 1
    assert result.n_scam == 0
    assert result.n_legit == 0
    async with session_factory() as s:
        rows = (await s.execute(select(TokenOutcome))).scalars().all()
    assert len(rows) == 0


@respx.mock
async def test_confirmed_scam_tokens_are_skipped(session_factory) -> None:
    """Tokens already confirmed as scam (caused_revert=True) must not be re-queried.

    Legit-only outcomes (`appeared_in_winner=True`) are NOT a reason to skip —
    every auction-touched token has those by default, and we still want
    external scam-classification for them.
    """
    await _seed_db(
        session_factory,
        tokens=[TOKEN_A, TOKEN_B],
        with_outcomes=[TOKEN_A],            # A has legit-only outcomes — should still be re-queried
        with_scam_outcomes=[TOKEN_B],       # B is confirmed scam — should be skipped
    )
    route = respx.get(GOPLUS_URL_RE).mock(
        return_value=httpx.Response(
            200, json=_gp_response({TOKEN_A: _gp_entry()})
        )
    )

    result = await _seed(
        batch_size=10, max_concurrent=2, dry_run=False, chain_id=42161,
        session_factory=session_factory,
    )

    # Exactly one API call for TOKEN_A; TOKEN_B was filtered out.
    assert route.call_count == 1
    assert result.n_checked == 1
    # A's pre-existing legit row + A's new legit row + B's scam row = 3 total.
    async with session_factory() as s:
        rows = (await s.execute(select(TokenOutcome))).scalars().all()
    assert len(rows) == 3


@respx.mock
async def test_rate_limit_retry_with_backoff(session_factory, monkeypatch) -> None:
    """First 429, second 200 — eventually writes the verdict."""
    await _seed_db(session_factory, tokens=[TOKEN_A])

    # Make backoff instantaneous so the test is fast.
    async def _no_sleep(_s: float) -> None:
        return None
    monkeypatch.setattr("scripts.auto_seed_labels.asyncio.sleep", _no_sleep)

    responses = iter([
        httpx.Response(429, json={"error": "rate limited"}),
        httpx.Response(
            200, json=_gp_response({TOKEN_A: _gp_entry(is_honeypot="1")})
        ),
    ])
    respx.get(GOPLUS_URL_RE).mock(side_effect=lambda req: next(responses))

    result = await _seed(
        batch_size=10, max_concurrent=2, dry_run=False, chain_id=42161,
        session_factory=session_factory,
    )

    assert result.n_scam == 1
    async with session_factory() as s:
        rows = (await s.execute(select(TokenOutcome))).scalars().all()
    assert len(rows) == 1
    assert bool(rows[0].caused_revert) is True


@respx.mock
async def test_dry_run_no_db_writes(session_factory) -> None:
    await _seed_db(session_factory, tokens=[TOKEN_A, TOKEN_B])
    respx.get(GOPLUS_URL_RE).mock(
        return_value=httpx.Response(
            200,
            json=_gp_response({
                TOKEN_A: _gp_entry(is_honeypot="1"),
                TOKEN_B: _gp_entry(is_honeypot="1"),
            }),
        )
    )

    result = await _seed(
        batch_size=10, max_concurrent=2, dry_run=True, chain_id=42161,
        session_factory=session_factory,
    )

    # Verdicts are counted, but nothing is persisted.
    assert result.n_scam == 2
    async with session_factory() as s:
        rows = (await s.execute(select(TokenOutcome))).scalars().all()
    assert len(rows) == 0


@respx.mock
async def test_concurrent_calls_respect_semaphore(session_factory) -> None:
    """With max_concurrent=2 and 4 chunks, peak in-flight must stay <= 2."""
    await _seed_db(session_factory, tokens=[TOKEN_A, TOKEN_B, TOKEN_C, TOKEN_D])

    import asyncio as _asyncio
    in_flight = 0
    peak = 0
    lock = _asyncio.Lock()

    async def _handler(_req: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        # yield so other coroutines can hit the handler concurrently
        await _asyncio.sleep(0.01)
        async with lock:
            in_flight -= 1
        # Each chunk has exactly one address; return a clean verdict for it.
        addrs = _req.url.params["contract_addresses"].split(",")
        return httpx.Response(
            200,
            json=_gp_response({a: _gp_entry() for a in addrs}),
        )

    respx.get(GOPLUS_URL_RE).mock(side_effect=_handler)

    # api_batch_size=1 forces one chunk per token so the semaphore actually bites.
    result = await _seed(
        batch_size=10, max_concurrent=2, dry_run=False, chain_id=42161,
        api_batch_size=1, session_factory=session_factory,
    )

    assert result.n_legit == 4
    assert peak <= 2


@respx.mock
async def test_main_async_swallows_unhandled_exceptions(monkeypatch) -> None:
    """Cron entry-point must never raise; broken DB returns empty SeedResult."""

    def _broken_factory() -> None:
        raise RuntimeError("DB unreachable")

    monkeypatch.setattr("scripts.auto_seed_labels.get_session_factory", _broken_factory)

    result = await main_async(batch_size=5, max_concurrent=1, dry_run=False, chain_id=42161)
    assert result.n_checked == 0  # nothing happened, but no exception escaped


async def test_no_anchor_auction_skips_writes(session_factory) -> None:
    """If no ShadowAuction rows exist yet, the run must short-circuit."""
    # Insert a token_features row but NO ShadowAuction.
    async with session_factory() as s:
        s.add(TokenFeatures(token_address=TOKEN_A, decimals=18))
        await s.commit()

    result = await _seed(
        batch_size=10, max_concurrent=2, dry_run=False, chain_id=42161,
        session_factory=session_factory,
    )

    assert result.n_checked == 0
    async with session_factory() as s:
        rows = (await s.execute(select(TokenOutcome))).scalars().all()
    assert len(rows) == 0


@respx.mock
async def test_batched_request_includes_multiple_addresses(session_factory) -> None:
    """A single GoPlus call must carry all addresses in the chunk, comma-joined."""
    await _seed_db(session_factory, tokens=[TOKEN_A, TOKEN_B, TOKEN_C])

    seen_params: dict[str, str] = {}

    async def _handler(req: httpx.Request) -> httpx.Response:
        seen_params["contract_addresses"] = req.url.params["contract_addresses"]
        return httpx.Response(
            200,
            json=_gp_response({
                TOKEN_A: _gp_entry(),
                TOKEN_B: _gp_entry(),
                TOKEN_C: _gp_entry(),
            }),
        )

    route = respx.get(GOPLUS_URL_RE).mock(side_effect=_handler)

    result = await _seed(
        batch_size=10, max_concurrent=2, dry_run=False, chain_id=42161,
        api_batch_size=50, session_factory=session_factory,
    )

    # One HTTP call carrying all three addresses.
    assert route.call_count == 1
    sent = seen_params["contract_addresses"].split(",")
    assert set(sent) == {TOKEN_A, TOKEN_B, TOKEN_C}
    assert result.n_legit == 3


@respx.mock
async def test_richer_classification_legit_requires_all_conditions(session_factory) -> None:
    """is_honeypot=0 alone isn't enough — closed-source must NOT be legit."""
    await _seed_db(session_factory, tokens=[TOKEN_A])
    respx.get(GOPLUS_URL_RE).mock(
        return_value=httpx.Response(
            200,
            json=_gp_response({
                TOKEN_A: _gp_entry(is_honeypot="0", is_open_source="0"),
            }),
        )
    )

    result = await _seed(
        batch_size=10, max_concurrent=2, dry_run=False, chain_id=42161,
        session_factory=session_factory,
    )

    # Ambiguous → skip; no DB write.
    assert result.n_legit == 0
    assert result.n_scam == 0
    assert result.n_skipped == 1
    async with session_factory() as s:
        rows = (await s.execute(select(TokenOutcome))).scalars().all()
    assert len(rows) == 0
