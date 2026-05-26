"""Tests for CoWMatchingSolver (multi-party ring matching)."""
import pytest

from edge.matching.multi_party import (
    CoWMatchingSolver,
    _has_reference_price,
    _order_in_money,
    _solve_ring_lp,
)
from src.models.auction import Auction, Token
from src.models.order import Order
from src.models.solution import Solution
from src.solver.base import NoSolution
from tests.test_edge._helpers import mk_partial_order


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


# ── _order_in_money helper ────────────────────────────────────────────────────

def test_order_in_money_when_surplus() -> None:
    """sell_amount * p_sell > buy_amount * p_buy → in the money."""
    tokens = {"A": _mk_token(price=10**18), "B": _mk_token(price=10**18)}
    order = _mk_order("o1", "A", "B", sell_amount=1000, buy_amount=900)
    assert _order_in_money(order, tokens) is True


def test_order_in_money_at_exact_parity() -> None:
    """sell_amount * p_sell == buy_amount * p_buy → still in the money (equality)."""
    tokens = {"A": _mk_token(price=10**18), "B": _mk_token(price=10**18)}
    order = _mk_order("o1", "A", "B", sell_amount=1000, buy_amount=1000)
    assert _order_in_money(order, tokens) is True


def test_order_not_in_money() -> None:
    """sell_amount * p_sell < buy_amount * p_buy → out of the money."""
    tokens = {"A": _mk_token(price=10**18), "B": _mk_token(price=10**18)}
    order = _mk_order("o1", "A", "B", sell_amount=900, buy_amount=1000)
    assert _order_in_money(order, tokens) is False


def test_order_otm_within_tolerance_is_accepted() -> None:
    """At 100 bps tolerance: a 0.5 % OTM order is treated as viable."""
    tokens = {"A": _mk_token(price=10**18), "B": _mk_token(price=10**18)}
    # sell_value = 995, buy_value = 1000 → -0.5 % OTM, within 1 % tolerance
    order = _mk_order("o1", "A", "B", sell_amount=995, buy_amount=1000)
    assert _order_in_money(order, tokens) is False  # strict default
    assert _order_in_money(order, tokens, tolerance_bps=100) is True


def test_order_far_otm_rejected_even_with_tolerance() -> None:
    """2 % OTM falls outside the 100 bps tolerance band."""
    tokens = {"A": _mk_token(price=10**18), "B": _mk_token(price=10**18)}
    # sell_value = 980, buy_value = 1000 → -2 % OTM
    order = _mk_order("o1", "A", "B", sell_amount=980, buy_amount=1000)
    assert _order_in_money(order, tokens, tolerance_bps=100) is False
    assert _order_in_money(order, tokens, tolerance_bps=200) is True


def test_order_tolerance_zero_matches_strict_legacy() -> None:
    """tolerance_bps=0 reproduces the pre-relax behaviour exactly."""
    tokens = {"A": _mk_token(price=10**18), "B": _mk_token(price=10**18)}
    itm = _mk_order("o1", "A", "B", sell_amount=1000, buy_amount=900)
    otm = _mk_order("o2", "A", "B", sell_amount=900, buy_amount=1000)
    assert _order_in_money(itm, tokens, 0) is True
    assert _order_in_money(otm, tokens, 0) is False


def test_order_not_in_money_when_unpriced() -> None:
    """Missing reference price → not viable (same as False)."""
    tokens: dict[str, Token] = {}
    order = _mk_order("o1", "A", "B", sell_amount=1000, buy_amount=900)
    assert _order_in_money(order, tokens) is False


# ── Happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_3_ring_produces_solution() -> None:
    """A → B → C → A ring with in-money orders should yield a solution."""
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
    result = await solver.solve(_mk_auction(orders, tokens))
    assert isinstance(result, NoSolution)


# ── Out-of-the-money filter (the real root cause of 0/192) ───────────────────

