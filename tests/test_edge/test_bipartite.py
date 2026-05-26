"""Tests for bipartite CoW matcher."""
import pytest

from edge.matching.bipartite import GHOST_OWNER_BLACKLIST, BipartiteMatcher
from src.models.auction import Auction
from src.models.order import Order
from src.models.solution import Solution
from src.shadow.scoring import _score_sell_trade
from src.solver.base import NoSolution
from tests.test_edge._helpers import mk_partial_order


def _mk_order(
    uid: str, sell_token: str, buy_token: str, sell_amount: int, buy_amount: int
) -> Order:
    return Order(
        uid=uid,
        sellToken=sell_token,
        buyToken=buy_token,
        sellAmount=sell_amount,
        buyAmount=buy_amount,
        feePolicies=[],
        validTo=999999,
        kind="sell",
        owner="0x" + "a" * 40,
        partiallyFillable=False,
        **{"class": "limit"},
    )


def _mk_auction(orders: list[Order]) -> Auction:
    return Auction(
        id="1",
        tokens={},
        orders=orders,
        liquidity=[],
        effectiveGasPrice=0,
        deadline=None,
    )


@pytest.mark.asyncio
async def test_empty_auction_returns_no_solution():
    m = BipartiteMatcher()
    result = await m.solve(_mk_auction([]))
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_single_order_returns_no_solution():
    m = BipartiteMatcher()
    auction = _mk_auction([_mk_order("o1", "0xa", "0xb", 1000, 800)])
    result = await m.solve(auction)
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_no_counterpart_returns_no_solution():
    """Two orders A→B but no B→A."""
    m = BipartiteMatcher()
    auction = _mk_auction([
        _mk_order("o1", "0xa", "0xb", 1000, 800),
        _mk_order("o2", "0xa", "0xb", 500, 400),
    ])
    result = await m.solve(auction)
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_simple_match_emits_two_trades():
    """A: 1000 X for >=800 Y. B: 1000 Y for >=800 X. Feasible (1e6 >= 640e3)."""
    m = BipartiteMatcher()
    auction = _mk_auction([
        _mk_order("oA", "0xa", "0xb", 1000, 800),
        _mk_order("oB", "0xb", "0xa", 1000, 800),
    ])
    result = await m.solve(auction)
    assert isinstance(result, Solution)
    assert len(result.trades) == 2
    uids = {t.order_uid for t in result.trades}
    assert uids == {"oA", "oB"}


@pytest.mark.asyncio
async def test_infeasible_limits_returns_no_solution():
    """A wants too much: A sells 100 for >=200. B sells 100 for >=200. 1e4 < 4e4."""
    m = BipartiteMatcher()
    auction = _mk_auction([
        _mk_order("oA", "0xa", "0xb", 100, 200),
        _mk_order("oB", "0xb", "0xa", 100, 200),
    ])
    result = await m.solve(auction)
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_greedy_matches_largest_first():
    """Two B→A orders, both should match their respective A→B counterpart."""
    m = BipartiteMatcher()
    auction = _mk_auction([
        _mk_order("oA1", "0xa", "0xb", 1000, 800),
        _mk_order("oA2", "0xa", "0xb", 100, 80),
        _mk_order("oB1", "0xb", "0xa", 1000, 800),
        _mk_order("oB2", "0xb", "0xa", 100, 80),
    ])
    result = await m.solve(auction)
    assert isinstance(result, Solution)
    # Should match 4 orders (greedy pairs largest-first then next)
    assert len(result.trades) == 4


