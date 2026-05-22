"""SQLAlchemy ORM models for shadow data + classifier features.

Tables:
- shadow_auctions       — every auction we polled
- shadow_solutions      — our solver attempts (one row per strategy per auction)
- shadow_winners        — winner solution per auction (from CoW competition API)
- token_outcomes        — per-token outcome (label source for classifier)
- token_features        — token feature snapshot (input for classifier)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ShadowAuction(Base):
    __tablename__ = "shadow_auctions"

    auction_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chain: Mapped[str] = mapped_column(String(20), default="arbitrum_one")
    polled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    n_orders: Mapped[int] = mapped_column(Integer)
    raw_competition: Mapped[dict[str, Any]] = mapped_column(JSON)
    raw_auction: Mapped[dict[str, Any]] = mapped_column(JSON)


class ShadowSolution(Base):
    __tablename__ = "shadow_solutions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    auction_id: Mapped[int] = mapped_column(ForeignKey("shadow_auctions.auction_id"), index=True)
    strategy: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    solution: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ShadowWinner(Base):
    __tablename__ = "shadow_winners"

    auction_id: Mapped[int] = mapped_column(
        ForeignKey("shadow_auctions.auction_id"), primary_key=True
    )
    winner_solver: Mapped[str] = mapped_column(Text)
    score: Mapped[int | None] = mapped_column(Numeric(40, 0))
    raw_solution: Mapped[dict[str, Any]] = mapped_column(JSON)


class TokenOutcome(Base):
    __tablename__ = "token_outcomes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token_address: Mapped[str] = mapped_column(String(42), index=True)
    auction_id: Mapped[int] = mapped_column(ForeignKey("shadow_auctions.auction_id"))
    appeared_in_winner: Mapped[bool] = mapped_column(Boolean)
    appeared_in_ours: Mapped[bool] = mapped_column(Boolean)
    caused_revert: Mapped[bool] = mapped_column(Boolean, default=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class TokenFeatures(Base):
    __tablename__ = "token_features"

    token_address: Mapped[str] = mapped_column(String(42), primary_key=True)
    decimals: Mapped[int | None] = mapped_column(Integer)
    contract_verified: Mapped[bool | None] = mapped_column(Boolean)
    has_transfer_tax: Mapped[bool | None] = mapped_column(Boolean)
    bridge_canonical: Mapped[bool | None] = mapped_column(Boolean)
    tvl_usd: Mapped[float | None] = mapped_column(Numeric(20, 2))
    volume_24h_usd: Mapped[float | None] = mapped_column(Numeric(20, 2))
    pool_count_v2: Mapped[int | None] = mapped_column(Integer)
    pool_count_v3: Mapped[int | None] = mapped_column(Integer)
    pool_count_camelot: Mapped[int | None] = mapped_column(Integer)
    holder_count: Mapped[int | None] = mapped_column(Integer)
    top10_concentration: Mapped[float | None] = mapped_column(Numeric(5, 4))
    age_blocks: Mapped[int | None] = mapped_column(Integer)
    on_arbitrum_token_list: Mapped[bool | None] = mapped_column(Boolean)
    on_coingecko: Mapped[bool | None] = mapped_column(Boolean)
    last_refreshed: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
