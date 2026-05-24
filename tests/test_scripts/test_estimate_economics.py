"""Unit tests for the pure-math projection in scripts.estimate_economics."""

from __future__ import annotations

from scripts.estimate_economics import (
    _ARBITRUM_MIN_REWARD_ETH,
    _DEDUP_STRATEGIES,
    _cap_outliers,
    _dedupe_by_ring,
    _ring_signature,
    project_monthly,
)


def _project(**kw: float) -> dict[str, float]:
    """Helper: project with sensible defaults, return key fields as dict."""
    defaults = {
        "wins_by_strategy": {"router-v2": [int(1e14)] * 12},  # 12 wins, 0.0001 ETH each
        "window_days": 7,
        "eth_price_eur": 3000.0,
        "server_cost_eur": 60.0,
        "bonding_fee_pct": 15.0,
        "win_rate_confidence": 0.0,  # zero band → low/point/high collapse
    }
    defaults.update(kw)
    p = project_monthly(**defaults)  # type: ignore[arg-type]
    return {
        "wins_total": p.wins_total,
        "wins_per_month": p.wins_per_month_point,
        "net_point": p.net_eur_month_point,
        "net_low": p.net_eur_month_low,
        "net_high": p.net_eur_month_high,
        "reward_after_fee": p.reward_after_fee_eur,
        "surplus_eur": p.surplus_eur_month,
    }


def test_empty_wins_yields_negative_net_equal_to_server_cost() -> None:
    r = _project(wins_by_strategy={})
    assert r["wins_total"] == 0
    assert r["wins_per_month"] == 0
    assert r["net_point"] == -60.0


def test_wins_scale_linearly_to_monthly() -> None:
    """12 wins in 7 days ≈ 52 wins / month (7/30.44 stretch factor)."""
    r = _project()
    assert 50 < r["wins_per_month"] < 55


def test_reward_uses_arbitrum_floor_not_solver_surplus() -> None:
    """Performance reward = wins × 0.00024 ETH × eth_price × (1 - 15%)."""
    r = _project()
    # 12 wins / 7 days * 30.44 = 52.18 wins/mo
    # 52.18 × 0.00024 = 0.01252 ETH/mo × €3000 = €37.57 × 0.85 = €31.94
    expected_reward = 52.18 * _ARBITRUM_MIN_REWARD_ETH * 3000 * 0.85
    assert abs(r["reward_after_fee"] - expected_reward) < 0.5


def test_zero_confidence_band_collapses_low_point_high() -> None:
    r = _project()
    assert r["net_low"] == r["net_point"] == r["net_high"]


def test_confidence_band_expands_around_point() -> None:
    r = _project(win_rate_confidence=0.2)
    assert r["net_low"] < r["net_point"] < r["net_high"]
    # 20% confidence: high should equal point + 20% of (point - server_cost)
    # Both reward and surplus scale linearly; spread = 0.2 × revenue_at_point
    revenue_at_point = r["net_point"] + 60  # add back server cost = pure revenue
    spread = r["net_high"] - r["net_point"]
    assert abs(spread - revenue_at_point * 0.2) < 0.01


def test_multiple_strategies_aggregate_additively() -> None:
    r = _project(
        wins_by_strategy={
            "router-v2": [int(1e14)] * 6,
            "cow-matching-bipartite": [int(2e14)] * 6,
        }
    )
    assert r["wins_total"] == 12
    # Avg surplus across both: (6 × 1e14 + 6 × 2e14) / 12 = 1.5e14 wei = 0.00015 ETH
    # Surplus / month: 52.18 × 0.00015 × 3000 = €23.48
    assert 22.5 < r["surplus_eur"] < 24.5


def test_g6_pass_when_wins_high_enough() -> None:
    """At ~150 wins/week with 0.0003 ETH avg surplus, we should clear break-even."""
    r = _project(wins_by_strategy={"router-v2": [int(3e14)] * 150})
    assert r["net_point"] > 0


def test_g6_fail_at_current_realistic_baseline() -> None:
    """Document the actual situation: 12 bipartite wins/week at 0.0001 ETH avg → fail."""
    r = _project(wins_by_strategy={"cow-matching-bipartite": [int(1e14)] * 12})
    assert r["net_point"] < 0


# ── Median vs mean: outlier robustness ────────────────────────────────────────


def test_projection_uses_median_not_mean_so_one_outlier_does_not_dominate() -> None:
    """11 typical wins at 0.0001 ETH and 1 outlier at 0.5 ETH: median ≈ 0.0001,
    not the inflated mean of ~0.042.  The pre-fix code projected on the mean
    and overstated revenue by ~400×."""
    values = [int(1e14)] * 11 + [int(5e17)]
    r = _project(wins_by_strategy={"router-v2": values})
    # Median-driven surplus: ~0.0001 ETH × 12 wins / 7 days × 30.44 days/mo × €3000
    # = 0.0001 × 52.18 × 3000 = €15.65. Plus reward (~€32). Minus server (€60).
    # Net ≈ -€12 (clearly negative).  If mean were used: €4500+ net.
    assert -50 < r["net_point"] < 50, (
        f"net={r['net_point']:.2f} — median should anchor projection, not mean"
    )