@pytest.mark.asyncio
async def test_non_sell_orders_ignored():
    m = BipartiteMatcher()
    buy_order = Order(
        uid="o_buy", sellToken="0xa", buyToken="0xb",
        sellAmount=1000, buyAmount=800, feePolicies=[],
        validTo=999, kind="buy",
        owner="0x" + "a" * 40, partiallyFillable=False,
        **{"class": "limit"},
    )
    sell_order = _mk_order("o_sell", "0xb", "0xa", 1000, 800)
    auction = _mk_auction([buy_order, sell_order])
    result = await m.solve(auction)
    # Only sell orders are considered → no bipartite match
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_executed_amounts_within_limits():
    """Verify the executed amounts satisfy order limits."""
    m = BipartiteMatcher()
    # A: sells 1000 X, wants >= 800 Y
    # B: sells 900 Y, wants >= 700 X
    # Feasibility: 1000 * 900 = 900000 >= 800 * 700 = 560000 ✓
    auction = _mk_auction([
        _mk_order("oA", "0xa", "0xb", 1000, 800),
        _mk_order("oB", "0xb", "0xa", 900, 700),
    ])
    result = await m.solve(auction)
    assert isinstance(result, Solution)
    assert len(result.trades) == 2
    trade_a = next(t for t in result.trades if t.order_uid == "oA")
    trade_b = next(t for t in result.trades if t.order_uid == "oB")
    # A's executed sell must not exceed sell_amount
    assert trade_a.executed_amount <= 1000
    assert trade_a.executed_amount > 0
    # B's executed sell must not exceed sell_amount
    assert trade_b.executed_amount <= 900
    assert trade_b.executed_amount > 0


@pytest.mark.asyncio
async def test_prices_in_solution():
    """Clearing prices for both tokens should be in the solution."""
    m = BipartiteMatcher()
    auction = _mk_auction([
        _mk_order("oA", "0xa", "0xb", 1000, 800),
        _mk_order("oB", "0xb", "0xa", 1000, 800),
    ])
    result = await m.solve(auction)
    assert isinstance(result, Solution)
    assert "0xa" in result.prices
    assert "0xb" in result.prices
    assert result.prices["0xa"] > 0
    assert result.prices["0xb"] > 0


@pytest.mark.asyncio
async def test_rf_filter_invoked_when_classifier_passed(monkeypatch):
    """When classifier+model wired, the filter is called on sell_orders."""

    class _DummyClassifier:
        model = "loaded"

        def score(self, features: dict) -> float:  # noqa: ARG002
            return 1.0

    captured: dict[str, object] = {}

    async def fake_filter(orders, session_factory, classifier, threshold=0.4):
        captured["called"] = True
        captured["n_in"] = len(orders)
        captured["threshold"] = threshold
        return orders  # pass through unchanged

    # Patch on the importing module: bipartite imports the symbol at load time,
    # so patching rf_filter's namespace wouldn't affect bipartite's reference.
    monkeypatch.setattr(
        "edge.matching.bipartite.filter_orders_by_token_quality", fake_filter
    )

    m = BipartiteMatcher(
        classifier=_DummyClassifier(),
        session_factory=lambda: None,
        rf_threshold=0.4,
    )
    auction = _mk_auction([
        _mk_order("oA", "0xa", "0xb", 1000, 800),
        _mk_order("oB", "0xb", "0xa", 1000, 800),
    ])
    result = await m.solve(auction)
    assert captured.get("called") is True
    assert captured.get("n_in") == 2
    assert isinstance(result, Solution)


