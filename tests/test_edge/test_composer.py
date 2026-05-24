"""Tests for the CIP-67 composer (token-disjoint composition)."""
from edge.matching.composer import (
    CandidateSolution,
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


def test_compose_empty_returns_none():
    result = compose([], auction_id=42)
    assert result is None


def test_compose_single_candidate():
    sol = _mk_solution({"0xa": 100, "0xb": 200}, ["uid1"], [500])
    result = compose([CandidateSolution(strategy="naive", solution=sol)], auction_id=42)
    assert result is not None
    assert result.id == 42
    assert len(result.trades) == 1


def test_compose_two_fully_disjoint_candidates_merge():
    """Disjoint token sets compose freely — every token's price comes from
    a single solver."""
    sol_a = _mk_solution({"0xa": 100, "0xb": 200}, ["uid1"], [500])
    sol_b = _mk_solution({"0xc": 198, "0xd": 300}, ["uid2"], [300])
    cands = [
        CandidateSolution(strategy="bipartite", solution=sol_a),
        CandidateSolution(strategy="router", solution=sol_b),
    ]
    result = compose(cands, auction_id=42)
    assert result is not None
    assert len(result.trades) == 2
    assert result.prices == {"0xa": 100, "0xb": 200, "0xc": 198, "0xd": 300}


def test_compose_any_token_overlap_rejects_lower_ranked_candidate():
    """A candidate overlapping ANY claimed token is dropped wholesale —
    no per-token averaging.  Higher-surplus candidate wins.

    Before this rule the composer averaged prices on overlap, mixing
    multi-party ring-internal prices with router-v2 market prices and
    producing fantasy CIP-14 scores (~480 ETH) on live shadow data."""
    # sol_a higher surplus (1000), sol_b lower (100) — sol_a wins
    sol_a = _mk_solution({"0xa": 100, "0xb": 200}, ["uid1"], [1000])
    # sol_b shares 0xb with sol_a but otherwise has a fresh 0xc
    sol_b = _mk_solution({"0xb": 198, "0xc": 300}, ["uid2"], [100])
    cands = [
        CandidateSolution(strategy="bipartite", solution=sol_a),
        CandidateSolution(strategy="router", solution=sol_b),
    ]
    result = compose(cands, auction_id=42)
    assert result is not None
    # sol_b dropped entirely (overlap on 0xb) — even its non-overlapping
    # 0xc / uid2 trade does not leak in.
    assert len(result.trades) == 1
    assert result.trades[0].order_uid == "uid1"
    assert "0xc" not in result.prices


def test_compose_conflicting_candidates_drops_one():
    """Two solutions claiming all the same tokens — lower-rank one is dropped."""
    sol_a = _mk_solution({"0xa": 100, "0xb": 200}, ["uid1"], [1000])
    sol_b = _mk_solution({"0xa": 130, "0xb": 200}, ["uid2"], [100])
    cands = [
        CandidateSolution(strategy="bipartite", solution=sol_a),
        CandidateSolution(strategy="router", solution=sol_b),
    ]
    result = compose(cands, auction_id=42)
    assert result is not None
    assert len(result.trades) == 1
    assert result.trades[0].order_uid == "uid1"


def test_compose_dedupes_trades_by_uid():
    """Same uid in two candidates with disjoint price-tokens → trade kept once."""
    sol_a = _mk_solution({"0xa": 100}, ["uid_same"], [500])
    sol_b = _mk_solution({"0xb": 100}, ["uid_same"], [600])
    cands = [
        CandidateSolution(strategy="s1", solution=sol_a),
        CandidateSolution(strategy="s2", solution=sol_b),
    ]
    result = compose(cands, auction_id=42)
    assert result is not None
    assert len(result.trades) == 1


def test_compose_uses_explicit_surplus_for_ordering():
    """surplus_estimate explicitly given → takes precedence over the
    executed-amount proxy.  With disjoint tokens both compose."""
    sol_a = _mk_solution({"0xa": 100}, ["uid_a"], [100])
    sol_b = _mk_solution({"0xb": 200}, ["uid_b"], [10000])
    cands = [
        CandidateSolution(strategy="a", solution=sol_a, surplus_estimate=999_999),
        CandidateSolution(strategy="b", solution=sol_b, surplus_estimate=1),
    ]
    result = compose(cands, auction_id=42)
    assert result is not None
    assert len(result.trades) == 2


def test_compose_prices_come_from_exactly_one_solver_per_token():
    """Invariant: every token in result.prices appears in EXACTLY ONE
    candidate's prices.  Guards against re-introducing averaging."""
    sol_a = _mk_solution({"0xa": 100, "0xb": 200}, ["uid1"], [1000])
    sol_b = _mk_solution({"0xb": 1_000_000, "0xc": 300}, ["uid2"], [100])  # 0xb conflicts
    result = compose(
        [
            CandidateSolution(strategy="a", solution=sol_a),
            CandidateSolution(strategy="b", solution=sol_b),
        ],
        auction_id=42,
    )
    assert result is not None
    assert result.prices["0xb"] == 200  # NOT averaged toward 1_000_000
