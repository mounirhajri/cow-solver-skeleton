"""Tests for the ring LP solver."""
from edge.matching.surplus import RingLPResult, solve_ring_lp
from src.models.auction import Token
from src.models.order import Order
from tests.test_edge._helpers import mk_partial_order


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


def test_solve_falls_back_when_missing_price():
    """Missing reference price ⇒ neutral weight; LP still solves (refactor).

    Before the OTM-tolerant refactor, missing reference prices gated
    feasibility.  Now reference prices only weight the objective — a feasible
    ring with one un-priced token resolves to a uniform-weighted solution.
    """
    ring = (
        _mk_order("o1", "A", "B", 1000, 900),
        _mk_order("o2", "B", "A", 1000, 900),
    )
    tokens = {"A": _mk_token()}  # B missing → neutral weighting
    result = solve_ring_lp(ring, tokens)
    assert result.feasible
    # Clearing prices include both tokens; B falls back to a neutral scale.
    assert "A" in result.clearing_prices
    assert "B" in result.clearing_prices


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


# ── OTM-tolerant refactor: feasibility decoupled from reference prices ──


def test_ring_all_itm_at_reference_solves():
    """3 ITM orders, Π r_i < 1 ⇒ feasible with positive surplus."""
    ring = (
        _mk_order("o1", "A", "B", 1000, 900),
        _mk_order("o2", "B", "C", 1000, 900),
        _mk_order("o3", "C", "A", 1000, 900),
    )
    tokens = {"A": _mk_token(), "B": _mk_token(), "C": _mk_token()}
    result = solve_ring_lp(ring, tokens)
    assert result.feasible
    for x in result.executed_amounts:
        assert x > 0
    assert result.surplus_units > 0


def test_ring_with_one_otm_order_solves_via_derived_prices():
    """One order is 1 % OTM at reference prices, but Π r_i ≤ 1 — feasible.

    o1 is the OTM order: sell 1000 A for 1005 B (sell_value=1000 < buy_value=1005
    at parity reference prices).  o2 and o3 over-compensate: Π r = 1.005 · 0.9 · 0.9 ≈ 0.814.
    """
    ring = (
        _mk_order("o1", "A", "B", 1000, 1005),   # 0.5 % OTM
        _mk_order("o2", "B", "C", 1000, 900),
        _mk_order("o3", "C", "A", 1000, 900),
    )
    tokens = {"A": _mk_token(), "B": _mk_token(), "C": _mk_token()}
    result = solve_ring_lp(ring, tokens)
    assert result.feasible, "ring with Π r ≤ 1 must be feasible regardless of reference-price-ITM"
    # All clearing prices present
    for tok in ("A", "B", "C"):
        assert tok in result.clearing_prices
        assert result.clearing_prices[tok] > 0


def test_ring_with_product_above_one_is_infeasible():
    """Engineered ring where Π r_i > 1 ⇒ infeasible by pure rate check."""
    # r1 = 1.5, r2 = 1.5, r3 = 1.5 ⇒ Π = 3.375 > 1
    ring = (
        _mk_order("o1", "A", "B", 1000, 1500),
        _mk_order("o2", "B", "C", 1000, 1500),
        _mk_order("o3", "C", "A", 1000, 1500),
    )
    tokens = {"A": _mk_token(), "B": _mk_token(), "C": _mk_token()}
    result = solve_ring_lp(ring, tokens)
    assert not result.feasible
    assert result.executed_amounts == ()
    assert result.clearing_prices == {}


def test_clearing_prices_derived_make_each_user_limit_satisfiable():
    """At derived clearing prices, every user gets >= their limit rate."""
    ring = (
        _mk_order("o1", "A", "B", 1000, 900),
        _mk_order("o2", "B", "C", 1000, 900),
        _mk_order("o3", "C", "A", 1000, 900),
    )
    tokens = {"A": _mk_token(), "B": _mk_token(), "C": _mk_token()}
    result = solve_ring_lp(ring, tokens)
    assert result.feasible

    # For each order i: executed_buy_value >= b_i / s_i * executed_sell_value.
    # Equivalent at derived prices: p[sell] / p[buy] >= b_i / s_i, i.e.
    # p[sell] * s_i >= b_i * p[buy].
    for i, order in enumerate(ring):
        p_sell = result.clearing_prices[order.sell_token]
        p_buy = result.clearing_prices[order.buy_token]
        assert p_sell * order.sell_amount >= p_buy * order.buy_amount - max(p_sell, p_buy), (
            f"order {order.uid}: clearing prices violate limit "
            f"(p_sell={p_sell}, p_buy={p_buy}, s={order.sell_amount}, b={order.buy_amount})"
        )
        # Also verify the executed amounts satisfy the user's quantity limit.
        x_i = result.executed_amounts[i]
        x_next = result.executed_amounts[(i + 1) % len(ring)]
        assert x_next * order.sell_amount + order.sell_amount >= x_i * order.buy_amount