@pytest.mark.asyncio
async def test_rf_filter_drops_orders_with_low_score_tokens(monkeypatch):
    """Behavioural: orders touching a token below threshold are excluded from matching.

    Two would-be matches in the auction:
      - (oA, oB): both tokens score high → should match
      - (oC, oD): one token scores low   → should be filtered out
    """
    scores_by_token = {"0xa": 0.9, "0xb": 0.9, "0xgood": 0.9, "0xbad": 0.1}

    async def fake_fetch(_session_factory, addresses):
        # Inject the per-token score as a synthetic feature; FakeClassifier reads it.
        return {a.lower(): {"_test_score": scores_by_token.get(a.lower(), 0.5)} for a in addresses}

    monkeypatch.setattr("edge.matching.rf_filter._fetch_token_features", fake_fetch)

    class _FakeClassifier:
        model = "fake"  # truthy so the gate lets the filter run

        def score(self, features: dict) -> float:
            return features.get("_test_score", 0.5)

    m = BipartiteMatcher(
        classifier=_FakeClassifier(),
        session_factory=lambda: None,  # not called — _fetch is monkeypatched
        rf_threshold=0.4,
    )
    auction = _mk_auction([
        _mk_order("oA", "0xa", "0xb", 1000, 800),
        _mk_order("oB", "0xb", "0xa", 1000, 800),
        _mk_order("oC", "0xgood", "0xbad", 1000, 800),  # touches 0xbad → dropped
        _mk_order("oD", "0xbad", "0xgood", 1000, 800),  # touches 0xbad → dropped
    ])
    result = await m.solve(auction)
    assert isinstance(result, Solution)
    trade_uids = {t.order_uid for t in result.trades}
    assert "oA" in trade_uids and "oB" in trade_uids
    assert "oC" not in trade_uids and "oD" not in trade_uids


@pytest.mark.asyncio
async def test_exact_match_symmetric():
    """Exact match: A sells 1000 for 1000, B sells 1000 for 1000."""
    m = BipartiteMatcher()
    auction = _mk_auction([
        _mk_order("oA", "0xa", "0xb", 1000, 1000),
        _mk_order("oB", "0xb", "0xa", 1000, 1000),
    ])
    result = await m.solve(auction)
    assert isinstance(result, Solution)
    assert len(result.trades) == 2


# ---------------------------------------------------------------------------
# Phase 2: partial-fill downscale tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bipartite_partial_pair_matches_at_min_volume():
    """Both sides partial, volumes mismatched — should downscale to B's capacity.

    A: sells 1000 X, wants >=900 Y  (partial=True)
    B: sells  600 Y, wants >=500 X  (partial=True)

    Feasibility: 1000*600=600000 >= 900*500=450000  ✓
    Current (pre-Phase-2) logic after clamping b_gives to 600:
      b_gives(600) < a.buy_amount(900) → would reject.
    Phase-2 fix: A is partial, so downscale A's executed sell proportionally.
      executed_a_sell = b_gives * a.sell_amount // a.buy_amount
                      = 600 * 1000 // 900 = 666
      Check A's limit: A sells 666 X and receives 600 Y >= 900*(666/1000)=599.4  ✓
      Check B's limit: B sells 600 Y and receives 666 X >= 500  ✓
    """
    m = BipartiteMatcher()
    oA = mk_partial_order(
        "oA", "0xa", "0xb", sell_amount=1000, buy_amount=900, partially_fillable=True
    )
    oB = mk_partial_order(
        "oB", "0xb", "0xa", sell_amount=600, buy_amount=500, partially_fillable=True
    )
    auction = _mk_auction([oA, oB])
    result = await m.solve(auction)
    assert isinstance(result, Solution), "Expected a match but got NoSolution"
    assert len(result.trades) == 2

    trade_a = next(t for t in result.trades if t.order_uid == "oA")
    trade_b = next(t for t in result.trades if t.order_uid == "oB")

    # B sells its full capacity (600 Y), A sells <= 1000 X
    assert trade_b.executed_amount == 600, (
        f"B should sell full 600, got {trade_b.executed_amount}"
    )
    assert 0 < trade_a.executed_amount <= 1000, (
        f"A executed_sell out of range: {trade_a.executed_amount}"
    )

    # Both fills must satisfy limit prices
    # A: executed_a_sell / trade_b.executed_amount >= a.buy_amount / a.sell_amount
    #    i.e. A gets enough Y relative to what A sold
    assert trade_b.executed_amount * oA.sell_amount >= oA.buy_amount * trade_a.executed_amount, (
        "A's limit price violated"
    )
    # B: trade_a.executed_amount / trade_b.executed_amount >= b.buy_amount / b.sell_amount
    assert trade_a.executed_amount * oB.sell_amount >= oB.buy_amount * trade_b.executed_amount, (
        "B's limit price violated"
    )


