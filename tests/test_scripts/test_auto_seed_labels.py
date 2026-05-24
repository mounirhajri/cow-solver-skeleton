"""Tests for scripts/auto_seed_labels.py — Honeypot.is auto-seeding pipeline."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.auto_seed_labels import _seed, main_async
from src.persistence.models import ShadowAuction, TokenFeatures, TokenOutcome

HONEYPOT_URL = "https://api.honeypot.is/v2/IsHoneypot"

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


async def _seed_db(factory, *, tokens: list[str], with_outcomes: list[str] | None = None) -> None:
    """Insert an anchor auction, token_features rows, and optional outcomes."""
    with_outcomes = with_outcomes or []
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
        await session.commit()


def _hp_response(*, is_honeypot: bool | None, sim_success: bool = True,
                 missing_result: bool = False) -> dict:
    body: dict = {
        "token": {"address": TOKEN_A, "name": "x", "symbol": "X", "decimals": 18},
        "simulationSuccess": sim_success,
    }
    if not missing_result:
        body["honeypotResult"] = {"isHoneypot": is_honeypot}
    return body


@respx.mock
async def test_honeypot_classified_as_scam(session_factory) -> None:
    await _seed_db(session_factory, tokens=[TOKEN_A])
    respx.get(HONEYPOT_URL).mock(
        return_value=httpx.Response(200, json=_hp_response(is_honeypot=True))
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
    respx.get(HONEYPOT_URL).mock(
        return_value=httpx.Response(
            200, json=_hp_response(is_honeypot=False, sim_success=True)
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
async def test_simulation_failure_skipped(session_factory) -> None:
    await _seed_db(session_factory, tokens=[TOKEN_A])
    # Sim failed AND honeypotResult missing → unknown → skip
    respx.get(HONEYPOT_URL).mock(
        return_value=httpx.Response(
            200, json=_hp_response(is_honeypot=None, sim_success=False, missing_result=True)
        )
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
async def test_idempotent_skips_already_labeled(session_factory) -> None:
    """Tokens with an existing token_outcomes row must not be re-queried."""
    await _seed_db(
        session_factory,
        tokens=[TOKEN_A, TOKEN_B],
        with_outcomes=[TOKEN_A],  # A already labeled
    )
    route = respx.get(HONEYPOT_URL).mock(
        return_value=httpx.Response(200, json=_hp_response(is_honeypot=False))
    )

    result = await _seed(
        batch_size=10, max_concurrent=2, dry_run=False, chain_id=42161,
        session_factory=session_factory,
    )

    # Only TOKEN_B got queried — TOKEN_A was filtered by the unlabeled query.
    assert route.call_count == 1
    assert result.n_checked == 1
    # A's pre-existing row + B's new legit row = 2 total.
    async with session_factory() as s:
        rows = (await s.execute(select(TokenOutcome))).scalars().all()
    assert len(rows) == 2


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
        httpx.Response(200, json=_hp_response(is_honeypot=True)),
    ])
    respx.get(HONEYPOT_URL).mock(side_effect=lambda req: next(responses))

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
    respx.get(HONEYPOT_URL).mock(
        return_value=httpx.Response(200, json=_hp_response(is_honeypot=True))
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
    """With max_concurrent=2 and 4 tokens, peak in-flight must stay <= 2."""
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
        return httpx.Response(200, json=_hp_response(is_honeypot=False))

    respx.get(HONEYPOT_URL).mock(side_effect=_handler)

    result = await _seed(
        batch_size=10, max_concurrent=2, dry_run=False, chain_id=42161,
        session_factory=session_factory,
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