def test_ring_signature_is_order_independent() -> None:
    """Permuting the trades must yield the same sig — ring identity is set-based."""
    sol_a = {"trades": [{"orderUid": "0xAA"}, {"orderUid": "0xBB"}, {"orderUid": "0xCC"}]}
    sol_b = {"trades": [{"orderUid": "0xCC"}, {"orderUid": "0xAA"}, {"orderUid": "0xBB"}]}
    assert _ring_signature(sol_a) == _ring_signature(sol_b)


def test_ring_signature_handles_malformed_solution() -> None:
    """A row with no trades or a non-dict solution must yield None, not crash."""
    assert _ring_signature(None) is None
    assert _ring_signature({}) is None
    assert _ring_signature({"trades": []}) is None
    assert _ring_signature({"trades": [{"no_uid": 1}]}) is None


def test_dedupe_by_ring_collapses_repeats_to_one_median_value() -> None:
    """The same TWAP ring matched 5 times → one win at the median surplus.

    This is the 1-distinct-ring × 430-matches situation observed in shadow.
    """
    rows = [
        (int(2.4e16), "ringA"),
        (int(2.4e16), "ringA"),
        (int(2.5e16), "ringA"),
        (int(2.5e16), "ringA"),
        (int(2.6e16), "ringA"),
        # different ring, single match — pass through
        (int(1.0e16), "ringB"),
        # ringless row — also pass through unchanged
        (int(5.0e15), None),
    ]
    out = _dedupe_by_ring(rows)
    # Expected: median(ringA group) = 2.5e16, ringB = 1.0e16, None-row = 5e15
    assert sorted(out) == sorted([int(5.0e15), int(1.0e16), int(2.5e16)])


def test_dedupe_with_realistic_scale_collapses_416_to_1() -> None:
    """430 matches of the persistent TWAP-ring → 1 representative win."""
    rows = [(int(2.4e16), "twap")] * 430
    assert _dedupe_by_ring(rows) == [int(2.4e16)]


def test_cap_outliers_clips_tail_to_percentile() -> None:
    """A single huge value gets clipped to p99 of the distribution."""
    typical = [int(1e14)] * 99
    outlier = [int(5e17)]
    capped = _cap_outliers(typical + outlier, percentile=99.0)
    assert max(capped) <= int(1e14) + 1  # cap close to the typical value
    assert len(capped) == 100  # no rows dropped


def test_cap_outliers_no_op_when_sample_too_small() -> None:
    """Need at least 10 values to define percentiles meaningfully."""
    small = [int(1e14), int(5e17)]
    assert _cap_outliers(small, percentile=99.0) == small  # unchanged


def test_cap_outliers_no_op_at_100_percentile() -> None:
    values = [int(1e14)] * 50 + [int(5e17)]
    assert _cap_outliers(values, percentile=100.0) == values


def test_dedup_strategies_includes_router_and_bipartite() -> None:
    """Regression: 2026-05-24 live data showed router-v2 hitting the same
    1-WBTC OTM order in 134/24h auctions (10-15 distinct UIDs, dozens of
    repeats each).  Bipartite has the same exposure when both sides of a
    pair are persistent (e.g. two TWAPs).  Both must be in _DEDUP_STRATEGIES."""
    assert "router-v2" in _DEDUP_STRATEGIES
    assert "cow-matching-bipartite" in _DEDUP_STRATEGIES
    assert "cow-matching-multi-party" in _DEDUP_STRATEGIES


def test_single_uid_signature_collapses_router_v2_repeats() -> None:
    """Router-v2 single-trade 'ring' = one UID. The same UID matched 50
    times in 24h must collapse to 1 representative.  Median = single value."""
    sig = _ring_signature({"trades": [{"orderUid": "0xdeadbeef" * 14}]})
    assert sig is not None
    rows = [(int(6.6e18), sig)] * 50  # 50× phantom-era router-v2 wins
    out = _dedupe_by_ring(rows)
    assert out == [int(6.6e18)], (
        f"50 repeats of same UID must collapse to 1 row; got {len(out)}"
    )


def test_bipartite_pair_signature_collapses_recurring_match() -> None:
    """Bipartite emits 2-trade solutions. A (uid_a, uid_b) pair appearing
    in N consecutive auctions (e.g. two long-lived TWAPs on opposite sides
    of one pair) must collapse to 1 representative across the window."""
    a = "0xaaaa" + "0" * 108
    b = "0xbbbb" + "0" * 108
    sig = _ring_signature({"trades": [
        {"orderUid": a}, {"orderUid": b}
    ]})
    other_sig = _ring_signature({"trades": [
        {"orderUid": a}, {"orderUid": "0xcccc" + "0" * 108}
    ]})
    rows = [
        # 25 hits on the (a,b) match
        *[(int(5e14), sig) for _ in range(25)],
        # 3 hits on a different (a,c) match
        *[(int(8e14), other_sig) for _ in range(3)],
    ]
    out = sorted(_dedupe_by_ring(rows))
    assert out == sorted([int(5e14), int(8e14)]), (
        f"distinct UID-sets must each contribute 1 row; got {out}"
    )