@pytest.mark.asyncio
async def test_bipartite_skips_pair_when_both_non_partial_and_volumes_differ():
    """Same volume mismatch as above, but both partial=False → must be rejected.

    A: sells 1000 X, wants >=900 Y  (partial=False)
    B: sells  600 Y, wants >=500 X  (partial=False)

    B cannot fully meet A's minimum buy (900 Y), so no full fill is possible.
    Non-partial orders cannot be partially filled → NoSolution.
    """
    m = BipartiteMatcher()
    oA = mk_partial_order(
        "oA", "0xa", "0xb", sell_amount=1000, buy_amount=900, partially_fillable=False
    )
    oB = mk_partial_order(
        "oB", "0xb", "0xa", sell_amount=600, buy_amount=500, partially_fillable=False
    )
    auction = _mk_auction([oA, oB])
    result = await m.solve(auction)
    assert isinstance(result, NoSolution), (
        "Expected NoSolution for non-partial volume mismatch, got a match"
    )


# ---------------------------------------------------------------------------
# Ghost-owner blacklist filter tests (2026-05-26)
# ---------------------------------------------------------------------------

_GHOST_OWNER = next(iter(GHOST_OWNER_BLACKLIST))  # "0x58e41e53..."
_LEGIT_OWNER = "0x" + "b" * 40


def _mk_order_with_owner(
    uid: str,
    sell_token: str,
    buy_token: str,
    sell_amount: int,
    buy_amount: int,
    owner: str,
) -> Order:
    return Order(
        uid=uid,
        sellToken=sell_token,
        buyToken=buy_token,
        sellAmount=sell_amount,
        buyAmount=buy_amount,
        feePolicies=[],
        validTo=999999,
        kind="sell",
        owner=owner,
        partiallyFillable=False,
        **{"class": "limit"},
    )


@pytest.mark.asyncio
async def test_blacklisted_owner_orders_are_filtered(monkeypatch):
    """Orders signed by a blacklisted owner are dropped before bipartite matching.

    Auction has two sell/buy pairs that would both match bipartite-style:
      - Legit pair  (oL_sell, oL_buy):  owner = non-blacklisted  → should match
      - Ghost pair  (oG_sell, oG_buy):  owner = blacklisted       → must NOT match

    Also verifies the ghost-filter log fires with n_filtered == 2.
    """
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "edge.matching.bipartite.log.info",
        lambda event, **kw: calls.append((event, kw)),
    )

    oL_sell = _mk_order_with_owner("oL_sell", "0xa", "0xb", 1000, 800, _LEGIT_OWNER)
    oL_buy  = _mk_order_with_owner("oL_buy",  "0xb", "0xa", 1000, 800, _LEGIT_OWNER)
    oG_sell = _mk_order_with_owner("oG_sell", "0xa", "0xb", 1000, 800, _GHOST_OWNER)
    oG_buy  = _mk_order_with_owner("oG_buy",  "0xb", "0xa", 1000, 800, _GHOST_OWNER)

    m = BipartiteMatcher()
    result = await m.solve(_mk_auction([oL_sell, oL_buy, oG_sell, oG_buy]))

    assert isinstance(result, Solution), "Legit pair should still produce a solution"
    trade_uids = {t.order_uid for t in result.trades}
    assert "oL_sell" in trade_uids and "oL_buy" in trade_uids, "Legit pair must match"
    assert "oG_sell" not in trade_uids and "oG_buy" not in trade_uids, (
        "Ghost-owner pair must not appear in trades"
    )

    # Ghost-filter log must have fired with n_filtered == 2 and surface
    # the offending owner(s) so prod debugging doesn't require re-querying.
    filter_events = [kw for ev, kw in calls if ev == "bipartite_ghost_owner_filter"]
    assert filter_events, "bipartite_ghost_owner_filter log was not emitted"
    assert filter_events[0]["n_filtered"] == 2, (
        f"Expected n_filtered=2, got {filter_events[0]['n_filtered']}"
    )
    assert filter_events[0]["filtered_owners"] == [_GHOST_OWNER.lower()], (
        f"Expected filtered_owners=[{_GHOST_OWNER.lower()}], "
        f"got {filter_events[0]['filtered_owners']}"
    )