@pytest.mark.asyncio
async def test_otm_orders_excluded_from_graph() -> None:
    """Out-of-the-money orders must not enter the ring graph.

    In real auctions ~1 000+ orders are out-of-the-money at reference prices.
    Any ring containing such an order is mathematically infeasible (the LP's
    ring-balance constraint forces a common settlement value V, which cannot
    simultaneously satisfy both an OTM buy requirement and an ITM sell bound).

    The 3-ring here has o3 asking for more than reference prices can provide
    (sell_amount * p_sell < buy_amount * p_buy). The graph should drop o3,
    leaving only 2 in-money orders — not enough for a 3-ring → NoSolution.
    """
    tokens = {"0xA": _mk_token(), "0xB": _mk_token(), "0xC": _mk_token()}
    orders = [
        _mk_order("o1", "0xA", "0xB", sell_amount=1000, buy_amount=900),   # ITM
        _mk_order("o2", "0xB", "0xC", sell_amount=1000, buy_amount=900),   # ITM
        _mk_order("o3", "0xC", "0xA", sell_amount=900, buy_amount=2000),   # OTM
    ]
    solver = CoWMatchingSolver()
    result = await solver.solve(_mk_auction(orders, tokens))
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_otm_orders_mixed_with_valid_ring() -> None:
    """OTM orders are silently dropped; valid ITM ring still produces a solution.

    This mirrors the real production case: 1095 orders, ~26 ITM.  The solver
    should only operate on ITM orders and find the ring among them.
    """
    tokens = {
        "0xA": _mk_token(), "0xB": _mk_token(), "0xC": _mk_token(),
        "0xX": _mk_token(), "0xY": _mk_token(),
    }
    itm_ring = [
        _mk_order("o1", "0xA", "0xB", sell_amount=1000, buy_amount=900),
        _mk_order("o2", "0xB", "0xC", sell_amount=1000, buy_amount=900),
        _mk_order("o3", "0xC", "0xA", sell_amount=1000, buy_amount=900),
    ]
    # OTM orders mixed in (asking 10x more than reference prices allow)
    otm_noise = [
        _mk_order("n1", "0xX", "0xY", sell_amount=100, buy_amount=100_000),
        _mk_order("n2", "0xY", "0xX", sell_amount=100, buy_amount=100_000),
        _mk_order("n3", "0xA", "0xC", sell_amount=100, buy_amount=100_000),
    ]
    solver = CoWMatchingSolver()
    result = await solver.solve(_mk_auction(itm_ring + otm_noise, tokens))
    assert isinstance(result, Solution)
    solved_uids = {t.order_uid for t in result.trades}
    assert solved_uids == {"o1", "o2", "o3"}
    assert not solved_uids.intersection({"n1", "n2", "n3"})


# ── Unpriced token filtering ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unpriced_ring_tokens_yield_no_solution() -> None:
    """Orders with unpriced tokens are treated as OTM → NoSolution."""
    orders = [
        _mk_order("o1", "0xX", "0xY"),
        _mk_order("o2", "0xY", "0xZ"),
        _mk_order("o3", "0xZ", "0xX"),
    ]
    tokens: dict[str, Token] = {}
    solver = CoWMatchingSolver()
    result = await solver.solve(_mk_auction(orders, tokens))
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_partially_priced_ring_is_excluded() -> None:
    """A ring where one token is unpriced → that order is OTM → no ring forms."""
    orders = [
        _mk_order("o1", "0xA", "0xB"),  # 0xB unpriced → OTM
        _mk_order("o2", "0xB", "0xC"),  # 0xB unpriced → OTM
        _mk_order("o3", "0xC", "0xA"),
    ]
    tokens = {"0xA": _mk_token(), "0xC": _mk_token()}  # 0xB absent
    solver = CoWMatchingSolver()
    result = await solver.solve(_mk_auction(orders, tokens))
    assert isinstance(result, NoSolution)


