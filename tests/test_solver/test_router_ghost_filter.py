"""Tests for the RouterSolver ↔ Phase-A ghost_detector wiring.

Phase A had been wired into BipartiteMatcher and CoWMatchingSolver but
not RouterSolver, so the strategy was systematically picking persistent
loose-limit phantom orders (e.g. one user's WBTC TWAP UID seen in 1451
auctions with 0 settlements — caught by Phase A but never consulted by
Router). Discovered 2026-05-27 post-Phase-0b shadow eval. These tests
lock down the gate to prevent the regression.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.models.auction import Auction
from src.models.order import Order
from src.routing.v3_batched import V3BatchedQuote
from src.solver.base import NoSolution
from src.solver.router import RouterSolver

USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
WBTC = "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f"


def _order(uid: str, sell: str = USDC, buy: str = WETH) -> Order:
    return Order.model_validate({
        "uid": uid, "sellToken": sell, "buyToken": buy,
        "sellAmount": "1000000", "buyAmount": "300000000000000",
        "validTo": 2_000_000_000, "kind": "sell", "owner": "0x" + "1" * 40,
        "partiallyFillable": False, "class": "limit",
    })


def _auction(orders: list[Order]) -> Auction:
    return Auction(id="42", orders=orders, tokens={}, deadline="2026-12-31T00:00:00Z")


def _ghost_detector(ghost_uids: set[str]) -> object:
    """Minimal stub of DynamicGhostDetector — is_ghost(order) returns True
    when the order's UID is in the configured set."""

    class _D:
        async def is_ghost(self, order: Order) -> bool:
            return order.uid in ghost_uids

    return _D()


@pytest.mark.asyncio
async def test_ghost_filter_drops_listed_uids_before_quoting(monkeypatch) -> None:
    """The exact bug we shipped this for: a persistent loose-limit order
    that Phase A has detected as a ghost (1+ auctions, 0 fills) must be
    dropped before the surplus-headroom sort. If the filter runs, the
    only remaining order in this auction gets picked; if not, the ghost
    sits at top-N and dominates the V3-batched path."""
    ghost_uid = "0x_ghost_uid"
    real_uid = "0x_real_uid"
    detector = _ghost_detector({ghost_uid})

    quoted_uids: list[str] = []

    async def mock_batched(_mc, paths, *args, **kwargs):
        # Record which UIDs reached the quoter — proves the filter
        # ran before path construction, not just inside the quoter.
        for p in paths:
            if p.order_uid not in quoted_uids:
                quoted_uids.append(p.order_uid)
        return [V3BatchedQuote(path=p, amount_out=400_000_000_000_000) for p in paths]

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    solver = RouterSolver(
        multicall=MagicMock(),
        intermediates=[],
        ghost_detector=detector,
    )
    result = await solver.solve(_auction([_order(ghost_uid), _order(real_uid)]))

    assert ghost_uid not in quoted_uids, (
        f"ghost UID {ghost_uid} reached the V3 quoter — filter did not run "
        f"before path construction. quoted={quoted_uids}"
    )
    assert real_uid in quoted_uids
    assert not isinstance(result, NoSolution)
    assert {t.order_uid for t in result.trades} == {real_uid}


@pytest.mark.asyncio
async def test_no_detector_means_no_filtering(monkeypatch) -> None:
    """ghost_detector=None must be a no-op — public-clone / Phase-0
    deployments without the edge module construct RouterSolver this way."""
    async def mock_batched(_mc, paths, *args, **kwargs):
        return [V3BatchedQuote(path=p, amount_out=400_000_000_000_000) for p in paths]

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    solver = RouterSolver(multicall=MagicMock(), intermediates=[])  # no ghost_detector
    result = await solver.solve(_auction([_order("0xany")]))
    assert not isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_detector_called_for_every_order(monkeypatch) -> None:
    """Defensive: every order in the auction MUST be passed through
    is_ghost() — partial-coverage would let ghosts slip through if the
    filter loop drops out early."""
    seen_uids: list[str] = []

    class _RecordingDetector:
        async def is_ghost(self, order: Order) -> bool:
            seen_uids.append(order.uid)
            return False

    async def mock_batched(_mc, paths, *args, **kwargs):
        return [V3BatchedQuote(path=p, amount_out=400_000_000_000_000) for p in paths]

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    solver = RouterSolver(
        multicall=MagicMock(), intermediates=[],
        ghost_detector=_RecordingDetector(),
    )
    await solver.solve(_auction([_order("0x_a"), _order("0x_b"), _order("0x_c")]))

    assert seen_uids == ["0x_a", "0x_b", "0x_c"]


@pytest.mark.asyncio
async def test_all_orders_filtered_returns_no_solution(monkeypatch) -> None:
    """When every order in the auction is a known ghost, the post-filter
    candidate list is empty — RouterSolver must return NoSolution rather
    than attempting to quote nothing."""
    detector = _ghost_detector({"0x_g1", "0x_g2", "0x_g3"})

    async def mock_batched(_mc, paths, *args, **kwargs):
        return [V3BatchedQuote(path=p, amount_out=0) for p in paths]

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)

    solver = RouterSolver(multicall=MagicMock(), intermediates=[], ghost_detector=detector)
    result = await solver.solve(_auction([_order("0x_g1"), _order("0x_g2"), _order("0x_g3")]))
    assert isinstance(result, NoSolution)
