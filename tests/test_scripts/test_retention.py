"""Tests for scripts/retention_loop.py.

The pure ``retain_row`` transform is tested directly; the cycle is tested
end-to-end against sqlite+aiosqlite using the same manual-DDL fixture
pattern as tests/test_shadow/test_persist.py (sqlite can't autoincrement
BIGINT, and the production tables use Postgres ``json`` columns that map
fine onto sqlite TEXT through SQLAlchemy's JSON type).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.retention_loop import (
    RETAINED_MARKER,
    extract_referenced_uids,
    retain_row,
    run_cycle,
)
from src.persistence.models import ShadowAuction, ShadowSolution

UID_A = "0x" + "aa" * 56
UID_B = "0x" + "bb" * 56
UID_C = "0x" + "cc" * 56


@pytest.fixture
async def session_factory(monkeypatch):
    """In-memory sqlite engine, schema created fresh per test."""
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
                feasible INTEGER,
                revert_reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("scripts.retention_loop.get_session_factory", lambda: factory)
    yield factory
    await engine.dispose()


def _raw_auction(uids: list[str]) -> dict:
    return {
        "id": "123",
        "tokens": {"0x" + "11" * 20: {"decimals": 18, "referencePrice": "1"}},
        "orders": [
            {"uid": uid, "sellToken": "0x" + "11" * 20, "sellAmount": "1000"}
            for uid in uids
        ],
        "effectiveGasPrice": "1500000000",
    }


async def _insert_auction(
    factory,
    auction_id: int,
    age_hours: float,
    raw_auction: dict,
    solutions: list[dict | None] | None = None,
) -> None:
    async with factory() as s:
        s.add(
            ShadowAuction(
                auction_id=auction_id,
                polled_at=datetime.now(UTC) - timedelta(hours=age_hours),
                n_orders=len(raw_auction.get("orders") or []),
                raw_competition={},
                raw_auction=raw_auction,
            )
        )
        for sol in solutions or []:
            s.add(
                ShadowSolution(
                    auction_id=auction_id,
                    strategy="router-v2",
                    status="solved" if sol is not None else "no_solution",
                    latency_ms=10,
                    solution=sol,
                    error=None,
                )
            )
        await s.commit()


async def _fetch_raw(factory, auction_id: int) -> dict:
    async with factory() as s:
        row = (
            await s.execute(
                select(ShadowAuction.raw_auction).where(
                    ShadowAuction.auction_id == auction_id
                )
            )
        ).scalar_one()
    return row


# ── retain_row: pure transform ───────────────────────────────────────────────


def test_retain_row_keeps_only_referenced_orders() -> None:
    raw = _raw_auction([UID_A, UID_B, UID_C])
    slim = retain_row(raw, {UID_B})
    assert [o["uid"] for o in slim["orders"]] == [UID_B]
    assert slim[RETAINED_MARKER] is True
    # Everything else untouched — tokens in particular (probe round-trip).
    assert slim["tokens"] == raw["tokens"]
    assert slim["effectiveGasPrice"] == raw["effectiveGasPrice"]
    # Input not mutated.
    assert RETAINED_MARKER not in raw
    assert len(raw["orders"]) == 3


def test_retain_row_uid_compare_is_case_insensitive() -> None:
    raw = _raw_auction([UID_A.upper().replace("0X", "0x")])
    slim = retain_row(raw, {UID_A})  # referenced set is lowercase
    assert len(slim["orders"]) == 1


def test_retain_row_empty_referenced_set_empties_orders() -> None:
    slim = retain_row(_raw_auction([UID_A, UID_B]), set())
    assert slim["orders"] == []
    assert slim[RETAINED_MARKER] is True


def test_retain_row_without_orders_key_sets_marker_without_crash() -> None:
    slim = retain_row({"tokens": {"0xabc": {}}}, {UID_A})
    assert slim[RETAINED_MARKER] is True
    assert "orders" not in slim
    assert slim["tokens"] == {"0xabc": {}}


def test_retain_row_drops_non_dict_and_uidless_orders() -> None:
    raw = {"orders": ["not-a-dict", None, {"owner": "0x1"}, {"uid": UID_A}]}
    slim = retain_row(raw, {UID_A})
    assert slim["orders"] == [{"uid": UID_A}]


# ── extract_referenced_uids ──────────────────────────────────────────────────


def test_extract_referenced_uids_handles_all_key_variants() -> None:
    solutions = [
        {"trades": [{"order": UID_A.upper().replace("0X", "0x")}]},
        {"trades": [{"orderUid": UID_B}]},
        {"trades": [{"order_uid": UID_C}]},
    ]
    assert extract_referenced_uids(solutions) == {UID_A, UID_B, UID_C}


def test_extract_referenced_uids_tolerates_garbage() -> None:
    solutions = [
        None,
        "not json {",
        f'{{"trades": [{{"order": "{UID_A}"}}]}}',  # JSON-string solution
        {"trades": "nope"},
        {"trades": [None, {"executedAmount": "1"}, {"order": 42}]},
        {},
    ]
    assert extract_referenced_uids(solutions) == {UID_A}


# ── run_cycle: end-to-end against sqlite ─────────────────────────────────────


async def test_cycle_slims_old_row_to_referenced_orders(session_factory) -> None:
    await _insert_auction(
        session_factory,
        auction_id=1,
        age_hours=72,
        raw_auction=_raw_auction([UID_A, UID_B, UID_C]),
        solutions=[{"prices": {}, "trades": [{"order": UID_A}]}],
    )

    stats, watermark = await run_cycle()

    assert stats.n_processed == 1
    assert stats.n_orders_before == 3
    assert stats.n_orders_after == 1
    assert stats.bytes_saved_estimate > 0
    assert watermark is not None

    raw = await _fetch_raw(session_factory, 1)
    assert raw[RETAINED_MARKER] is True
    assert [o["uid"] for o in raw["orders"]] == [UID_A]
    assert "tokens" in raw  # untouched


async def test_cycle_leaves_young_rows_untouched(session_factory) -> None:
    fresh = _raw_auction([UID_A, UID_B])
    await _insert_auction(session_factory, auction_id=2, age_hours=1, raw_auction=fresh)

    stats, watermark = await run_cycle()

    assert stats.n_processed == 0
    assert watermark is None  # nothing was even scanned
    raw = await _fetch_raw(session_factory, 2)
    assert RETAINED_MARKER not in raw
    assert len(raw["orders"]) == 2


async def test_cycle_skips_already_retained_rows(session_factory) -> None:
    already = _raw_auction([UID_A])
    already[RETAINED_MARKER] = True
    await _insert_auction(session_factory, auction_id=3, age_hours=72, raw_auction=already)

    stats, _ = await run_cycle()

    assert stats.n_processed == 0
    assert stats.n_skipped_retained == 1
    raw = await _fetch_raw(session_factory, 3)
    assert len(raw["orders"]) == 1  # not re-filtered (UID_A is unreferenced)


async def test_cycle_empties_orders_when_no_solutions(session_factory) -> None:
    await _insert_auction(
        session_factory,
        auction_id=4,
        age_hours=72,
        raw_auction=_raw_auction([UID_A, UID_B]),
        solutions=[],
    )

    stats, _ = await run_cycle()

    assert stats.n_processed == 1
    raw = await _fetch_raw(session_factory, 4)
    assert raw["orders"] == []  # row survives, dead weight gone
    assert raw[RETAINED_MARKER] is True


async def test_cycle_marks_malformed_payload_without_crash(session_factory) -> None:
    # No "orders" key at all — must get the marker and never be rescanned.
    await _insert_auction(
        session_factory, auction_id=5, age_hours=72, raw_auction={"backfilled": True}
    )

    stats, _ = await run_cycle()

    assert stats.n_processed == 1
    raw = await _fetch_raw(session_factory, 5)
    assert raw[RETAINED_MARKER] is True
    assert raw["backfilled"] is True


async def test_cycle_watermark_paginates_across_cycles(session_factory) -> None:
    for aid, age in ((10, 80), (11, 70)):
        await _insert_auction(
            session_factory,
            auction_id=aid,
            age_hours=age,
            raw_auction=_raw_auction([UID_A]),
            solutions=[{"trades": [{"order": UID_A}]}],
        )

    stats1, watermark = await run_cycle(batch=1)
    assert stats1.n_processed == 1

    # Second cycle resumes past the first row via the watermark.
    stats2, watermark2 = await run_cycle(batch=1, watermark=watermark)
    assert stats2.n_processed == 1
    assert stats2.n_skipped_retained == 0  # watermark excluded row 10 entirely
    assert watermark2 is not None and watermark2 > watermark

    # Third cycle: nothing left beyond the watermark.
    stats3, _ = await run_cycle(batch=1, watermark=watermark2)
    assert stats3.n_processed == 0


async def test_cycle_respects_batch_cap(session_factory) -> None:
    for aid in range(20, 25):
        await _insert_auction(
            session_factory, auction_id=aid, age_hours=72, raw_auction=_raw_auction([UID_A])
        )

    stats, watermark = await run_cycle(batch=2)

    assert stats.n_processed == 2
    # Remaining rows are picked up by the next cycle from the watermark.
    stats2, _ = await run_cycle(batch=200, watermark=watermark)
    assert stats2.n_processed == 3


def test_retained_row_round_trips_auction_model() -> None:
    """The offline probes do ``Auction.model_validate(raw_auction)`` on
    retained rows — the slim form (referenced orders only + ``_retained``
    marker) must still parse into a typed Auction with resolvable trade UIDs
    (reviewer-requested contract lock)."""
    from scripts.retention_loop import retain_row
    from src.models.auction import Auction

    raw = {
        "id": "7558000",
        "tokens": {"0x" + "aa" * 20: {"decimals": 18, "referencePrice": "1"}},
        "orders": [
            {
                "uid": "0x" + "ab" * 56,
                "owner": "0x" + "11" * 20,
                "sellToken": "0x" + "aa" * 20,
                "buyToken": "0x" + "bb" * 20,
                "sellAmount": "1000",
                "buyAmount": "900",
                "validTo": 2_000_000_000,
                "kind": "sell",
                "partiallyFillable": False,
                "class": "limit",
            },
            {
                "uid": "0x" + "cd" * 56,
                "owner": "0x" + "22" * 20,
                "sellToken": "0x" + "bb" * 20,
                "buyToken": "0x" + "aa" * 20,
                "sellAmount": "500",
                "buyAmount": "400",
                "validTo": 2_000_000_000,
                "kind": "sell",
                "partiallyFillable": False,
                "class": "limit",
            },
        ],
        "deadline": "2030-01-01T00:00:00Z",
    }
    slim = retain_row(raw, {("0x" + "ab" * 56)})

    auction = Auction.model_validate(slim)
    assert len(auction.orders) == 1
    assert auction.orders[0].uid == "0x" + "ab" * 56
    assert auction.orders[0].sell_amount == 1000
