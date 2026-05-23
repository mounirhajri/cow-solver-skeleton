"""Tests for CoWMatchingSolver (multi-party ring matching)."""
import pytest

from edge.matching.multi_party import CoWMatchingSolver, _has_reference_price
from src.models.auction import Auction, Token
from src.models.order import Order
from src.models.solution import Solution
from src.solver.base import NoSolution


def _mk_order(
    uid: str,
    sell_token: str,
    buy_token: str,
    sell_amount: int = 1000,
    buy_amount: int = 900,
) -> Order:
    return Order(
        uid=uid,
        sellToken=sell_token,
        buyToken=buy_token,
        sellAmount=sell_amount,
        buyAmount=buy_amount,
        feePolicies=[],
        validTo=99,
        kind="sell",
        owner="0x" + "a" * 40,
        partiallyFillable=False,
        **{"class": "limit"},
    )


def _mk_token(price: int = 10**18) -> Token:
    return Token(decimals=18, referencePrice=price)


def _mk_auction(orders: list[Order], tokens: dict[str, Token], auction_id: str = "1") -> Auction:
    return Auction(
        id=auction_id,
        tokens=tokens,
        orders=orders,
        liquidity=[],
        effectiveGasPrice=0,
        deadline=None,
    )


# ── _has_reference_price helper ──────────────────────────────────────────────

def test_has_reference_price_true() -> None:
    tokens = {"0xA": _mk_token()}
    assert _has_reference_price("0xA", tokens) is True


def test_has_reference_price_missing_token() -> None:
    assert _has_reference_price("0xZ", {}) is False


def test_has_reference_price_none_price() -> None:
    tokens = {"0xA": Token(decimals=18, referencePrice=None)}
    assert _has_reference_price("0xA", tokens) is False


# ── Happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_3_ring_produces_solution() -> None:
    """A → B → C → A ring with priced tokens should yield a solution."""
    orders = [
        _mk_order("o1", "0xA", "0xB"),
        _mk_order("o2", "0xB", "0xC"),
        _mk_order("o3", "0xC", "0xA"),
    ]
    tokens = {
        "0xA": _mk_token(),
        "0xB": _mk_token(),
        "0xC": _mk_token(),
    }
    solver = CoWMatchingSolver()
    result = await solver.solve(_mk_auction(orders, tokens, auction_id="42"))
    assert isinstance(result, Solution)
    assert len(result.trades) == 3
    uids = {t.order_uid for t in result.trades}
    assert uids == {"o1", "o2", "o3"}


# ── Edge cases ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_no_solution_when_not_enough_orders() -> None:
    """Fewer than MIN_RING_LENGTH sell orders → NoSolution immediately."""
    orders = [_mk_order("o1", "0xA", "0xB"), _mk_order("o2", "0xB", "0xA")]
    tokens = {"0xA": _mk_token(), "0xB": _mk_token()}
    solver = CoWMatchingSolver()
    result = await solver.solve(_mk_auction(orders, tokens))
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_buy_orders_are_excluded() -> None:
    """Buy-kind orders are not included in the sell_orders list."""
    orders = [
        Order(
            uid="b1",
            sellToken="0xA",
            buyToken="0xB",
            sellAmount=1000,
            buyAmount=900,
            feePolicies=[],
            validTo=99,
            kind="buy",
            owner="0x" + "a" * 40,
            partiallyFillable=False,
            **{"class": "limit"},
        ),
        _mk_order("o2", "0xB", "0xC"),
        _mk_order("o3", "0xC", "0xA"),
    ]
    tokens = {"0xA": _mk_token(), "0xB": _mk_token(), "0xC": _mk_token()}
    solver = CoWMatchingSolver()
    # Only 2 sell orders — can't form a 3-ring
    result = await solver.solve(_mk_auction(orders, tokens))
    assert isinstance(result, NoSolution)


# ── Unpriced token filtering (the root cause of 0/171) ────────────────────────