@pytest.mark.asyncio
async def test_non_blacklisted_owners_match_normally(monkeypatch):
    """Regression guard: no blacklisted owners → ghost filter does not fire, matches work.

    Same auction shape as the previous test but ALL orders owned by a legit address.
    Both pairs should match and the ghost-filter log must NOT be emitted.
    """
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "edge.matching.bipartite.log.info",
        lambda event, **kw: calls.append((event, kw)),
    )

    oA_sell = _mk_order_with_owner("oA_sell", "0xa", "0xb", 1000, 800, _LEGIT_OWNER)
    oA_buy  = _mk_order_with_owner("oA_buy",  "0xb", "0xa", 1000, 800, _LEGIT_OWNER)
    oB_sell = _mk_order_with_owner("oB_sell", "0xa", "0xb", 500,  400, _LEGIT_OWNER)
    oB_buy  = _mk_order_with_owner("oB_buy",  "0xb", "0xa", 500,  400, _LEGIT_OWNER)

    m = BipartiteMatcher()
    result = await m.solve(_mk_auction([oA_sell, oA_buy, oB_sell, oB_buy]))

    assert isinstance(result, Solution), "All-legit auction must produce a solution"
    assert len(result.trades) == 4, (
        f"Expected 4 trades (both pairs), got {len(result.trades)}"
    )

    # Ghost-filter log must NOT have been emitted (n_filtered == 0 → no log)
    filter_events = [ev for ev, _ in calls if ev == "bipartite_ghost_owner_filter"]
    assert not filter_events, (
        "bipartite_ghost_owner_filter must not fire when no blacklisted owners present"
    )


@pytest.mark.asyncio
async def test_all_orders_blacklisted_returns_no_solution(monkeypatch):
    """Auction full of ghost orders → filter drops everything → NoSolution.

    The most plausible production scenario for ghost-rich auctions: every
    sell-side order is signed by the blacklisted owner. After the filter
    runs, ``len(sell_orders) < 2`` and the matcher must short-circuit to
    NoSolution without touching ``_find_matches`` or the RF-filter.
    """
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "edge.matching.bipartite.log.info",
        lambda event, **kw: calls.append((event, kw)),
    )

    o1 = _mk_order_with_owner("oG1", "0xa", "0xb", 1000, 800, _GHOST_OWNER)
    o2 = _mk_order_with_owner("oG2", "0xb", "0xa", 1000, 800, _GHOST_OWNER)

    m = BipartiteMatcher()
    result = await m.solve(_mk_auction([o1, o2]))

    assert isinstance(result, NoSolution), (
        "All-ghost auction must short-circuit to NoSolution"
    )
    # Ghost-filter log fires (n_filtered=2), RF-filter log must NOT fire
    # — the early return happens before the RF-filter gate.
    events = [ev for ev, _ in calls]
    assert "bipartite_ghost_owner_filter" in events
    assert "bipartite_rf_filter" not in events, (
        "RF-filter must be skipped after ghost-filter empties the order list"
    )