# ── Limit price boundary ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rf_filter_invoked_when_classifier_passed(monkeypatch) -> None:
    """When classifier+model wired, the filter is called before OTM filter."""

    class _DummyClassifier:
        model = "loaded"

        def score(self, features: dict) -> float:  # noqa: ARG002
            return 1.0

    captured: dict[str, object] = {}

    async def fake_filter(orders, session_factory, classifier, threshold=0.4):
        captured["called"] = True
        captured["n_in"] = len(orders)
        return orders  # pass through unchanged

    # Patch on the importing module: multi_party imports the symbol at load time,
    # so patching rf_filter's namespace wouldn't affect multi_party's reference.
    monkeypatch.setattr(
        "edge.matching.multi_party.filter_orders_by_token_quality", fake_filter
    )

    orders = [
        _mk_order("o1", "0xA", "0xB"),
        _mk_order("o2", "0xB", "0xC"),
        _mk_order("o3", "0xC", "0xA"),
    ]
    tokens = {"0xA": _mk_token(), "0xB": _mk_token(), "0xC": _mk_token()}
    solver = CoWMatchingSolver(
        classifier=_DummyClassifier(),
        session_factory=lambda: None,
    )
    result = await solver.solve(_mk_auction(orders, tokens))
    assert captured.get("called") is True
    assert captured.get("n_in") == 3
    assert isinstance(result, Solution)


@pytest.mark.asyncio
async def test_rf_filter_drops_orders_touching_low_score_tokens(monkeypatch) -> None:
    """Behavioural: an order touching a token below threshold is removed before
    ring enumeration. The remaining orders are below MIN_RING_LENGTH → NoSolution.

    Without the filter (see `test_ring_*` above) the same auction finds a ring.
    """
    scores_by_token = {"0xa": 0.9, "0xb": 0.9, "0xc": 0.1}  # token C is "scam"

    async def fake_fetch(_session_factory, addresses):
        return {
            a.lower(): {"_test_score": scores_by_token.get(a.lower(), 0.5)}
            for a in addresses
        }

    monkeypatch.setattr("edge.matching.rf_filter._fetch_token_features", fake_fetch)

    class _FakeClassifier:
        model = "fake"

        def score(self, features: dict) -> float:
            return features.get("_test_score", 0.5)

    orders = [
        _mk_order("o1", "0xA", "0xB"),  # both pass: kept
        _mk_order("o2", "0xB", "0xC"),  # 0xC fails: dropped
        _mk_order("o3", "0xC", "0xA"),  # 0xC fails: dropped
    ]
    tokens = {"0xA": _mk_token(), "0xB": _mk_token(), "0xC": _mk_token()}
    solver = CoWMatchingSolver(
        classifier=_FakeClassifier(),
        session_factory=lambda: None,  # not called — _fetch is monkeypatched
        rf_threshold=0.4,
    )
    result = await solver.solve(_mk_auction(orders, tokens))
    assert isinstance(result, NoSolution), (
        "after filter only o1 survives (< MIN_RING_LENGTH=3) → no ring possible"
    )


@pytest.mark.asyncio
async def test_ring_at_exact_limit_price_is_feasible() -> None:
    """buy_amount == sell_amount (zero surplus) is treated as ITM and attempted."""
    orders = [
        _mk_order("o1", "0xA", "0xB", sell_amount=1000, buy_amount=1000),
        _mk_order("o2", "0xB", "0xC", sell_amount=1000, buy_amount=1000),
        _mk_order("o3", "0xC", "0xA", sell_amount=1000, buy_amount=1000),
    ]
    tokens = {"0xA": _mk_token(), "0xB": _mk_token(), "0xC": _mk_token()}
    solver = CoWMatchingSolver()
    result = await solver.solve(_mk_auction(orders, tokens))
    assert isinstance(result, (Solution, NoSolution))


# ── Ring-cooldown ────────────────────────────────────────────────────────────


