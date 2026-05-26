"""Tests for shadow persistence.

These tests use sqlite+aiosqlite as a transient in-memory backend to verify
the persist function constructs correct SQL and runs without error.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.models.auction import Auction
from src.persistence.models import ShadowAuction, ShadowSolution
from src.shadow.persist import (
    EPSILON_HIGH_WEI,
    EPSILON_WEI,
    persist_shadow_attempt,
    persist_shadow_attempt_safe,
)
from src.solver.orchestrator import AttemptRecord


@pytest.fixture
async def session_factory(monkeypatch):
    """In-memory sqlite engine, schema created fresh per test.

    SQLite doesn't autoincrement BIGINT columns (only INTEGER), so we
    create the tables with raw DDL that uses INTEGER for the id columns.
    """
    from sqlalchemy import text

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # Create tables with INTEGER (not BIGINT) so sqlite autoincrement works
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
            CREATE TABLE shadow_solutions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                auction_id INTEGER NOT NULL REFERENCES shadow_auctions(auction_id),
                strategy TEXT NOT NULL,
                status TEXT NOT NULL,
                latency_ms INTEGER,
                solution TEXT,
                error TEXT,
                our_score_wei NUMERIC,
                score_vs_winner_prices_wei NUMERIC,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        await conn.execute(text("""
            CREATE TABLE shadow_winners (
                auction_id INTEGER PRIMARY KEY REFERENCES shadow_auctions(auction_id),
                winner_solver TEXT NOT NULL,
                score NUMERIC,
                raw_solution TEXT NOT NULL
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
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.shadow.persist.get_session_factory", lambda: factory)
    yield factory
    await engine.dispose()


def _auction(auction_id: str = "1234") -> Auction:
    return Auction(
        id=auction_id,
        tokens={},
        orders=[],
        liquidity=[],
        effectiveGasPrice=0,
        deadline=None,
    )


@pytest.mark.asyncio
async def test_persist_writes_auction_and_solutions(session_factory) -> None:
    auction = _auction("1234")
    attempts = [
        AttemptRecord(
            strategy="naive",
            status="solved",
            latency_ms=312,
            solution={"id": 1234},
            error=None,
        ),
        AttemptRecord(
            strategy="cow-matching-multi-party",
            status="no_solution",
            latency_ms=45,
            solution=None,
            error=None,
        ),
    ]

    await persist_shadow_attempt(auction, attempts, raw_competition={"test": True})

    async with session_factory() as session:
        auction_rows = (await session.execute(select(ShadowAuction))).scalars().all()
        assert len(auction_rows) == 1
        assert auction_rows[0].auction_id == 1234
        assert auction_rows[0].n_orders == 0
        assert auction_rows[0].raw_competition == {"test": True}

        sol_rows = (await session.execute(select(ShadowSolution))).scalars().all()
        assert len(sol_rows) == 2
        statuses = {r.strategy: r.status for r in sol_rows}
        assert statuses["naive"] == "solved"
        assert statuses["cow-matching-multi-party"] == "no_solution"


@pytest.mark.asyncio
async def test_persist_is_idempotent_on_auction(session_factory) -> None:
    """Calling persist twice for the same auction_id must not duplicate auction row."""
    auction = _auction("999")
    attempts = [
        AttemptRecord(
            strategy="naive", status="no_solution", latency_ms=10, solution=None, error=None
        ),
    ]

    await persist_shadow_attempt(auction, attempts)
    await persist_shadow_attempt(auction, attempts)  # second call — must not raise or duplicate

    async with session_factory() as session:
        rows = (await session.execute(select(ShadowAuction))).scalars().all()
        assert len(rows) == 1  # still only one auction row


@pytest.mark.asyncio
async def test_persist_records_error_attempt(session_factory) -> None:
    auction = _auction("555")
    attempts = [
        AttemptRecord(
            strategy="naive",
            status="error",
            latency_ms=50,
            solution=None,
            error="something went wrong",
        ),
    ]

    await persist_shadow_attempt(auction, attempts)

    async with session_factory() as session:
        sol_rows = (await session.execute(select(ShadowSolution))).scalars().all()
        assert len(sol_rows) == 1
        assert sol_rows[0].status == "error"
        assert sol_rows[0].error == "something went wrong"


@pytest.mark.asyncio
async def test_persist_safe_swallows_errors(monkeypatch) -> None:
    """persist_shadow_attempt_safe must never raise even if underlying call fails."""

    async def broken(*args, **kwargs) -> None:
        raise RuntimeError("DB down")

    monkeypatch.setattr("src.shadow.persist.persist_shadow_attempt", broken)
    auction = _auction("1")
    # Must NOT raise
    await persist_shadow_attempt_safe(auction, [], None)


# ─── Winner + token outcome tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_winner_inserts_row(session_factory) -> None:
    from src.persistence.models import ShadowAuction, ShadowWinner
    from src.shadow.persist import persist_winner_and_outcomes

    # Prerequisite: shadow_auction row exists (FK constraint)
    async with session_factory() as s:
        s.add(ShadowAuction(
            auction_id=999, polled_at=datetime.now(UTC),
            n_orders=0, raw_competition={}, raw_auction={},
        ))
        await s.commit()

    comp = {
        "auctionId": "999",
        "solutions": [
            {"solver": "0xabc", "score": "1000000000000000000", "isWinner": True,
             "ranking": 1, "prices": {"0xa": "1"}, "trades": []}
        ],
    }
    auction = {"orders": [{"sellToken": "0xA", "buyToken": "0xB"}]}
    ours = {"prices": {"0xA": "1"}}

    await persist_winner_and_outcomes(999, comp, auction, ours)

    async with session_factory() as s:
        winners = (await s.execute(select(ShadowWinner))).scalars().all()
        assert len(winners) == 1
        assert winners[0].winner_solver == "0xabc"
        assert int(winners[0].score) == 1000000000000000000


@pytest.mark.asyncio
async def test_persist_winner_handles_no_winner(session_factory) -> None:
    from src.persistence.models import ShadowAuction, ShadowWinner, TokenOutcome
    from src.shadow.persist import persist_winner_and_outcomes

    async with session_factory() as s:
        s.add(ShadowAuction(
            auction_id=888, polled_at=datetime.now(UTC),
            n_orders=0, raw_competition={}, raw_auction={},
        ))
        await s.commit()

    comp = {"auctionId": "888", "solutions": []}
    auction = {"orders": [{"sellToken": "0xA", "buyToken": "0xB"}]}

    await persist_winner_and_outcomes(888, comp, auction, None)

    async with session_factory() as s:
        winners = (await s.execute(select(ShadowWinner))).scalars().all()
        assert len(winners) == 0
        # But outcomes still derived
        outcomes = (await s.execute(select(TokenOutcome))).scalars().all()
        assert len(outcomes) == 2  # 0xa, 0xb


@pytest.mark.asyncio
async def test_persist_winner_handles_invalid_score(session_factory) -> None:
    from src.persistence.models import ShadowAuction, ShadowWinner
    from src.shadow.persist import persist_winner_and_outcomes

    async with session_factory() as s:
        s.add(ShadowAuction(
            auction_id=777, polled_at=datetime.now(UTC),
            n_orders=0, raw_competition={}, raw_auction={},
        ))
        await s.commit()

    comp = {
        "auctionId": "777",
        "solutions": [{"solver": "0xabc", "score": "not_a_number", "isWinner": True,
                       "prices": {}, "trades": []}],
    }
    await persist_winner_and_outcomes(777, comp, {"orders": []}, None)

    async with session_factory() as s:
        winners = (await s.execute(select(ShadowWinner))).scalars().all()
        assert len(winners) == 1
        assert winners[0].score is None  # gracefully parsed as None


@pytest.mark.asyncio
async def test_persist_winner_creates_auction_row_when_missing(session_factory) -> None:
    """Ensure FK target (shadow_auctions) is created when missing — handles race."""
    from src.persistence.models import ShadowAuction, ShadowWinner
    from src.shadow.persist import persist_winner_and_outcomes

    # Deliberately do NOT pre-insert the ShadowAuction row
    comp = {
        "auctionId": "666",
        "solutions": [{"solver": "0xdef", "score": "500", "isWinner": True,
                       "prices": {}, "trades": []}],
    }
    auction = {"orders": [{"sellToken": "0xA", "buyToken": "0xB"}]}

    await persist_winner_and_outcomes(666, comp, auction, None)

    async with session_factory() as s:
        auctions = (await s.execute(select(ShadowAuction))).scalars().all()
        assert len(auctions) == 1  # created as side-effect
        winners = (await s.execute(select(ShadowWinner))).scalars().all()
        assert len(winners) == 1
        assert winners[0].winner_solver == "0xdef"


@pytest.mark.asyncio
async def test_persist_winner_populates_score_vs_winner_prices(session_factory) -> None:
    """persist_winner_and_outcomes recomputes our score at winner clearingPrices."""
    from src.persistence.models import ShadowAuction, ShadowSolution
    from src.shadow.persist import persist_winner_and_outcomes

    ETH = 10**18
    auction_id = 4242

    # Auction + one solved solution row whose trades we can score
    async with session_factory() as s:
        s.add(
            ShadowAuction(
                auction_id=auction_id,
                polled_at=datetime.now(UTC),
                n_orders=1,
                raw_competition={},
                raw_auction={},
            )
        )
        s.add(
            ShadowSolution(
                auction_id=auction_id,
                strategy="naive",
                status="solved",
                latency_ms=10,
                solution={
                    "prices": {"0xsell": str(11 * ETH), "0xbuy": str(10 * ETH)},
                    "trades": [
                        {
                            "kind": "fulfillment",
                            "orderUid": "0xabc",
                            "executedAmount": str(1000 * ETH),
                        }
                    ],
                },
                error=None,
            )
        )
        await s.commit()

    # Winner uses different (better) clearingPrices → score @ winner prices > our_score
    comp = {
        "auctionId": str(auction_id),
        "auction": {"prices": {"0xbuy": str(ETH)}},
        "solutions": [
            {
                "solver": "0xwinner",
                "score": "0",
                "isWinner": True,
                "ranking": 1,
                "clearingPrices": {"0xsell": str(2 * ETH), "0xbuy": str(ETH)},
                "trades": [],
            }
        ],
    }
    auction_payload = {
        "orders": [
            {
                "uid": "0xabc",
                "sellToken": "0xsell",
                "buyToken": "0xbuy",
                "sellAmount": str(1000 * ETH),
                "buyAmount": str(900 * ETH),
                "kind": "sell",
            }
        ],
        "tokens": {"0xbuy": {"referencePrice": str(ETH)}},
    }

    await persist_winner_and_outcomes(auction_id, comp, auction_payload, None)

    async with session_factory() as s:
        sol = (
            await s.execute(
                select(ShadowSolution).where(ShadowSolution.auction_id == auction_id)
            )
        ).scalar_one()
        assert sol.score_vs_winner_prices_wei is not None
        assert int(sol.score_vs_winner_prices_wei) > 0

    # Second call must not overwrite (idempotency: where col IS NULL)
    sentinel = 12345
    async with session_factory() as s:
        await s.execute(
            ShadowSolution.__table__.update()
            .where(ShadowSolution.auction_id == auction_id)
            .values(score_vs_winner_prices_wei=sentinel)
        )
        await s.commit()

    await persist_winner_and_outcomes(auction_id, comp, auction_payload, None)

    async with session_factory() as s:
        sol = (
            await s.execute(
                select(ShadowSolution).where(ShadowSolution.auction_id == auction_id)
            )
        ).scalar_one()
        assert int(sol.score_vs_winner_prices_wei) == sentinel


@pytest.mark.asyncio
async def test_persist_winner_outcomes_safe_swallows_errors(monkeypatch) -> None:
    """persist_winner_and_outcomes_safe must never raise."""
    from src.shadow.persist import persist_winner_and_outcomes_safe

    async def broken(*args, **kwargs) -> None:
        raise RuntimeError("DB down")

    monkeypatch.setattr("src.shadow.persist.persist_winner_and_outcomes", broken)
    # Must NOT raise
    await persist_winner_and_outcomes_safe(42, {}, {}, None)


# ─── persist_skipped_auction tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_skipped_auction_inserts_rows(session_factory) -> None:
    """persist_skipped_auction must create auction row + skipped solution row."""
    from src.shadow.persist import persist_skipped_auction

    await persist_skipped_auction(
        auction_id=11111,
        auction_payload={"id": "11111", "tokens": {}, "orders": [], "liquidity": []},
        raw_competition={"auctionId": "11111", "solutions": []},
        n_orders=750,
    )

    async with session_factory() as s:
        auctions = (await s.execute(select(ShadowAuction))).scalars().all()
        assert len(auctions) == 1
        assert auctions[0].auction_id == 11111
        assert auctions[0].n_orders == 750

        solutions = (await s.execute(select(ShadowSolution))).scalars().all()
        assert len(solutions) == 1
        assert solutions[0].strategy == "poller-skipped"
        assert solutions[0].status == "skipped"
        assert "750" in solutions[0].error


@pytest.mark.asyncio
async def test_persist_skipped_auction_idempotent_on_auction_row(session_factory) -> None:
    """If the shadow_auction row already exists, persist_skipped_auction must not duplicate it."""
    from src.shadow.persist import persist_skipped_auction

    # Pre-insert the auction row (e.g., from persist_winner_and_outcomes running first)
    async with session_factory() as s:
        s.add(ShadowAuction(
            auction_id=22222,
            polled_at=datetime.now(UTC),
            n_orders=0,
            raw_competition={},
            raw_auction={},
        ))
        await s.commit()

    await persist_skipped_auction(
        auction_id=22222,
        auction_payload={},
        raw_competition={},
        n_orders=900,
    )

    async with session_factory() as s:
        auctions = (await s.execute(select(ShadowAuction))).scalars().all()
        assert len(auctions) == 1  # still only one — not duplicated

        solutions = (await s.execute(select(ShadowSolution))).scalars().all()
        assert len(solutions) == 1
        assert solutions[0].strategy == "poller-skipped"


@pytest.mark.asyncio
async def test_persist_skipped_auction_safe_swallows_errors(monkeypatch) -> None:
    """persist_skipped_auction_safe must never raise."""
    from src.shadow.persist import persist_skipped_auction_safe

    async def broken(*args, **kwargs) -> None:
        raise RuntimeError("DB down")

    monkeypatch.setattr("src.shadow.persist.persist_skipped_auction", broken)
    # Must NOT raise
    await persist_skipped_auction_safe(99, {}, {}, 500)


# ─── Sub-dust filter tests (Issue #3) ────────────────────────────────────────


def _attempt_with_solution(score_override: int | None = None) -> AttemptRecord:
    """Return an AttemptRecord that has a solution dict.

    The solution content is irrelevant — we patch compute_solution_score to
    control the returned score directly. Uses a non-naive strategy because
    persist_shadow_attempt now forces our_score_wei=None for naive
    regardless of the patched score (KNOWN-BAD phantom path; see
    src/shadow/persist.py).
    """
    return AttemptRecord(
        strategy="router-v2",
        status="solved",
        latency_ms=10,
        solution={"id": 9999, "prices": {}, "trades": []},
        error=None,
    )


def _patch_scoring(monkeypatch, score_value: int) -> None:
    """Patch the three scoring helpers so the per-attempt score path executes.

    uid_map and native_prices must both be non-empty for the scoring guard to
    pass; compute_solution_score is patched to return the desired value.
    """
    monkeypatch.setattr(
        "src.shadow.persist.orders_by_uid_from_auction",
        lambda *args, **kwargs: {"0xdummy": object()},
    )
    monkeypatch.setattr(
        "src.shadow.persist.extract_native_prices",
        lambda *args, **kwargs: {"0xdummy": 1},
    )
    monkeypatch.setattr(
        "src.shadow.persist.compute_solution_score",
        lambda *args, **kwargs: score_value,
    )


@pytest.mark.asyncio
async def test_persist_skips_sub_dust_solution(session_factory, monkeypatch) -> None:
    """Solutions with score < EPSILON_WEI must NOT be written to shadow_solutions."""
    sub_dust_score = int(5e11)  # below EPSILON_WEI (10**12)
    assert sub_dust_score < EPSILON_WEI

    _patch_scoring(monkeypatch, sub_dust_score)

    auction = _auction("3001")
    attempts = [_attempt_with_solution()]
    await persist_shadow_attempt(auction, attempts)

    async with session_factory() as s:
        rows = (await s.execute(select(ShadowSolution))).scalars().all()
    assert len(rows) == 0, "Sub-dust solution should not have been persisted"


@pytest.mark.asyncio
async def test_persist_keeps_dust_threshold_solution(session_factory, monkeypatch) -> None:
    """Solutions with score == EPSILON_WEI must be persisted (filter uses <, not <=)."""
    at_threshold_score = EPSILON_WEI  # exactly 10**12 — must be kept

    _patch_scoring(monkeypatch, at_threshold_score)

    auction = _auction("3002")
    attempts = [_attempt_with_solution()]
    await persist_shadow_attempt(auction, attempts)

    async with session_factory() as s:
        rows = (await s.execute(select(ShadowSolution))).scalars().all()
    assert len(rows) == 1, "Solution at EPSILON_WEI threshold must be kept"
    assert int(rows[0].our_score_wei) == EPSILON_WEI


@pytest.mark.asyncio
async def test_persist_keeps_score_none_solution(session_factory, monkeypatch) -> None:
    """When score computation yields 0 (→ score=None), the row is still persisted.

    score=None means zero surplus (unprofitable but valid) — not sub-dust. The
    sub-dust filter only fires when score is a positive int below EPSILON_WEI.
    """
    # raw_score=0 → score gets set to None by the `raw_score > 0` guard.
    _patch_scoring(monkeypatch, 0)

    auction = _auction("3003")
    attempts = [_attempt_with_solution()]
    await persist_shadow_attempt(auction, attempts)

    async with session_factory() as s:
        rows = (await s.execute(select(ShadowSolution))).scalars().all()
    assert len(rows) == 1, "Solution with score=None must still be persisted"
    assert rows[0].our_score_wei is None


@pytest.mark.asyncio
async def test_persist_logs_sub_dust_count(session_factory, monkeypatch) -> None:
    """3 sub-dust + 1 real solution: single summary log.info fires with n_sub_dust_skipped=3.

    structlog uses PrintLoggerFactory which bypasses stdlib caplog, so we
    capture by monkeypatching the module-level log.info directly — the same
    pattern used in test_router.py and test_rf_filter.py.
    """
    sub_dust = int(5e11)  # below EPSILON_WEI
    real_score = int(1e13)  # well above EPSILON_WEI

    # uid_map and native_prices must be non-empty so the scoring path runs.
    monkeypatch.setattr(
        "src.shadow.persist.orders_by_uid_from_auction",
        lambda *args, **kwargs: {"0xdummy": object()},
    )
    monkeypatch.setattr(
        "src.shadow.persist.extract_native_prices",
        lambda *args, **kwargs: {"0xdummy": 1},
    )

    scores = iter([sub_dust, sub_dust, sub_dust, real_score])
    monkeypatch.setattr(
        "src.shadow.persist.compute_solution_score",
        lambda *args, **kwargs: next(scores),
    )

    log_calls: list[tuple[str, dict]] = []

    def capture_log(event: str, **kwargs: object) -> None:
        log_calls.append((event, dict(kwargs)))

    monkeypatch.setattr("src.shadow.persist.log.info", capture_log)

    auction = _auction("3004")
    attempts = [
        AttemptRecord(
            strategy=f"naive-{i}",
            status="solved",
            latency_ms=10,
            solution={"id": 9999, "prices": {}, "trades": []},
            error=None,
        )
        for i in range(4)
    ]

    await persist_shadow_attempt(auction, attempts)

    # One real solution should be persisted
    async with session_factory() as s:
        rows = (await s.execute(select(ShadowSolution))).scalars().all()
    assert len(rows) == 1, "Only the real solution should be persisted"

    # Exactly one summary log must be emitted for the sub-dust count
    dust_logs = [(ev, kw) for ev, kw in log_calls if ev == "sub_dust_solutions_skipped"]
    assert len(dust_logs) == 1, "Exactly one summary log must be emitted"
    assert dust_logs[0][1]["n_sub_dust_skipped"] == 3


# ── Phantom upper-cap tests (router-v2 phantom prevention) ──────────────────


@pytest.mark.asyncio
async def test_persist_nulls_score_above_upper_cap(session_factory, monkeypatch) -> None:
    """Solutions with score >= EPSILON_HIGH_WEI must persist the row but NULL the score.

    Production-observed: router-v2 emits CIP-14 surplus in the 6 ETH range from
    arb-style high-surplus order limits that no AMM can clear. Pre-fix these
    polluted estimate_economics + analyze_competitors with phantom revenue.
    Post-fix: row stays for observability; score is NULL so downstream queries
    treating NULL as "ignore" automatically filter them out.
    """
    phantom_score = int(6 * 10**18)  # 6 ETH — typical router-v2 phantom
    assert phantom_score >= EPSILON_HIGH_WEI

    _patch_scoring(monkeypatch, phantom_score)

    auction = _auction("3010")
    attempts = [_attempt_with_solution()]
    await persist_shadow_attempt(auction, attempts)

    async with session_factory() as s:
        rows = (await s.execute(select(ShadowSolution))).scalars().all()
    assert len(rows) == 1, "Row must persist (we want observability of phantom emissions)"
    assert rows[0].our_score_wei is None, "Score must be NULL for phantom-suspect emissions"
    assert rows[0].strategy == "router-v2"
    assert rows[0].status == "solved"


@pytest.mark.asyncio
async def test_persist_nulls_score_exactly_at_upper_cap(session_factory, monkeypatch) -> None:
    """Filter uses >=, so a score EXACTLY equal to EPSILON_HIGH_WEI is NULL'd.

    1 ETH is itself phantom-suspect on Arbitrum (largest legitimate observation
    is 0.094 ETH), so the boundary is inclusive on the safe side.
    """
    at_cap_score = EPSILON_HIGH_WEI

    _patch_scoring(monkeypatch, at_cap_score)

    auction = _auction("3011")
    attempts = [_attempt_with_solution()]
    await persist_shadow_attempt(auction, attempts)

    async with session_factory() as s:
        rows = (await s.execute(select(ShadowSolution))).scalars().all()
    assert len(rows) == 1
    assert rows[0].our_score_wei is None, "Score at the cap boundary must also be NULL'd"


@pytest.mark.asyncio
async def test_persist_keeps_score_well_below_upper_cap(session_factory, monkeypatch) -> None:
    """A score well below the upper cap is persisted verbatim — regression guard.

    Locks in that the cap is surgical: legitimate observations (which currently
    top out around 0.094 ETH = 10× below the cap) are unaffected.  We use
    0.5 ETH rather than ``EPSILON_HIGH_WEI - 1`` to avoid SQLite REAL precision
    loss at the 10**18 boundary (Postgres NUMERIC in prod is exact, but the
    in-memory aiosqlite backend rounds near 10**18).
    """
    half_eth = 5 * 10**17  # 0.5 ETH — well below 1 ETH cap, well above sub-dust
    assert EPSILON_WEI < half_eth < EPSILON_HIGH_WEI

    _patch_scoring(monkeypatch, half_eth)

    auction = _auction("3012")
    attempts = [_attempt_with_solution()]
    await persist_shadow_attempt(auction, attempts)

    async with session_factory() as s:
        rows = (await s.execute(select(ShadowSolution))).scalars().all()
    assert len(rows) == 1
    assert int(rows[0].our_score_wei) == half_eth, (
        "Score well below the cap must be persisted as-is"
    )


@pytest.mark.asyncio
async def test_persist_logs_phantom_above_cap_count(session_factory, monkeypatch) -> None:
    """Summary log fires once per persist call with the phantom count.

    Mirrors the sub_dust_solutions_skipped log pattern — one aggregate log per
    auction so analytics can grep+jq the rate of phantom suppressions over time.
    """
    phantom = int(6 * 10**18)
    real_score = int(1e15)  # 0.001 ETH — clean bipartite-range win

    monkeypatch.setattr(
        "src.shadow.persist.orders_by_uid_from_auction",
        lambda *args, **kwargs: {"0xdummy": object()},
    )
    monkeypatch.setattr(
        "src.shadow.persist.extract_native_prices",
        lambda *args, **kwargs: {"0xdummy": 1},
    )

    scores = iter([phantom, phantom, real_score])
    monkeypatch.setattr(
        "src.shadow.persist.compute_solution_score",
        lambda *args, **kwargs: next(scores),
    )

    log_calls: list[tuple[str, dict]] = []

    def capture_log(event: str, **kwargs: object) -> None:
        log_calls.append((event, dict(kwargs)))

    monkeypatch.setattr("src.shadow.persist.log.info", capture_log)

    auction = _auction("3013")
    attempts = [
        AttemptRecord(
            strategy=f"router-v2-attempt-{i}",
            status="solved",
            latency_ms=10,
            solution={"id": 9999, "prices": {}, "trades": []},
            error=None,
        )
        for i in range(3)
    ]

    await persist_shadow_attempt(auction, attempts)

    # All 3 rows persisted (observability) — but 2 with NULL score (phantom),
    # 1 with the real score.
    async with session_factory() as s:
        rows = (await s.execute(select(ShadowSolution))).scalars().all()
    assert len(rows) == 3
    null_scores = sum(1 for r in rows if r.our_score_wei is None)
    real_scores = sum(1 for r in rows if r.our_score_wei is not None)
    assert null_scores == 2
    assert real_scores == 1

    # Exactly one per-event log fires per call.  Also fires `shadow_score_above_upper_cap`
    # twice (once per phantom row) for finer-grained tuning data.
    summary_logs = [(ev, kw) for ev, kw in log_calls if ev == "phantom_above_cap_nulled"]
    assert len(summary_logs) == 1
    assert summary_logs[0][1]["n_phantom_above_cap"] == 2

    per_row_logs = [(ev, kw) for ev, kw in log_calls if ev == "shadow_score_above_upper_cap"]
    assert len(per_row_logs) == 2
    # Each per-row log includes the strategy and raw_score_wei for forensics.
    for _, kw in per_row_logs:
        assert "strategy" in kw
        assert "raw_score_wei" in kw
