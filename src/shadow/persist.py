"""Persist shadow auction + solution attempts to Postgres.

Called from /solve via BackgroundTasks — must never raise; logs and swallows.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from src.log import get_logger
from src.models.auction import Auction
from src.persistence.db import get_session_factory
from src.persistence.models import ShadowAuction, ShadowSolution
from src.shadow.scoring import (
    compute_solution_score,
    extract_native_prices,
    orders_by_uid_from_auction,
    score_at_external_prices,
)
from src.solver.orchestrator import AttemptRecord

log = get_logger(__name__)

# Skip persisting microscopic-surplus solutions. These are mathematically
# valid matches against tiny counter-orders (e.g. $10M TWAP vs $0.001 micro-
# order in same auction) that pass limit checks via partial downscale but
# produce surplus below any reasonable submission threshold. The estimator
# already filters them via winner_score comparison — we just stop creating
# the rows in the first place. Saves shadow_solutions row growth without
# losing any economically relevant data.
EPSILON_WEI = 10**12  # 1 microETH ≈ $0.002 at €1800/ETH

# Upper cap for phantom-suspect surplus.  On Arbitrum One the largest
# legitimate single-auction surplus we have observed (post-ghost-filter)
# is 0.094 ETH on USDC/USDT — anything above 1 ETH is virtually certain
# to be a scoring bug, most commonly router-v2 emitting CIP-14 surplus
# derived from arb-style high-surplus order limits that no AMM can
# actually clear at that volume.  Rather than DROP the row (we want
# observability — every above-cap emission is a phantom we should be
# able to count), we NULL the score and emit a structured log.
# analyze_competitors + estimate_economics already treat NULL as
# "ignore", so this surgically removes the pollution without losing
# the diagnostic signal.
#
# 1 ETH chosen empirically: bipartite max observed = 0.094 ETH,
# multi-party single solve = 0.027 ETH, real router emissions ~0.01 ETH.
# Phantom router emissions cluster at 6+ ETH.  A 10× margin between
# real (0.1 ETH) and cap (1 ETH) is comfortable.
EPSILON_HIGH_WEI = 10**18  # 1 ETH


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

        n_sub_dust_skipped = 0
        n_phantom_above_cap = 0
        for a in attempts:
            score: int | None = None
            if a.strategy == "naive":
                # KNOWN-BAD score path: price_refiner.py:168-183 uses each
                # token's oracle reference_price as the clearing price, which
                # produces phantom CIP-14 surplus equal to the order's
                # OTM-headroom at oracle. Naive is never submitted —
                # orchestrator.py:150-152 excludes it from the composer
                # candidate set — but persisting its score here pollutes any
                # downstream aggregate analytics (estimate_economics,
                # analyze_router_solutions, percentile dashboards).  Keep the
                # solution JSON and status for observability; leave score NULL.
                pass
            elif a.solution and uid_map and native_prices:
                with contextlib.suppress(Exception):  # noqa: BLE001
                    raw_score = compute_solution_score(a.solution, uid_map, native_prices)
                    if raw_score <= 0:
                        # Zero/negative → NULL: solution is valid but unprofitable.
                        score = None
                    elif raw_score >= EPSILON_HIGH_WEI:
                        # Phantom-suspect upper cap.  Most commonly router-v2
                        # emitting arb-style scores derived from high-surplus
                        # order limits (no AMM clears at that volume).  Keep
                        # the row for observability; NULL the score so it
                        # doesn't pollute analytics.  Log the original score
                        # + strategy so we can tune the threshold from data.
                        score = None
                        n_phantom_above_cap += 1
                        log.info(
                            "shadow_score_above_upper_cap",
                            auction_id=auction_id,
                            strategy=a.strategy,
                            raw_score_wei=str(raw_score),
                            cap_wei=str(EPSILON_HIGH_WEI),
                        )
                    else:
                        score = raw_score

            # Sub-dust filter: skip persisting solutions with a computed score
            # below EPSILON_WEI. These originate from bipartite-matcher downscaling
            # large TWAP orders against tiny counter-orders (Ghost Order pattern).
            # Rows with solution=None (errors/timeouts) or score=None (computation
            # failed / zero surplus) are always kept — only skip when score is a
            # positive integer below EPSILON_WEI.
            if a.solution is not None and score is not None and score < EPSILON_WEI:
                n_sub_dust_skipped += 1
                continue

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

        if n_sub_dust_skipped:
            log.info(
                "sub_dust_solutions_skipped",
                auction_id=auction_id,
                n_sub_dust_skipped=n_sub_dust_skipped,
            )
        if n_phantom_above_cap:
            log.info(
                "phantom_above_cap_nulled",
                auction_id=auction_id,
                n_phantom_above_cap=n_phantom_above_cap,
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
            try:
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
            except IntegrityError:
                await session.rollback()
                # Row was inserted concurrently — that's fine, proceed to children.
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

        # ── Recompute our scores at the winner's clearing prices ─────────────
        # Phase 4a: isolates "wrong trades chosen" from "our prices off".
        # Must never break winner/outcome persistence — wrap defensively.
        try:
            await _backfill_winner_price_scores_for_auction(
                session, auction_id, winner_sol, raw_competition, auction_payload
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "winner_price_score_recompute_failed",
                auction_id=auction_id,
                error=str(e),
            )

        await session.commit()


async def _backfill_winner_price_scores_for_auction(
    session: Any,
    auction_id: int,
    winner_sol: dict[str, Any] | None,
    raw_competition: dict[str, Any],
    auction_payload: dict[str, Any],
) -> None:
    """Update ``score_vs_winner_prices_wei`` for every solved row of this auction.

    Only fills rows where the column is still NULL (idempotent).  Skips
    silently when prerequisites (winner clearingPrices, native prices, orders)
    cannot be assembled — the dedicated backfill script handles those.
    """
    if winner_sol is None:
        return

    # CoW API uses camelCase clearingPrices; our own solver dict uses "prices".
    clearing_prices = winner_sol.get("clearingPrices") or winner_sol.get("prices") or {}
    if not clearing_prices:
        return

    # Native prices: prefer raw_competition.auction.prices, fall back to
    # raw_auction.tokens[*].referencePrice (mirrors persist_shadow_attempt).
    native_prices = extract_native_prices(raw_competition or {})
    if not native_prices:
        for addr, tok in (auction_payload or {}).get("tokens", {}).items():
            ref = (
                tok.get("referencePrice")
                if isinstance(tok, dict)
                else None
            ) or (tok.get("reference_price") if isinstance(tok, dict) else None)
            if ref:
                with contextlib.suppress(ValueError, TypeError):
                    native_prices[addr.lower()] = int(ref)
    if not native_prices:
        return

    # auction_payload may be a placeholder ({"backfilled": true}) from
    # backfill flows — there are no orders to score against here; the
    # dedicated backfill script handles those rows via the CoW API.
    uid_map = orders_by_uid_from_auction(auction_payload or {})
    if not uid_map:
        return

    q = await session.execute(
        select(ShadowSolution.id, ShadowSolution.solution)
        .where(ShadowSolution.auction_id == auction_id)
        .where(ShadowSolution.status == "solved")
        .where(ShadowSolution.solution.is_not(None))
        .where(ShadowSolution.score_vs_winner_prices_wei.is_(None))
    )
    rows = q.all()

    for sol_id, solution in rows:
        if not isinstance(solution, dict):
            continue
        try:
            raw_score = score_at_external_prices(
                solution, uid_map, native_prices, clearing_prices
            )
        except Exception:  # noqa: BLE001
            continue
        # Keep 0 as NULL (consistent with our_score_wei handling).
        value = raw_score if raw_score > 0 else None
        await session.execute(
            update(ShadowSolution)
            .where(ShadowSolution.id == sol_id)
            .where(ShadowSolution.score_vs_winner_prices_wei.is_(None))
            .values(score_vs_winner_prices_wei=value)
        )


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
            try:
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
            except IntegrityError:
                await session.rollback()
                # Row inserted concurrently — safe to continue to child insert.

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
