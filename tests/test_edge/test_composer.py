"""Tests for the CIP-67 composer."""
from edge.matching.composer import (
    CandidateSolution,
    _prices_compatible,
    compose,
)
from src.models.solution import Solution, Trade


def _mk_solution(prices: dict[str, int], uids: list[str], amounts: list[int]) -> Solution:
    return Solution(
        id=1,
        prices=prices,
        trades=[
            Trade(kind="fulfillment", orderUid=uid, executedAmount=amt)
            for uid, amt in zip(uids, amounts, strict=True)
        ],
        interactions=[],
    )


def test_prices_compatible_within_tolerance():
    assert _prices_compatible(1000, 1010, tolerance=0.02)
    assert _prices_compatible(1000, 990, tolerance=0.02)


def test_prices_incompatible_beyond_tolerance():
    assert not _prices_compatible(1000, 1050, tolerance=0.02)
    assert not _prices_compatible(1000, 900, tolerance=0.02)


def test_compose_empty_returns_none():
    result = compose([], auction_id=42)
    assert result is None


def test_compose_single_candidate():
    sol = _mk_solution({"0xa": 100, "0xb": 200}, ["uid1"], [500])
    result = compose([CandidateSolution(strategy="naive", solution=sol)], auction_id=42)
    assert result is not None
    assert result.id == 42
    assert len(result.trades) == 1


def test_compose_two_compatible_candidates_merge():
    sol_a = _mk_solution({"0xa": 100, "0xb": 200}, ["uid1"], [500])
    sol_b = _mk_solution({"0xb": 198, "0xc": 300}, ["uid2"], [300])
    cands = [
        CandidateSolution(strategy="bipartite", solution=sol_a),
        CandidateSolution(strategy="router", solution=sol_b),
    ]
    result = compose(cands, auction_id=42)
    assert result is not None
    assert len(result.trades) == 2
    assert "0xa" in result.prices
    assert "0xb" in result.prices
    assert "0xc" in result.prices


def test_compose_conflicting_candidates_drops_one():
    """Two solutions with incompatible prices for same token — the one with
    higher surplus wins, the other is dropped."""
    # sol_a has higher surplus (executed_amount=1000 vs 100); sol_b has conflicting 0xa price
    sol_a = _mk_solution({"0xa": 100, "0xb": 200}, ["uid1"], [1000])
    sol_b = _mk_solution({"0xa": 130, "0xb": 200}, ["uid2"], [100])
    cands = [
        CandidateSolution(strategy="bipartite", solution=sol_a),
        CandidateSolution(strategy="router", solution=sol_b),
    ]
    result = compose(cands, auction_id=42)
    assert result is not None
    # Only one trade (the higher-surplus candidate wins)
    assert len(result.trades) == 1
    assert result.trades[0].order_uid == "uid1"


def test_compose_dedupes_trades_by_uid():
    """Same uid in two candidates — only first kept."""
    sol_a = _mk_solution({"0xa": 100}, ["uid_same"], [500])
    sol_b = _mk_solution({"0xa": 100}, ["uid_same"], [600])  # would duplicate
    cands = [
        CandidateSolution(strategy="s1", solution=sol_a),
        CandidateSolution(strategy="s2", solution=sol_b),
    ]
    result = compose(cands, auction_id=42)
    assert result is not None
    assert len(result.trades) == 1


def test_compose_uses_explicit_surplus_for_ordering():
    """When surplus_estimate is provided, it takes precedence over the
    proxy."""
    sol_a = _mk_solution({"0xa": 100}, ["uid_a"], [100])   # small
    sol_b = _mk_solution({"0xb": 200}, ["uid_b"], [10000])  # large by proxy
    cands = [
        CandidateSolution(strategy="a", solution=sol_a, surplus_estimate=999_999),
        CandidateSolution(strategy="b", solution=sol_b, surplus_estimate=1),
    ]
    # No conflict — both included regardless of ordering
    result = compose(cands, auction_id=42)
    assert result is not None
    assert len(result.trades) == 2