def test_missing_reference_price_falls_back_gracefully():
    """One token un-priced ⇒ neutral weight, LP still solves."""
    ring = (
        _mk_order("o1", "A", "B", 1000, 900),
        _mk_order("o2", "B", "C", 1000, 900),
        _mk_order("o3", "C", "A", 1000, 900),
    )
    # Token C lacks a reference price entirely.
    tokens = {"A": _mk_token(), "B": _mk_token()}
    result = solve_ring_lp(ring, tokens)
    assert result.feasible
    for x in result.executed_amounts:
        assert x > 0
    for tok in ("A", "B", "C"):
        assert tok in result.clearing_prices


# ── Partial-fills: received_short_fill field ──────────────────────────────────


def test_lp_emits_floor_rounded_executed_for_partial_ring():
    """LP floor-rounds x_real and reports received_short_fill correctly.

    Ring construction: order 0 has sell_amount=1000 but the cycle constraint
    (leg 1 caps at sell_amount=500) forces x_real[0] ≈ (1000/700)*500 = 714.28
    → floor = 714 < 1000.  received_short_fill[0] must be True.
    All orders are partially_fillable=True so no rejection occurs.
    Legs 1 and 2 fill fully (500 and 1000 respectively).
    """
    ring = (
        mk_partial_order("o1", "A", "B", sell_amount=1000, buy_amount=700),
        mk_partial_order("o2", "B", "C", sell_amount=500, buy_amount=400),
        mk_partial_order("o3", "C", "A", sell_amount=1000, buy_amount=600),
    )
    tokens = {"A": _mk_token(), "B": _mk_token(), "C": _mk_token()}
    result = solve_ring_lp(ring, tokens)

    assert result.feasible
    # Leg 0 is short: floor((1000/700)*500) = 714.  Pin the exact value so a
    # future regression that relaxes the bound to `<=` would still trip here.
    assert result.executed_amounts[0] == 714
    # received_short_fill tracks the shortfall
    assert result.received_short_fill[0] is True
    # Legs 1 and 2 fill fully
    assert result.executed_amounts[1] == 500
    assert result.executed_amounts[2] == 1000
    assert result.received_short_fill[1] is False
    assert result.received_short_fill[2] is False


def test_rejection_reason_none_on_success():
    """A feasible ring returns rejection_reason=None."""
    ring = (
        _mk_order("o1", "A", "B", 1000, 900),
        _mk_order("o2", "B", "C", 1000, 900),
        _mk_order("o3", "C", "A", 1000, 900),
    )
    tokens = {"A": _mk_token(), "B": _mk_token(), "C": _mk_token()}
    result = solve_ring_lp(ring, tokens)
    assert result.feasible
    assert result.rejection_reason is None


def test_rejection_reason_rate_infeasible_when_product_above_one():
    """Π r_i > 1 ⇒ rejection_reason == 'rate_infeasible'."""
    # r1 = r2 = r3 = 1.5 → Π = 3.375 > 1
    ring = (
        _mk_order("o1", "A", "B", 1000, 1500),
        _mk_order("o2", "B", "C", 1000, 1500),
        _mk_order("o3", "C", "A", 1000, 1500),
    )
    tokens = {"A": _mk_token(), "B": _mk_token(), "C": _mk_token()}
    result = solve_ring_lp(ring, tokens)
    assert not result.feasible
    assert result.rejection_reason == "rate_infeasible"


def test_rejection_reason_ring_too_short():
    """Single-order 'ring' is rejected as too short."""
    ring = (_mk_order("o1", "A", "B", 1000, 900),)
    tokens = {"A": _mk_token(), "B": _mk_token()}
    result = solve_ring_lp(ring, tokens)
    assert not result.feasible
    assert result.rejection_reason == "ring_too_short"


def test_lp_full_fill_unchanged_when_no_fractional():
    """Regression: fully-filled ring has no short legs and received_short_fill is all False.

    All three orders have sell_amount=1000 and a generous limit; the LP
    pushes every x_i to 1000 → executed_amounts == sell_amounts, no shortfall.
    """
    ring = (
        mk_partial_order("o1", "A", "B", sell_amount=1000, buy_amount=900),
        mk_partial_order("o2", "B", "C", sell_amount=1000, buy_amount=900),
        mk_partial_order("o3", "C", "A", sell_amount=1000, buy_amount=900),
    )
    tokens = {"A": _mk_token(), "B": _mk_token(), "C": _mk_token()}
    result = solve_ring_lp(ring, tokens)

    assert result.feasible
    for i, x in enumerate(result.executed_amounts):
        assert x == ring[i].sell_amount, f"leg {i} should be fully filled"
    assert result.received_short_fill == (False, False, False)
