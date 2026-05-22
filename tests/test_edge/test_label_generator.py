from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from edge.classifier.label_generator import class_distribution, generate_labels
from src.persistence.models import Base, TokenOutcome


@pytest.fixture
async def session_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("edge.classifier.label_generator.get_session_factory", lambda: factory)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_no_outcomes_returns_empty(session_factory):
    labeled = await generate_labels()
    assert labeled == []


@pytest.mark.asyncio
async def test_legit_label(session_factory):
    # Insert a parent auction row (FK target)
    from src.persistence.models import ShadowAuction

    now = datetime.now(UTC)
    async with session_factory() as s:
        s.add(ShadowAuction(
            auction_id=1, polled_at=now, n_orders=0,
            raw_competition={}, raw_auction={},
        ))
        for i in range(5):
            s.add(TokenOutcome(
                id=i + 1, token_address="0xa", auction_id=1,
                appeared_in_winner=True, appeared_in_ours=False,
                caused_revert=False, observed_at=now,
            ))
        await s.commit()
    labeled = await generate_labels()
    assert len(labeled) == 1
    assert labeled[0].label == "legit"
    assert labeled[0].n_winner_appearances == 5


@pytest.mark.asyncio
async def test_scam_via_reverts(session_factory):
    from src.persistence.models import ShadowAuction

    now = datetime.now(UTC)
    async with session_factory() as s:
        s.add(ShadowAuction(
            auction_id=1, polled_at=now, n_orders=0,
            raw_competition={}, raw_auction={},
        ))
        for i in range(2):
            s.add(TokenOutcome(
                id=i + 1, token_address="0xb", auction_id=1,
                appeared_in_winner=False, appeared_in_ours=True,
                caused_revert=True, observed_at=now,
            ))
        await s.commit()
    labeled = await generate_labels()
    assert labeled[0].label == "scam"


@pytest.mark.asyncio
async def test_class_distribution():
    from edge.classifier.label_generator import LabeledToken

    labels = [
        LabeledToken("0xa", "legit", 5, 0),
        LabeledToken("0xb", "scam", 0, 2),
        LabeledToken("0xc", "unknown", 1, 0),
        LabeledToken("0xd", "legit", 6, 0),
    ]
    dist = class_distribution(labels)
    assert dist == {"legit": 2, "scam": 1, "unknown": 1}