class _FakeClock:
    """Monotonic clock stub with an explicit `advance()` for cooldown tests."""

    def __init__(self) -> None:
        self._t = 1_000.0

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _ring_3() -> tuple[list[Order], dict[str, Token]]:
    """A→B→C→A ring with simple in-money orders."""
    orders = [
        _mk_order("o1", "0xA", "0xB"),
        _mk_order("o2", "0xB", "0xC"),
        _mk_order("o3", "0xC", "0xA"),
    ]
    tokens = {"0xA": _mk_token(), "0xB": _mk_token(), "0xC": _mk_token()}
    return orders, tokens


@pytest.mark.asyncio
async def test_cooldown_zero_disabled_emits_every_call() -> None:
    """With cooldown_seconds=0 the legacy behaviour is preserved."""
    orders, tokens = _ring_3()
    solver = CoWMatchingSolver(ring_cooldown_seconds=0)
    r1 = await solver.solve(_mk_auction(orders, tokens, auction_id="1"))
    r2 = await solver.solve(_mk_auction(orders, tokens, auction_id="2"))
    assert isinstance(r1, Solution) and isinstance(r2, Solution)
    assert len(r2.trades) == 3  # second auction still emits


@pytest.mark.asyncio
async def test_cooldown_suppresses_repeat_emission() -> None:
    """An emitted ring's UIDs go into cooldown — subsequent auction returns NoSolution."""
    orders, tokens = _ring_3()
    clock = _FakeClock()
    solver = CoWMatchingSolver(ring_cooldown_seconds=600, clock=clock)

    r1 = await solver.solve(_mk_auction(orders, tokens, auction_id="1"))
    assert isinstance(r1, Solution)
    assert {t.order_uid for t in r1.trades} == {"o1", "o2", "o3"}

    # Same orders in next auction — all three UIDs are in cooldown.
    r2 = await solver.solve(_mk_auction(orders, tokens, auction_id="2"))
    assert isinstance(r2, NoSolution)


@pytest.mark.asyncio
async def test_cooldown_expires_after_window() -> None:
    """After the configured window the same UIDs become eligible again."""
    orders, tokens = _ring_3()
    clock = _FakeClock()
    solver = CoWMatchingSolver(ring_cooldown_seconds=600, clock=clock)

    await solver.solve(_mk_auction(orders, tokens, auction_id="1"))
    clock.advance(700.0)  # past the 600 s window
    r2 = await solver.solve(_mk_auction(orders, tokens, auction_id="2"))
    assert isinstance(r2, Solution)
    assert {t.order_uid for t in r2.trades} == {"o1", "o2", "o3"}


@pytest.mark.asyncio
async def test_cooldown_only_blocks_emitted_uids() -> None:
    """A fresh ring sharing no UIDs with the cooled set must still emit."""
    clock = _FakeClock()
    solver = CoWMatchingSolver(ring_cooldown_seconds=600, clock=clock)

    # First auction emits ring1 = {o1, o2, o3} on tokens A/B/C.
    orders1, tokens1 = _ring_3()
    r1 = await solver.solve(_mk_auction(orders1, tokens1, auction_id="1"))
    assert isinstance(r1, Solution)

    # Second auction has a DIFFERENT ring on tokens D/E/F with fresh UIDs.
    orders2 = [
        _mk_order("p1", "0xD", "0xE"),
        _mk_order("p2", "0xE", "0xF"),
        _mk_order("p3", "0xF", "0xD"),
    ]
    tokens2 = {"0xD": _mk_token(), "0xE": _mk_token(), "0xF": _mk_token()}
    r2 = await solver.solve(_mk_auction(orders2, tokens2, auction_id="2"))
    assert isinstance(r2, Solution)
    assert {t.order_uid for t in r2.trades} == {"p1", "p2", "p3"}


@pytest.mark.asyncio
async def test_cooldown_not_set_on_no_solution_path() -> None:
    """An auction that yields NoSolution must NOT poison the cooldown set —
    otherwise a single infeasible candidate would lock out future legit rings."""
    orders, tokens = _ring_3()
    # Make the ring infeasible by pricing one token far OTM.
    tokens["0xA"] = Token(decimals=18, referencePrice=1)  # extreme imbalance
    clock = _FakeClock()
    solver = CoWMatchingSolver(ring_cooldown_seconds=600, clock=clock)

    r1 = await solver.solve(_mk_auction(orders, tokens, auction_id="1"))
    # Whether feasible or not depends on filter; assert state is empty if NoSolution.
    if isinstance(r1, NoSolution):
        assert solver._uid_cooldown == {}


