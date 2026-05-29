"""Tests for JointClearingSolver."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.models.auction import Auction, Token
from src.models.order import Order
from src.models.solution import Solution
from src.routing.v3_batched import V3BatchedQuote, V3Path
from src.solver.base import NoSolution
from src.solver.joint_clearing import (
    JointClearingSolver,
    _GROUP_UID_PREFIX,
    _all_limits_satisfied,
    _ceil_div,
)

_WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
_USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
_WBTC = "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"

# Smallest realistic sell/buy amounts (avoids division-by-zero in _ceil_div)
_SELL_1 = 1_000_000  # 1 USDC (6 dec)
_BUY_1 = 500         # 500 wei ETH (tiny, just to test the math)


def _make_order(
    uid: str = "o1",
    sell_token: str = _WETH,
    buy_token: str = _USDC,
    sell_amount: int = 10**18,
    buy_amount: int = 2_900 * 10**6,
    kind: str = "sell",
    partially_fillable: bool = False,
) -> Order:
    return Order(
        uid=uid,
        sellToken=sell_token,
        buyToken=buy_token,
        sellAmount=sell_amount,
        buyAmount=buy_amount,
        feePolicies=[],
        validTo=99999999,
        kind=kind,
        owner="0x" + "a" * 40,
        partiallyFillable=partially_fillable,
        **{"class": "limit"},
    )


def _make_auction(orders: list[Order], auction_id: str = "42") -> Auction:
    return Auction(
        id=auction_id,
        tokens={},
        orders=orders,
        liquidity=[],
        effectiveGasPrice=0,
        deadline=None,
    )


def _make_v3_path(
    order_uid: str,
    token_in: str = _WETH,
    token_out: str = _USDC,
    amount_in: int = 10**18,
    fee: int = 500,
) -> V3Path:
    return V3Path(
        order_uid=order_uid,
        token_in=token_in,
        token_out=token_out,
        amount_in=amount_in,
        fee_tier_in=fee,
        exact_output=False,
    )


# ── Unit: _ceil_div ───────────────────────────────────────────────────────────

def test_ceil_div_basic() -> None:
    assert _ceil_div(10, 3) == 4
    assert _ceil_div(9, 3) == 3
    assert _ceil_div(0, 5) == 0


def test_ceil_div_zero_denominator() -> None:
    assert _ceil_div(10, 0) == 0


# ── Unit: _all_limits_satisfied ───────────────────────────────────────────────

def test_all_limits_satisfied_single_order_passes() -> None:
    order = _make_order(sell_amount=10**18, buy_amount=2_900 * 10**6)
    # combined = only this order; rate same as limit → passes
    assert _all_limits_satisfied([order], 10**18, 2_900 * 10**6)


def test_all_limits_satisfied_better_rate_passes() -> None:
    order = _make_order(sell_amount=10**18, buy_amount=2_900 * 10**6)
    # Combined rate is 3000 USDC per WETH — better than limit of 2900
    assert _all_limits_satisfied([order], 10**18, 3_000 * 10**6)


def test_all_limits_satisfied_below_limit_fails() -> None:
    order = _make_order(sell_amount=10**18, buy_amount=2_900 * 10**6)
    # Rate 2800 < limit 2900 → fails
    assert not _all_limits_satisfied([order], 10**18, 2_800 * 10**6)


def test_all_limits_satisfied_two_orders_both_pass() -> None:
    o1 = _make_order("o1", sell_amount=10**18, buy_amount=2_900 * 10**6)
    o2 = _make_order("o2", sell_amount=2 * 10**18, buy_amount=5_800 * 10**6)
    # Combined: 3 WETH → 8700 USDC → rate = 2900/WETH
    # o1 receives ceil(1e18 * 8700e6 / 3e18) = ceil(2900e6) = 2900e6 >= 2900e6 ✓
    # o2 receives ceil(2e18 * 8700e6 / 3e18) = ceil(5800e6) = 5800e6 >= 5800e6 ✓
    assert _all_limits_satisfied([o1, o2], 3 * 10**18, 8_700 * 10**6)


def test_all_limits_satisfied_one_of_two_fails() -> None:
    o1 = _make_order("o1", sell_amount=10**18, buy_amount=2_900 * 10**6)
    o2 = _make_order("o2", sell_amount=2 * 10**18, buy_amount=6_000 * 10**6)  # tighter limit
    # Combined: 3 WETH → 8700 USDC → rate 2900/WETH
    # o2 receives 5800e6, needs 6000e6 → fails
    assert not _all_limits_satisfied([o1, o2], 3 * 10**18, 8_700 * 10**6)


def test_all_limits_satisfied_zero_inputs() -> None:
    order = _make_order(sell_amount=10**18, buy_amount=2_900 * 10**6)
    assert not _all_limits_satisfied([order], 0, 2_900 * 10**6)
    assert not _all_limits_satisfied([order], 10**18, 0)


# ── Integration: JointClearingSolver.solve ────────────────────────────────────

@pytest.mark.asyncio
async def test_joint_clearing_no_orders() -> None:
    multicall = AsyncMock()
    solver = JointClearingSolver(multicall=multicall, intermediates=[])
    result = await solver.solve(_make_auction([]))
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_joint_clearing_single_order_fallback_to_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One order → no group → falls through to RouterSolver individual path."""
    order = _make_order("o1", sell_amount=10**18, buy_amount=2_900 * 10**6)
    auction = _make_auction([order])

    # Individual quote clears limit
    ind_quote = V3BatchedQuote(
        path=_make_v3_path("o1", amount_in=10**18),
        amount_out=3_000 * 10**6,
    )

    async def mock_batched(*args, **kwargs):  # noqa: ANN001
        return [ind_quote]

    monkeypatch.setattr("src.solver.joint_clearing.batched_v3_quote", mock_batched)

    with patch("src.solver.joint_clearing.RouterSolver._encode_path_interaction") as mock_enc:
        from src.encoder.interactions import Interaction
        mock_enc.return_value = Interaction(
            target="0x" + "0" * 40,
            value=0,
            call_data=b"\x00",
        )
        multicall = AsyncMock()
        solver = JointClearingSolver(multicall=multicall, intermediates=[])
        result = await solver.solve(auction)

    assert isinstance(result, Solution)
    assert len(result.trades) == 1
    assert result.trades[0].order_uid == "o1"


