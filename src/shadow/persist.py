"""Persist shadow auction + solution attempts to Postgres.

Called from /solve via BackgroundTasks — must never raise; logs and swallows.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.log import get_logger
from src.models.auction import Auction
from src.persistence.db import get_session_factory
from src.persistence.models import ShadowAuction, ShadowSolution
from src.shadow.scoring import (
    compute_solution_score,
    extract_native_prices,
    orders_by_uid_from_auction,
)
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
        # Upsert shadow_auctions: check-then-insert with IntegrityError guard.
        # Race: both this BackgroundTask and persist_winner_and_outcomes_safe
        # (called directly by the shadow poller) may attempt the insert
        # concurrently.  The second insert raises IntegrityError which we
        # catch and ignore — the row already exists, which is fine.
        existing = (
            await session.execute(
                select(ShadowAuction).where(ShadowAuction.auction_id == auction_id)
            )
        ).first()

        if existing is None:
            try:
                session.add(
                    ShadowAuction(
                        auction_id=auction_id,
                        polled_at=datetime.now(UTC),
                        n_orders=len(auction.orders),
                        raw_competition=raw_competition or {},
                        raw_auction=auction.model_dump(mode="json", by_alias=True),
                    )
                )
                await session.flush()
            except IntegrityError:
                await session.rollback()
                # Row inserted by the concurrent path — continue to solution inserts.

        # Pre-compute CIP-14 scores for all attempts that produced solutions.
        uid_map = orders_by_uid_from_auction(auction)
        native_prices = extract_native_prices(raw_competition or {})
        if not native_prices:
            # Fallback: extract from auction.tokens.reference_price (always present
            # in shadow-poller payloads even when raw_competition is unavailable).
            native_prices = {
                addr.lower(): int(tok.reference_price)
                for addr, tok in auction.tokens.items()
                if tok.reference_price
            }

        for a in attempts:
            score: int | None = None
            if a.solution and uid_map and native_prices:
                with contextlib.suppress(Exception):  # noqa: BLE001
                    raw_score = compute_solution_score(a.solution, uid_map, native_prices)
                    # Keep 0 as NULL (no real surplus); only store positive scores.
                    # A score of 0 means the solution is valid but unprofitable.
                    score = raw_score if raw_score > 0 else None
            session.add(
                ShadowSolution(
                    auction_id=auction_id,
                    strategy=a.strategy,
                    status=a.status,
                    latency_ms=a.latency_ms,
                    solution=a.solution,
                    error=a.error,
                    our_score_wei=score,
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


async def persist_winner_and_outcomes(
    auction_id: int,
    raw_competition: dict[str, Any],
    auction_payload: dict[str, Any],
    our_solution: dict[str, Any] | None,
) -> None:
    """Extract winner + per-token outcomes from a competition response and persist.

    Idempotent on ``auction_id`` for the winner; token outcomes append a row per
    (token, auction) pair.

    Also ensures the ``shadow_auctions`` FK target exists before inserting
    children — handles the race with the /solve BackgroundTask path from Task 1.2.
    """
    from src.persistence.models import ShadowWinner, TokenOutcome
    from src.shadow.token_outcomes import extract_token_outcomes

    solutions = raw_competition.get("solutions") or []
    winner_sol = next(
        (s for s in solutions if s.get("isWinner") or s.get("ranking") == 1),
        None,
    )

    Session = get_session_factory()

    # ── Ensure the shadow_auctions row exists (FK target) ─────────────────────
    async with Session() as session:
        existing_auction = await session.execute(
            select(ShadowAuction).where(ShadowAuction.auction_id == auction_id)
        )
        if existing_auction.scalar_one_or_none() is None:
            # n_orders: prefer the full count from raw_competition.auction.orders
            # (which has uids even when we didn't fetch them); fall back to
            # auction_payload.orders length.
            comp_auction = raw_competition.get("auction") or {}
            n_orders = len(comp_auction.get("orders") or []) or len(
                auction_payload.get("orders", [])
            )
            session.add(
                ShadowAuction(
                    auction_id=auction_id,
                    polled_at=datetime.now(UTC),
                    n_orders=n_orders,
                    raw_competition=raw_competition,
                    raw_auction=auction_payload,
                )
            )
            await session.flush()  # ensure FK target exists before children
        await session.commit()

    # ── Winner + token outcomes ───────────────────────────────────────────────
    async with Session() as session:
        if winner_sol is not None:
            existing = await session.execute(
                select(ShadowWinner).where(ShadowWinner.auction_id == auction_id)
            )
            if existing.scalar_one_or_none() is None:
                score_raw = winner_sol.get("score")
                try:
                    score = int(score_raw) if score_raw is not None else None
                except (ValueError, TypeError):
                    score = None
                session.add(
                    ShadowWinner(
                        auction_id=auction_id,
                        winner_solver=str(winner_sol.get("solver", "unknown")),
                        score=score,
                        raw_solution=winner_sol,
                    )
                )

        outcomes = extract_token_outcomes(auction_payload, winner_sol, our_solution)
        for o in outcomes:
            session.add(
                TokenOutcome(
                    auction_id=auction_id,
                    token_address=o["token_address"],
                    appeared_in_winner=o["appeared_in_winner"],
                    appeared_in_ours=o["appeared_in_ours"],
                    caused_revert=o["caused_revert"],
                )
            )

        await session.commit()


async def persist_winner_and_outcomes_safe(
    auction_id: int,
    raw_competition: dict[str, Any],
    auction_payload: dict[str, Any],
    our_solution: dict[str, Any] | None,
) -> None:
    """Never-raise wrapper for use in shadow_poller (no FastAPI BackgroundTasks)."""
    try:
        await persist_winner_and_outcomes(
            auction_id, raw_competition, auction_payload, our_solution
        )
    except Exception as e:  # noqa: BLE001
        log.error("winner_persist_failed", auction_id=auction_id, error=str(e))


async def persist_skipped_auction(
    auction_id: int,
    auction_payload: dict[str, Any],
    raw_competition: dict[str, Any],
    n_orders: int,
) -> None:
    """Record an auction we didn't /solve because it had too many orders.

    Ensures a ``shadow_auctions`` row exists (idempotent), then inserts a
    ``shadow_solutions`` row with ``strategy="poller-skipped"`` and
    ``status="skipped"`` so the analyzer knows we saw the auction.
    """
    Session = get_session_factory()
    async with Session() as session:
        existing = await session.execute(
            select(ShadowAuction).where(ShadowAuction.auction_id == auction_id)
        )
        if existing.scalar_one_or_none() is None:
            session.add(
                ShadowAuction(
                    auction_id=auction_id,
                    polled_at=datetime.now(UTC),
                    n_orders=n_orders,
                    raw_competition=raw_competition,
                    raw_auction=auction_payload,
                )
            )
            await session.flush()  # ensure FK target exists before child insert

        session.add(
            ShadowSolution(
                auction_id=auction_id,
                strategy="poller-skipped",
                status="skipped",
                latency_ms=None,
                solution=None,
                error=f"auction_too_large_to_solve: {n_orders} orders",
            )
        )
        await session.commit()


async def persist_skipped_auction_safe(
    auction_id: int,
    auction_payload: dict[str, Any],
    raw_competition: dict[str, Any],
    n_orders: int,
) -> None:
    """Never-raise wrapper around persist_skipped_auction."""
    try:
        await persist_skipped_auction(auction_id, auction_payload, raw_competition, n_orders)
    except Exception as e:  # noqa: BLE001
        log.error("skipped_persist_failed", auction_id=auction_id, error=str(e))