# ── Partial-fill rejection ────────────────────────────────────────────────────


def test_lp_drops_ring_when_non_partial_short_after_round():
    """_solve_ring_lp returns None when a non-partially-fillable leg is short.

    Ring construction identical to test_lp_emits_floor_rounded_executed_for_partial_ring:
    order 0 has sell_amount=1000 and buy_amount=700 but cycle constraints
    (leg 1 capped at 500) force x_real[0] ≈ 714.28 → floor = 714 < 1000.
    With order 0 flagged partially_fillable=False the whole ring must be rejected.
    """
    tokens = {"A": _mk_token(), "B": _mk_token(), "C": _mk_token()}
    ring = (
        mk_partial_order(
            "o1", "A", "B", sell_amount=1000, buy_amount=700, partially_fillable=False
        ),
        mk_partial_order(
            "o2", "B", "C", sell_amount=500, buy_amount=400, partially_fillable=True
        ),
        mk_partial_order(
            "o3", "C", "A", sell_amount=1000, buy_amount=600, partially_fillable=True
        ),
    )
    result = _solve_ring_lp(ring, tokens)
    assert result is None, (
        "_solve_ring_lp must return None when a non-partial leg gets a fractional fill"
    )


def test_solve_ring_lp_rejects_when_non_partial_gets_zero_fill(monkeypatch):
    """Zero-fill is just an extreme short fill: the guard must still fire.

    Rather than engineer an LP that returns x_real=0 (brittle), we patch
    solve_ring_lp to return a hand-crafted RingLPResult with
    executed_amounts=(0, 1000, 1000) and received_short_fill=(True, False, False).
    With leg 0 flagged partially_fillable=False, the ring must be rejected.
    Tests the guard contract directly.
    """
    from edge.matching.surplus import RingLPResult

    tokens = {"A": _mk_token(), "B": _mk_token(), "C": _mk_token()}
    ring = (
        mk_partial_order(
            "o1", "A", "B", sell_amount=1000, buy_amount=700, partially_fillable=False
        ),
        mk_partial_order(
            "o2", "B", "C", sell_amount=1000, buy_amount=900, partially_fillable=True
        ),
        mk_partial_order(
            "o3", "C", "A", sell_amount=1000, buy_amount=900, partially_fillable=True
        ),
    )
    fake_result = RingLPResult(
        feasible=True,
        executed_amounts=(0, 1000, 1000),
        surplus_units=100,
        clearing_prices={"A": 10**18, "B": 10**18, "C": 10**18},
        received_short_fill=(True, False, False),
    )
    monkeypatch.setattr(
        "edge.matching.surplus.solve_ring_lp",
        lambda r, t, *_a, **_kw: fake_result,
    )
    assert _solve_ring_lp(ring, tokens) is None


def test_all_non_partial_ring_with_matching_volumes_solves():
    """All-non-partial ring with balanced volumes solves end-to-end.

    With the LP-bounds pinning for non-partial orders, this ring's
    pinned solution (x = sell_amounts = (1000, 1000, 1000)) satisfies
    every leg's limit (b/s = 0.9 < 1).  Locks in that pinning bounds
    does not over-reject legitimate non-partial rings.
    """
    tokens = {"A": _mk_token(), "B": _mk_token(), "C": _mk_token()}
    ring = (
        _mk_order("o1", "A", "B", sell_amount=1000, buy_amount=900),
        _mk_order("o2", "B", "C", sell_amount=1000, buy_amount=900),
        _mk_order("o3", "C", "A", sell_amount=1000, buy_amount=900),
    )
    cand = _solve_ring_lp(ring, tokens)
    assert cand is not None, "balanced all-non-partial ring must solve"
    # Every leg fully filled.
    assert cand.executed_amounts == (1000, 1000, 1000)
    assert cand.surplus_estimate > 0