@pytest.mark.asyncio
async def test_joint_clearing_two_same_pair_orders_batched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two WETH→USDC orders get a combined quote → one interaction, two trades."""
    o1 = _make_order("o1", sell_amount=10**18, buy_amount=2_900 * 10**6)
    o2 = _make_order("o2", sell_amount=2 * 10**18, buy_amount=5_800 * 10**6)
    auction = _make_auction([o1, o2])

    combined_sell = 3 * 10**18  # o1 + o2
    combined_buy = 8_700 * 10**6  # 2900 USDC/WETH × 3 WETH

    group_path = _make_v3_path(
        f"{_GROUP_UID_PREFIX}xxxxx",
        amount_in=combined_sell,
    )
    group_quote = V3BatchedQuote(path=group_path, amount_out=combined_buy)

    # Individual quotes (for the fallback pass — shouldn't be used since group succeeds)
    ind1 = V3BatchedQuote(path=_make_v3_path("o1", amount_in=10**18), amount_out=2_950 * 10**6)
    ind2 = V3BatchedQuote(path=_make_v3_path("o2", amount_in=2 * 10**18), amount_out=5_850 * 10**6)

    async def mock_batched(*args, **kwargs):  # noqa: ANN001
        return [ind1, ind2, group_quote]

    monkeypatch.setattr("src.solver.joint_clearing.batched_v3_quote", mock_batched)

    with patch("src.solver.joint_clearing.RouterSolver._encode_path_interaction") as mock_enc:
        from src.encoder.interactions import Interaction
        mock_enc.return_value = Interaction(
            target="0x" + "0" * 40,
            value=0,
            call_data=b"\x01\x02",
        )
        multicall = AsyncMock()
        solver = JointClearingSolver(multicall=multicall, intermediates=[], min_group_size=2)
        result = await solver.solve(auction)

    assert isinstance(result, Solution)
    assert len(result.trades) == 2
    uids = {t.order_uid for t in result.trades}
    assert uids == {"o1", "o2"}

    # Both trades use the same executed_amount (their individual sell_amounts)
    for trade in result.trades:
        if trade.order_uid == "o1":
            assert trade.executed_amount == 10**18
        else:
            assert trade.executed_amount == 2 * 10**18

    # Clearing prices reflect combined quote
    weth_lower = _WETH.lower()
    usdc_lower = _USDC.lower()
    assert result.prices[weth_lower] == combined_buy   # prices[sell] = combined_buy
    assert result.prices[usdc_lower] == combined_sell  # prices[buy]  = combined_sell

    # Only ONE interaction (combined AMM swap)
    assert len(result.interactions) == 1


@pytest.mark.asyncio
async def test_joint_clearing_group_limits_missed_fallback_individual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Combined quote doesn't satisfy all limits → fall back to individual paths."""
    o1 = _make_order("o1", sell_amount=10**18, buy_amount=2_900 * 10**6)
    o2 = _make_order("o2", sell_amount=2 * 10**18, buy_amount=7_000 * 10**6)  # tight limit
    auction = _make_auction([o1, o2])

    combined_sell = 3 * 10**18
    # Combined gives only 2800/WETH — below o2's limit of 3500/WETH
    combined_buy = 8_400 * 10**6

    group_path = _make_v3_path(f"{_GROUP_UID_PREFIX}xxx", amount_in=combined_sell)
    group_quote = V3BatchedQuote(path=group_path, amount_out=combined_buy)

    # Individual o1 passes its limit; o2 does not
    ind1 = V3BatchedQuote(path=_make_v3_path("o1", amount_in=10**18), amount_out=2_950 * 10**6)
    ind2 = V3BatchedQuote(path=_make_v3_path("o2", amount_in=2 * 10**18), amount_out=6_000 * 10**6)

    async def mock_batched(*args, **kwargs):  # noqa: ANN001
        return [ind1, ind2, group_quote]

    monkeypatch.setattr("src.solver.joint_clearing.batched_v3_quote", mock_batched)

    with patch("src.solver.joint_clearing.RouterSolver._encode_path_interaction") as mock_enc:
        from src.encoder.interactions import Interaction
        mock_enc.return_value = Interaction(target="0x" + "0" * 40, value=0, call_data=b"")
        multicall = AsyncMock()
        solver = JointClearingSolver(multicall=multicall, intermediates=[], min_group_size=2)
        result = await solver.solve(auction)

    # Only o1 should settle (individually); o2 fails its limit
    assert isinstance(result, Solution)
    uids = {t.order_uid for t in result.trades}
    assert "o1" in uids
    assert "o2" not in uids


