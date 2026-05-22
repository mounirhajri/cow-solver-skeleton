"""Persist shadow auction + solution attempts to Postgres.

Called from /solve via BackgroundTasks — must never raise; logs and swallows.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from src.log import get_logger
from src.models.auction import Auction
from src.persistence.db import get_session_factory
from src.persistence.models import ShadowAuction, ShadowSolution
from src.solver.orchestrator import AttemptRecord

log = get_logger(__name__)


async def persist_shadow_attempt(
    auction: Auction,
    attempts: list[AttemptRecord],
    raw_competition: dict[str, object] | None = None,
) -> None:
    """Upsert auction row + insert one row per strategy attempt."""
    Session = get_session_factory()
    auction_id = int(auction.id)

    async with Session() as session:
        # Dialect-agnostic upsert: check-then-insert (works with both Postgres and sqlite)
        existing = (
            await session.execute(
                select(ShadowAuction).where(ShadowAuction.auction_id == auction_id)
            )
        ).first()

        if existing is None:
            session.add(
                ShadowAuction(
                    auction_id=auction_id,
                    polled_at=datetime.now(UTC),
                    n_orders=len(auction.orders),
                    raw_competition=raw_competition or {},
                    raw_auction=auction.model_dump(mode="json", by_alias=True),
                )
            )

        for a in attempts:
            session.add(
                ShadowSolution(
                    auction_id=auction_id,
                    strategy=a.strategy,
                    status=a.status,
                    latency_ms=a.latency_ms,
                    solution=a.solution,
                    error=a.error,
                )
            )

        await session.commit()


async def persist_shadow_attempt_safe(
    auction: Auction,
    attempts: list[AttemptRecord],
    raw_competition: dict[str, object] | None = None,
) -> None:
    """Same as persist_shadow_attempt but never raises (logs and swallows).

    For use in FastAPI BackgroundTasks where exceptions would be lost.
    """
    try:
        await persist_shadow_attempt(auction, attempts, raw_competition)
    except Exception as e:  # noqa: BLE001
        log.error("shadow_persist_failed", auction_id=str(auction.id), error=str(e))