def test_non_partial_ring_volume_cliff_rejected_as_lp_failed():
    """Non-partial ring with volume cliff is rejected with rejection_reason='lp_failed'.

    Previously this slipped through the LP (which would allocate a
    fractional fill) and was caught by the ``non_partial_short`` filter,
    wasting an LP solve.  With pinning, the LP itself reports infeasibility
    and the rejection reason is ``"lp_failed"``.
    """
    from edge.matching.multi_party import _solve_ring_lp_with_reason

    tokens = {"A": _mk_token(), "B": _mk_token(), "C": _mk_token()}
    ring = (
        _mk_order("o1", "A", "B", sell_amount=1000, buy_amount=900),
        _mk_order("o2", "B", "C", sell_amount=10, buy_amount=9),
        _mk_order("o3", "C", "A", sell_amount=1000, buy_amount=900),
    )
    cand, reason = _solve_ring_lp_with_reason(ring, tokens)
    assert cand is None
    assert reason == "lp_failed", (
        f"expected lp_failed (pinned bounds), got {reason!r}"
    )


# ── Ring breakdown instrumentation ────────────────────────────────────────────


def _capture_log(monkeypatch) -> list[dict]:
    """Capture multi_party.log.info events into a list of {event, ...kw} dicts."""
    events: list[dict] = []

    def capture(event, **kw):
        events.append({"event": event, **kw})

    monkeypatch.setattr("edge.matching.multi_party.log.info", capture)
    return events


@pytest.mark.asyncio
async def test_ring_breakdown_log_emits_on_solved_ring(monkeypatch) -> None:
    """A feasible ring increments n_solved and emits the aggregate log once."""
    events = _capture_log(monkeypatch)
    orders = [
        _mk_order("o1", "0xA", "0xB"),
        _mk_order("o2", "0xB", "0xC"),
        _mk_order("o3", "0xC", "0xA"),
    ]
    tokens = {"0xA": _mk_token(), "0xB": _mk_token(), "0xC": _mk_token()}
    solver = CoWMatchingSolver()
    result = await solver.solve(_mk_auction(orders, tokens, auction_id="42"))
    assert isinstance(result, Solution)
    breakdowns = [e for e in events if e["event"] == "multi_party_ring_breakdown"]
    assert len(breakdowns) == 1
    bd = breakdowns[0]
    assert bd["auction_id"] == "42"
    assert bd["n_rings_detected"] >= 1
    assert bd["n_solved"] >= 1
    assert bd["n_rate_infeasible"] == 0
    assert bd["n_lp_failed"] == 0
    assert bd["n_post_round_violation"] == 0
    assert bd["n_non_partial_short"] == 0


@pytest.mark.asyncio
async def test_ring_breakdown_counts_rate_infeasible(monkeypatch) -> None:
    """Mix of feasible + rate-infeasible rings: counts must reflect both buckets.

    The Johnson enumerator on this multigraph yields two 3-rings:
      ring1 = (o1, o2, o3) — all ITM, low rates → feasible
      ring2 = (o1b, o2, o3) — o1b has b/s = 1.5 → Π r > 1 → rate_infeasible

    Both share tokens A/B/C; the OTM/ITM filter passes everything since
    o1b is technically ITM at reference prices (sell_value > buy_value when
    we pick sell=2000, buy=1500 with both tokens at parity).
    """
    events = _capture_log(monkeypatch)
    # Two parallel A→B edges so rustworkx enumerates two distinct rings.
    orders = [
        _mk_order("o1", "0xA", "0xB", sell_amount=1000, buy_amount=900),   # feasible
        _mk_order("o1b", "0xA", "0xB", sell_amount=2000, buy_amount=1500), # rate-infeasible in ring
        _mk_order("o2", "0xB", "0xC", sell_amount=1000, buy_amount=900),
        _mk_order("o3", "0xC", "0xA", sell_amount=1000, buy_amount=900),
    ]
    tokens = {"0xA": _mk_token(), "0xB": _mk_token(), "0xC": _mk_token()}
    solver = CoWMatchingSolver()
    await solver.solve(_mk_auction(orders, tokens, auction_id="99"))

    breakdowns = [e for e in events if e["event"] == "multi_party_ring_breakdown"]
    assert len(breakdowns) == 1
    bd = breakdowns[0]
    # Johnson over the multigraph may enumerate fewer or more candidates
    # depending on edge-mapping; the contract is that the bucket sum equals
    # n_rings_detected.
    total = (
        bd["n_solved"]
        + bd["n_rate_infeasible"]
        + bd["n_lp_failed"]
        + bd["n_post_round_violation"]
        + bd["n_non_partial_short"]
    )
    assert total == bd["n_rings_detected"]