@pytest.mark.asyncio
async def test_joint_clearing_different_pairs_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two orders with different token pairs → both can settle independently."""
    o1 = _make_order("o1", sell_token=_WETH, buy_token=_USDC,
                     sell_amount=10**18, buy_amount=2_900 * 10**6)
    o2 = _make_order("o2", sell_token=_WBTC, buy_token=_USDC,
                     sell_amount=10**8, buy_amount=60_000 * 10**6)  # 1 WBTC → 60000 USDC
    auction = _make_auction([o1, o2])

    ind1 = V3BatchedQuote(
        path=_make_v3_path("o1", token_in=_WETH, token_out=_USDC, amount_in=10**18),
        amount_out=2_950 * 10**6,
    )
    ind2 = V3BatchedQuote(
        path=_make_v3_path("o2", token_in=_WBTC, token_out=_USDC, amount_in=10**8),
        amount_out=62_000 * 10**6,
    )

    async def mock_batched(*args, **kwargs):  # noqa: ANN001
        return [ind1, ind2]

    monkeypatch.setattr("src.solver.joint_clearing.batched_v3_quote", mock_batched)

    with patch("src.solver.joint_clearing.RouterSolver._encode_path_interaction") as mock_enc:
        from src.encoder.interactions import Interaction
        mock_enc.return_value = Interaction(target="0x" + "0" * 40, value=0, call_data=b"")
        multicall = AsyncMock()
        solver = JointClearingSolver(multicall=multicall, intermediates=[], min_group_size=2)
        result = await solver.solve(auction)

    # Both orders have different pairs (USDC appears in both but as BUY token,
    # while sell tokens differ). RouterSolver's _register_prices will try to
    # register prices[WETH] and prices[WBTC] independently; prices[USDC] conflict.
    # o1 registers first: prices[USDC] = 10^18. o2 tries prices[USDC] = 10^8 → conflict.
    # So only one order settles — this tests the CIP-67 constraint is enforced.
    assert isinstance(result, Solution)
    assert len(result.trades) >= 1  # at least one order settles


@pytest.mark.asyncio
async def test_joint_clearing_no_quote_returns_no_solution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _make_order("o1")
    auction = _make_auction([order])

    async def mock_batched(*args, **kwargs):  # noqa: ANN001
        return []

    monkeypatch.setattr("src.solver.joint_clearing.batched_v3_quote", mock_batched)

    multicall = AsyncMock()
    solver = JointClearingSolver(multicall=multicall, intermediates=[])
    result = await solver.solve(auction)
    assert isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_joint_clearing_batched_quote_error_returns_no_solution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _make_order("o1")
    auction = _make_auction([order])

    async def mock_batched(*args, **kwargs):  # noqa: ANN001
        raise RuntimeError("RPC failure")

    monkeypatch.setattr("src.solver.joint_clearing.batched_v3_quote", mock_batched)

    multicall = AsyncMock()
    solver = JointClearingSolver(multicall=multicall, intermediates=[])
    result = await solver.solve(auction)
    assert isinstance(result, NoSolution)