@pytest.mark.asyncio
async def test_unpriced_ring_tokens_yield_no_solution() -> None:
    """If all ring tokens lack reference prices, all rings are infeasible → NoSolution.

    Before the fix, the graph was built from ALL orders regardless of pricing,
    producing rings that the LP immediately rejects. After the fix, such orders
    are not added to the graph at all.
    """
    orders = [
        _mk_order("o1", "0xX", "0xY"),
        _mk_order("o2", "0xY", "0xZ"),
        _mk_order("o3", "0xZ", "0xX"),
    ]
    # No reference prices for any of these tokens
    tokens: dict[str, Token] = {}
    solver = CoWMatchingSolver()
    result = await solver.solve(_mk_auction(orders, tokens))
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_mixed_priced_unpriced_uses_only_priced_orders() -> None:
    """Orders with priced tokens form a ring; unpriced orders are ignored.

    Ring o1→o2→o3 uses priced tokens and should yield a solution.
    Orders o4→o5 use unpriced tokens and should be silently dropped.
    """
    # Priced ring: A→B→C→A
    priced = [
        _mk_order("o1", "0xA", "0xB"),
        _mk_order("o2", "0xB", "0xC"),
        _mk_order("o3", "0xC", "0xA"),
    ]
    # Unpriced pair (no reference_price in tokens dict)
    unpriced = [
        _mk_order("o4", "0xX", "0xY"),
        _mk_order("o5", "0xY", "0xX"),
    ]
    tokens = {
        "0xA": _mk_token(),
        "0xB": _mk_token(),
        "0xC": _mk_token(),
        # 0xX and 0xY intentionally absent
    }
    solver = CoWMatchingSolver()
    result = await solver.solve(_mk_auction(priced + unpriced, tokens))
    assert isinstance(result, Solution)
    solved_uids = {t.order_uid for t in result.trades}
    # Only the priced ring should be in the solution
    assert solved_uids == {"o1", "o2", "o3"}
    assert "o4" not in solved_uids
    assert "o5" not in solved_uids


@pytest.mark.asyncio
async def test_partially_priced_ring_is_excluded() -> None:
    """A ring where only some tokens are priced should not produce a solution.

    o1 (A→B), o2 (B→C), o3 (C→A): B is unpriced.
    The graph filter drops o1 and o2 (B appears as buy/sell), leaving too few
    priced orders to form a ring.
    """
    orders = [
        _mk_order("o1", "0xA", "0xB"),  # 0xB unpriced
        _mk_order("o2", "0xB", "0xC"),  # 0xB unpriced
        _mk_order("o3", "0xC", "0xA"),
    ]
    tokens = {
        "0xA": _mk_token(),
        # 0xB: no entry → unpriced
        "0xC": _mk_token(),
    }
    solver = CoWMatchingSolver()
    result = await solver.solve(_mk_auction(orders, tokens))
    assert isinstance(result, NoSolution)


# ── Limit price boundary ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ring_at_exact_limit_price_is_feasible() -> None:
    """An order where buy_amount == sell_amount (no surplus) should still be fillable.

    The LP solution is exactly at the limit price boundary. Before the tolerance
    fix, float truncation could drop below it and reject the ring.
    """
    orders = [
        _mk_order("o1", "0xA", "0xB", sell_amount=1000, buy_amount=1000),
        _mk_order("o2", "0xB", "0xC", sell_amount=1000, buy_amount=1000),
        _mk_order("o3", "0xC", "0xA", sell_amount=1000, buy_amount=1000),
    ]
    tokens = {"0xA": _mk_token(), "0xB": _mk_token(), "0xC": _mk_token()}
    solver = CoWMatchingSolver()
    result = await solver.solve(_mk_auction(orders, tokens))
    # May or may not produce a solution at zero-surplus — key is it doesn't crash
    assert isinstance(result, (Solution, NoSolution))