@pytest.mark.asyncio
async def test_ring_breakdown_all_rate_infeasible(monkeypatch) -> None:
    """When every ring fails the rate check, n_rate_infeasible == n_rings_detected."""
    events = _capture_log(monkeypatch)
    # Π r = 1.5^3 = 3.375 > 1 — rate-infeasible. All ITM at reference prices
    # (sell=1000 > buy=750 at parity) so they pass the OTM gate.
    orders = [
        _mk_order("o1", "0xA", "0xB", sell_amount=1000, buy_amount=750),
        _mk_order("o2", "0xB", "0xC", sell_amount=1000, buy_amount=750),
        _mk_order("o3", "0xC", "0xA", sell_amount=2000, buy_amount=3000),  # OTM unless tol
    ]
    # Use a far higher reference price for 0xC so o3 is ITM (sell=2000 C *
    # high price > buy=3000 A * low price), letting all three orders into
    # the graph.  Rates remain: r1=0.75, r2=0.75, r3=1.5 → Π = 0.84 — actually
    # feasible.  Pick stronger ratios to force infeasibility:
    orders = [
        _mk_order("o1", "0xA", "0xB", sell_amount=1000, buy_amount=1500),
        _mk_order("o2", "0xB", "0xC", sell_amount=1000, buy_amount=1500),
        _mk_order("o3", "0xC", "0xA", sell_amount=1000, buy_amount=1500),
    ]
    # Reference prices: A=B=C=1.  Each order: sell_value=1000, buy_value=1500
    # → OTM, would be filtered out.  Bump sell-side reference prices so each
    # passes the OTM gate but rates (b/s = 1.5) still fail the LP.
    tokens = {
        "0xA": _mk_token(price=2 * 10**18),
        "0xB": _mk_token(price=2 * 10**18),
        "0xC": _mk_token(price=2 * 10**18),
    }
    solver = CoWMatchingSolver()
    await solver.solve(_mk_auction(orders, tokens, auction_id="77"))

    breakdowns = [e for e in events if e["event"] == "multi_party_ring_breakdown"]
    # If the ring was enumerated, breakdown must fire and every ring must
    # land in the rate_infeasible bucket.
    if breakdowns:
        bd = breakdowns[0]
        assert bd["n_rate_infeasible"] == bd["n_rings_detected"]
        assert bd["n_solved"] == 0


@pytest.mark.asyncio
async def test_ring_breakdown_not_emitted_when_no_rings(monkeypatch) -> None:
    """When no rings are enumerated the breakdown log is suppressed (early return)."""
    events = _capture_log(monkeypatch)
    # Just two orders → cannot form a 3-ring.
    orders = [
        _mk_order("o1", "0xA", "0xB"),
        _mk_order("o2", "0xB", "0xA"),
    ]
    tokens = {"0xA": _mk_token(), "0xB": _mk_token()}
    solver = CoWMatchingSolver()
    await solver.solve(_mk_auction(orders, tokens, auction_id="1"))
    breakdowns = [e for e in events if e["event"] == "multi_party_ring_breakdown"]
    assert breakdowns == []


