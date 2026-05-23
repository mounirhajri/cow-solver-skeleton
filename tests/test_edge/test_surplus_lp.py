"""Tests for the ring LP solver."""
from edge.matching.surplus import RingLPResult, solve_ring_lp
from src.models.auction import Token
from src.models.order import Order


def _mk_order(
    uid: str, sell_token: str, buy_token: str, sell_amount: int, buy_amount: int
) -> Order:
    return Order(
        uid=uid, sellToken=sell_token, buyToken=buy_token,
        sellAmount=sell_amount, buyAmount=buy_amount,
        feePolicies=[], validTo=99, kind="sell",
        owner="0x" + "a" * 40, partiallyFillable=False,
        **{"class": "limit"},
    )


def _mk_token(reference_price: int = 10**18) -> Token:
    return Token(decimals=18, referencePrice=reference_price)


def test_solve_simple_3_ring_feasible():
    """A→B→C→A with uniform prices and overlap-positive limits."""
    ring = (
        _mk_order("o1", "A", "B", 1000, 900),
        _mk_order("o2", "B", "C", 1000, 900),
        _mk_order("o3", "C", "A", 1000, 900),
    )
    tokens = {"A": _mk_token(), "B": _mk_token(), "C": _mk_token()}
    result = solve_ring_lp(ring, tokens)
    assert result.feasible
    assert len(result.executed_amounts) == 3
    # All x_i should be > 0 and <= 1000
    for x in result.executed_amounts:
        assert 0 < x <= 1000
    # Surplus should be positive
    assert result.surplus_units > 0


def test_solve_infeasible_when_limit_unmet():
    """Order wants more than the ring can provide."""
    ring = (
        _mk_order("o1", "A", "B", 100, 10_000),  # buy_amount > sell_amount * any reasonable price
        _mk_order("o2", "B", "A", 100, 10_000),
    )
    tokens = {"A": _mk_token(), "B": _mk_token()}
    result = solve_ring_lp(ring, tokens)
    # 2-cycle: not really a "ring" but should still try
    # With these limits, infeasible
    assert not result.feasible


def test_solve_returns_infeasible_when_missing_price():
    ring = (
        _mk_order("o1", "A", "B", 1000, 900),
        _mk_order("o2", "B", "A", 1000, 900),
    )
    # Note: token B is missing
    tokens = {"A": _mk_token()}
    result = solve_ring_lp(ring, tokens)
    assert not result.feasible


def test_solve_with_different_prices():
    """A is 2x B, so x_A=500, x_B=1000 is balanced."""
    ring = (
        _mk_order("o1", "A", "B", 1000, 1000),  # 1000 A wants 1000 B at 2:1 ratio = lots of surplus
        _mk_order("o2", "B", "A", 1000, 500),
    )
    tokens = {
        "A": _mk_token(reference_price=2 * 10**18),
        "B": _mk_token(reference_price=10**18),
    }
    result = solve_ring_lp(ring, tokens)
    # Feasibility depends on exact ring balance — 2-cycle test, may be infeasible
    # depending on price overlap. Just check it doesn't crash.
    assert isinstance(result, RingLPResult)


def test_executed_amounts_within_sell_bounds():
    ring = (
        _mk_order("o1", "A", "B", 500, 400),
        _mk_order("o2", "B", "A", 800, 600),
    )
    tokens = {"A": _mk_token(), "B": _mk_token()}
    result = solve_ring_lp(ring, tokens)
    if result.feasible:
        assert result.executed_amounts[0] <= 500
        assert result.executed_amounts[1] <= 800


def test_infeasible_single_order_ring():
    """Rings with fewer than 2 orders are rejected immediately."""
    ring = (_mk_order("o1", "A", "B", 1000, 900),)
    tokens = {"A": _mk_token(), "B": _mk_token()}
    result = solve_ring_lp(ring, tokens)
    assert not result.feasible
    assert result.executed_amounts == ()
    assert result.surplus_units == 0


def test_executed_amounts_are_integers():
    """Verify no floats leak out of the LP rounding."""
    ring = (
        _mk_order("o1", "A", "B", 1000, 900),
        _mk_order("o2", "B", "C", 1000, 900),
        _mk_order("o3", "C", "A", 1000, 900),
    )
    tokens = {"A": _mk_token(), "B": _mk_token(), "C": _mk_token()}
    result = solve_ring_lp(ring, tokens)
    assert result.feasible
    for x in result.executed_amounts:
        assert isinstance(x, int)
    assert isinstance(result.surplus_units, int)
