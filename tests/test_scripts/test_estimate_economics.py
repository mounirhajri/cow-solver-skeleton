"""Unit tests for the pure-math projection in scripts.estimate_economics."""

from __future__ import annotations

from scripts.estimate_economics import _ARBITRUM_MIN_REWARD_ETH, project_monthly


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