@pytest.mark.asyncio
async def test_blacklist_matches_checksummed_order_owner(monkeypatch):
    """Filter is case-insensitive on the order side too.

    Orders fetched from the CoW API often have checksummed owner addresses
    (mixed-case). The filter must still match them against the lower-case
    blacklist entries.
    """
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "edge.matching.bipartite.log.info",
        lambda event, **kw: calls.append((event, kw)),
    )

    # Checksum-style: alternating case, semantically equal to _GHOST_OWNER
    checksummed = "0x" + "".join(
        c.upper() if i % 2 == 0 else c
        for i, c in enumerate(_GHOST_OWNER[2:])
    )
    assert checksummed != _GHOST_OWNER, "test setup: must differ in casing"
    assert checksummed.lower() == _GHOST_OWNER, "test setup: must match when lowered"

    oL_sell = _mk_order_with_owner("oL_sell", "0xa", "0xb", 1000, 800, _LEGIT_OWNER)
    oL_buy  = _mk_order_with_owner("oL_buy",  "0xb", "0xa", 1000, 800, _LEGIT_OWNER)
    oG_sell = _mk_order_with_owner("oG_sell", "0xa", "0xb", 1000, 800, checksummed)
    oG_buy  = _mk_order_with_owner("oG_buy",  "0xb", "0xa", 1000, 800, checksummed)

    m = BipartiteMatcher()
    result = await m.solve(_mk_auction([oL_sell, oL_buy, oG_sell, oG_buy]))

    assert isinstance(result, Solution)
    trade_uids = {t.order_uid for t in result.trades}
    assert "oG_sell" not in trade_uids and "oG_buy" not in trade_uids, (
        "Checksummed ghost-owner orders must still be filtered"
    )


def test_blacklist_is_lowercase_normalized():
    """All entries in GHOST_OWNER_BLACKLIST are lower-case at module load.

    Guards against a future contributor pasting a checksummed address into
    the source set — the comprehension at construction time must normalize
    it, otherwise the filter would silently no-op for that entry.
    """
    assert all(addr == addr.lower() for addr in GHOST_OWNER_BLACKLIST), (
        f"Non-lowercase entry found in GHOST_OWNER_BLACKLIST: "
        f"{[a for a in GHOST_OWNER_BLACKLIST if a != a.lower()]}"
    )


def test_bipartite_score_proportional_to_executed():
    """Surplus from _score_sell_trade scales linearly with executed amount.

    At half the executed volume, surplus should be half (±1 due to integer rounding).
    Uses a simple 1:1 clearing price so the arithmetic is exact.
    """
    # Order: sells 1000, wants >=800 (20% surplus margin)
    signed_sell = 1000
    signed_buy = 800
    # Clearing prices: 1:1 ratio (sell and buy token equally valued)
    cp_sell = 1000
    cp_buy = 1000
    native_price_buy = 10**18  # 1 ETH per buy-token (simplifies score = surplus)

    score_full = _score_sell_trade(
        executed=1000,
        signed_sell=signed_sell,
        signed_buy=signed_buy,
        cp_sell=cp_sell,
        cp_buy=cp_buy,
        native_price_buy=native_price_buy,
    )
    score_half = _score_sell_trade(
        executed=500,
        signed_sell=signed_sell,
        signed_buy=signed_buy,
        cp_sell=cp_sell,
        cp_buy=cp_buy,
        native_price_buy=native_price_buy,
    )

    assert score_full > 0, "Full fill should produce positive surplus"
    assert score_half > 0, "Half fill should produce positive surplus"
    # Linear scaling: half executed → half surplus (allow ±1 for integer rounding)
    assert abs(score_half * 2 - score_full) <= 1, (
        f"Surplus not proportional: full={score_full}, half={score_half}, 2*half={score_half * 2}"
    )


# ---------------------------------------------------------------------------
# DynamicGhostDetector integration tests (2026-05-26)
# ---------------------------------------------------------------------------


class _FakeGhostDetector:
    """In-memory GhostDetector stub for tests — no DB required."""

    def __init__(self, ghost_uids: set[str]) -> None:
        self._ghost_uids = ghost_uids

    async def is_ghost(self, order: Order) -> bool:
        return order.uid in self._ghost_uids