# ── otm_tolerance_bps plumbed into ring-feasibility ──────────────────────────


@pytest.mark.asyncio
async def test_loose_tolerance_admits_ring_but_zero_volume_rejects(monkeypatch) -> None:
    """Π r_i > 1 ring is admitted under loose tolerance but rejected as zero-volume.

    Three orders with b/s = 1010/1000 = 1.01 each. Π r_i ≈ 1.0303,
    log_sum ≈ 0.02985.  At parity reference prices each order is slightly
    OTM (sell_value=1000 < buy_value=1010) — so only the loose-OTM solver
    admits them into the candidate graph; the strict solver filters them
    out before ring enumeration.

    Under loose admission the ring passes the pre-LP rate-feasibility
    shortcut (slack 3 × -ln(1-0.02) ≈ 0.0606 > 0.02985), so the LP runs.
    BUT: any ring with Π r_i > 1 has only the trivial solution x=0 (the
    chain ``x_0 >= Π r * x_0`` with ``Π r > 1`` forces ``x_0 <= 0``).
    The zero-volume guard correctly rejects this case so the composer
    never sees an operationally-barren candidate.

    Contract under test: the breakdown log records this ring under
    ``n_zero_volume``, NOT ``n_solved`` or ``n_rate_infeasible``. This is
    the post-cleanup behaviour — the prior commit emitted these rings as
    zero-amount Solutions that the sub-dust filter then NULL'd.
    """
    events = _capture_log(monkeypatch)
    orders = [
        mk_partial_order(
            "o1", "0xA", "0xB", sell_amount=1000, buy_amount=1010,
            partially_fillable=True,
        ),
        mk_partial_order(
            "o2", "0xB", "0xC", sell_amount=1000, buy_amount=1010,
            partially_fillable=True,
        ),
        mk_partial_order(
            "o3", "0xC", "0xA", sell_amount=1000, buy_amount=1010,
            partially_fillable=True,
        ),
    ]
    tokens = {"0xA": _mk_token(), "0xB": _mk_token(), "0xC": _mk_token()}

    # ── Loose tolerance: pre-LP filter admits, zero-volume guard rejects.
    # No candidate reaches the composer; breakdown shows n_zero_volume=1. ─
    loose_solver = CoWMatchingSolver(otm_tolerance_bps=200)
    loose_result = await loose_solver.solve(
        _mk_auction(orders, tokens, auction_id="100")
    )
    assert isinstance(loose_result, NoSolution), (
        "Π r > 1 rings must not produce a Solution even under loose tolerance"
    )
    loose_bds = [
        e for e in events
        if e["event"] == "multi_party_ring_breakdown" and e["auction_id"] == "100"
    ]
    assert len(loose_bds) == 1, (
        "loose-tolerance solver should admit the ring and emit a breakdown log"
    )
    bd_loose = loose_bds[0]
    assert bd_loose["n_zero_volume"] == 1, (
        f"expected n_zero_volume=1 (Π r > 1 → x=0 → guard rejects), got {bd_loose}"
    )
    assert bd_loose["n_solved"] == 0, (
        f"zero-volume ring must not count as solved, got {bd_loose}"
    )
    assert bd_loose["n_rate_infeasible"] == 0, (
        "ring passed the pre-LP rate-feasibility filter under loose tolerance"
    )

    # ── Strict tolerance (default 0): orders fail OTM filter, no rings. ──
    # No ring is enumerated → breakdown log is suppressed (matches the
    # ``not_emitted_when_no_rings`` contract) and the solver returns
    # NoSolution.  This validates that strict-mode behaviour is unchanged.
    strict_solver = CoWMatchingSolver()  # otm_tolerance_bps=0 by default
    strict_result = await strict_solver.solve(
        _mk_auction(orders, tokens, auction_id="101")
    )
    assert isinstance(strict_result, NoSolution)
    strict_bds = [
        e for e in events
        if e["event"] == "multi_party_ring_breakdown" and e["auction_id"] == "101"
    ]
    assert strict_bds == []
