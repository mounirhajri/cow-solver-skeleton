"""Tests for the ShadowCompetitor model and its UniqueConstraint."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.persistence.models import ShadowAuction, ShadowCompetitor


@pytest.fixture
async def session_factory():
    """In-memory SQLite session factory with shadow_auctions + shadow_competitors tables."""
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
            CREATE TABLE shadow_competitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                auction_id INTEGER NOT NULL REFERENCES shadow_auctions(auction_id),
                solver_name TEXT NOT NULL,
                solver_address TEXT NOT NULL,
                score NUMERIC,
                ranking INTEGER NOT NULL,
                is_winner INTEGER NOT NULL DEFAULT 0,
                filtered_out INTEGER NOT NULL DEFAULT 0,
                clearing_prices TEXT NOT NULL DEFAULT '{}',
                orders TEXT NOT NULL DEFAULT '[]',
                polled_at TEXT NOT NULL,
                UNIQUE(auction_id, solver_address)
            )
        """))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _insert_auction(factory: async_sessionmaker, auction_id: int = 1) -> None:
    async with factory() as session:
        session.add(
            ShadowAuction(
                auction_id=auction_id,
                polled_at=datetime.now(UTC),
                n_orders=10,
                raw_competition={},
                raw_auction={},
            )
        )
        await session.commit()


def _make_competitor(
    auction_id: int = 1,
    solver_name: str = "helixbox-solve",
    solver_address: str = "0x" + "a" * 40,
    ranking: int = 1,
    is_winner: bool = True,
    score: int | None = 1_000_000,
) -> ShadowCompetitor:
    return ShadowCompetitor(
        auction_id=auction_id,
        solver_name=solver_name,
        solver_address=solver_address,
        score=score,
        ranking=ranking,
        is_winner=is_winner,
        filtered_out=False,
        clearing_prices={"0x" + "b" * 40: "1000000000000000000"},
        orders=[{"id": "0x" + "c" * 144, "sellAmount": "1000", "buyAmount": "900"}],
        polled_at=datetime.now(UTC),
    )


class TestShadowCompetitorFields:
    async def test_all_fields_persisted(self, session_factory) -> None:
        await _insert_auction(session_factory)
        competitor = _make_competitor(
            solver_name="kaisersolver-solve",
            solver_address="0x" + "d" * 40,
            ranking=2,
            is_winner=False,
            score=999_000,
        )
        async with session_factory() as session:
            session.add(competitor)
            await session.commit()

        async with session_factory() as session:
            row = (await session.execute(select(ShadowCompetitor))).scalar_one()

        assert row.solver_name == "kaisersolver-solve"
        assert row.solver_address == "0x" + "d" * 40
        assert int(row.score) == 999_000
        assert row.ranking == 2
        assert bool(row.is_winner) is False
        assert bool(row.filtered_out) is False
        assert isinstance(row.clearing_prices, dict)
        assert isinstance(row.orders, list)

    async def test_score_nullable(self, session_factory) -> None:
        await _insert_auction(session_factory)
        competitor = _make_competitor(score=None)
        async with session_factory() as session:
            session.add(competitor)
            await session.commit()

        async with session_factory() as session:
            row = (await session.execute(select(ShadowCompetitor))).scalar_one()
        assert row.score is None


class TestUniqueConstraint:
    async def test_duplicate_auction_solver_raises(self, session_factory) -> None:
        await _insert_auction(session_factory)
        addr = "0x" + "e" * 40
        c1 = _make_competitor(solver_address=addr, ranking=1)
        c2 = _make_competitor(solver_address=addr, ranking=1)

        async with session_factory() as session:
            session.add(c1)
            await session.commit()

        with pytest.raises((IntegrityError, Exception)):
            async with session_factory() as session:
                session.add(c2)
                await session.commit()

    async def test_same_solver_different_auctions_allowed(self, session_factory) -> None:
        await _insert_auction(session_factory, auction_id=1)
        await _insert_auction(session_factory, auction_id=2)

        addr = "0x" + "f" * 40
        c1 = _make_competitor(auction_id=1, solver_address=addr)
        c2 = _make_competitor(auction_id=2, solver_address=addr)

        async with session_factory() as session:
            session.add(c1)
            session.add(c2)
            await session.commit()

        async with session_factory() as session:
            rows = (await session.execute(select(ShadowCompetitor))).scalars().all()
        assert len(rows) == 2

    async def test_different_solver_same_auction_allowed(self, session_factory) -> None:
        await _insert_auction(session_factory)

        c1 = _make_competitor(solver_address="0x" + "1" * 40, solver_name="solver-a")
        c2 = _make_competitor(solver_address="0x" + "2" * 40, solver_name="solver-b")

        async with session_factory() as session:
            session.add(c1)
            session.add(c2)
            await session.commit()

        async with session_factory() as session:
            rows = (await session.execute(select(ShadowCompetitor))).scalars().all()
        assert len(rows) == 2


class TestOnConflictDoNothing:
    async def test_insert_on_conflict_do_nothing_is_idempotent(
        self, session_factory
    ) -> None:
        """INSERT … ON CONFLICT DO NOTHING must not raise and must not duplicate rows."""
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        await _insert_auction(session_factory)
        addr = "0x" + "a" * 40
        row_data = {
            "auction_id": 1,
            "solver_name": "helixbox-solve",
            "solver_address": addr,
            "score": 1_000_000,
            "ranking": 1,
            "is_winner": True,
            "filtered_out": False,
            "clearing_prices": {},
            "orders": [],
            "polled_at": datetime.now(UTC),
        }

        async with session_factory() as session:
            stmt = sqlite_insert(ShadowCompetitor).values([row_data])
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["auction_id", "solver_address"]
            )
            await session.execute(stmt)
            await session.commit()

        # Second identical insert — must not raise or duplicate.
        async with session_factory() as session:
            stmt = sqlite_insert(ShadowCompetitor).values([row_data])
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["auction_id", "solver_address"]
            )
            await session.execute(stmt)
            await session.commit()

        async with session_factory() as session:
            rows = (await session.execute(select(ShadowCompetitor))).scalars().all()
        assert len(rows) == 1