@pytest.mark.asyncio
async def test_dynamic_ghost_detector_filters_uids(monkeypatch):
    """UIDs flagged by the injected GhostDetector are dropped before matching.

    Mirrors the static-blacklist test but uses the dynamic-detector path.
    Two pairs in the auction: one with UIDs in the ghost set, one without.
    Only the non-ghost pair should appear in the result.
    """
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "edge.matching.bipartite.log.info",
        lambda event, **kw: calls.append((event, kw)),
    )

    oG_sell = _mk_order_with_owner("oG_sell", "0xa", "0xb", 1000, 800, _LEGIT_OWNER)
    oG_buy  = _mk_order_with_owner("oG_buy",  "0xb", "0xa", 1000, 800, _LEGIT_OWNER)
    oL_sell = _mk_order_with_owner("oL_sell", "0xa", "0xb", 500, 400, _LEGIT_OWNER)
    oL_buy  = _mk_order_with_owner("oL_buy",  "0xb", "0xa", 500, 400, _LEGIT_OWNER)

    detector = _FakeGhostDetector(ghost_uids={"oG_sell", "oG_buy"})
    m = BipartiteMatcher(ghost_detector=detector)
    result = await m.solve(_mk_auction([oG_sell, oG_buy, oL_sell, oL_buy]))

    assert isinstance(result, Solution), "Non-ghost pair should still match"
    trade_uids = {t.order_uid for t in result.trades}
    assert "oL_sell" in trade_uids and "oL_buy" in trade_uids
    assert "oG_sell" not in trade_uids and "oG_buy" not in trade_uids

    filter_events = [kw for ev, kw in calls if ev == "bipartite_dynamic_ghost_filter"]
    assert filter_events, "bipartite_dynamic_ghost_filter log was not emitted"
    assert filter_events[0]["n_filtered"] == 2


@pytest.mark.asyncio
async def test_dynamic_ghost_detector_none_is_noop(monkeypatch):
    """No detector injected → matcher behaves exactly like the pre-detector code.

    Regression guard for the public skeleton / tests that instantiate
    ``BipartiteMatcher()`` without arguments.
    """
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "edge.matching.bipartite.log.info",
        lambda event, **kw: calls.append((event, kw)),
    )

    o1 = _mk_order_with_owner("o1", "0xa", "0xb", 1000, 800, _LEGIT_OWNER)
    o2 = _mk_order_with_owner("o2", "0xb", "0xa", 1000, 800, _LEGIT_OWNER)

    m = BipartiteMatcher()  # no ghost_detector
    result = await m.solve(_mk_auction([o1, o2]))
    assert isinstance(result, Solution)

    filter_events = [ev for ev, _ in calls if ev == "bipartite_dynamic_ghost_filter"]
    assert not filter_events, (
        "Dynamic ghost-filter must not fire when no detector is provided"
    )


@pytest.mark.asyncio
async def test_dynamic_ghost_detector_all_filtered_returns_no_solution(monkeypatch):
    """Every UID flagged → < 2 orders remain → short-circuit to NoSolution.

    Verifies the matcher returns before reaching the static blacklist + RF-filter.
    """
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "edge.matching.bipartite.log.info",
        lambda event, **kw: calls.append((event, kw)),
    )

    o1 = _mk_order_with_owner("o1", "0xa", "0xb", 1000, 800, _LEGIT_OWNER)
    o2 = _mk_order_with_owner("o2", "0xb", "0xa", 1000, 800, _LEGIT_OWNER)

    detector = _FakeGhostDetector(ghost_uids={"o1", "o2"})
    m = BipartiteMatcher(ghost_detector=detector)
    result = await m.solve(_mk_auction([o1, o2]))

    assert isinstance(result, NoSolution)
    events = [ev for ev, _ in calls]
    assert "bipartite_dynamic_ghost_filter" in events
    assert "bipartite_rf_filter" not in events, (
        "RF-filter must not run when ghost-filter short-circuited"
    )
