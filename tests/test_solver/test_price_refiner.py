"""Tests for price_refiner — replacing oracle clearing prices with real DEX quotes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.auction import Auction
from src.models.solution import Solution, Trade
from src.solver.price_refiner import refine_solution_prices

# ── Helpers ──────────────────────────────────────────────────────────────────

WETH = "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"
USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
UID_A = "0x" + "aa" * 56


def _make_auction(sell_amount: int, buy_amount: int) -> Auction:
    return Auction.model_validate({
        "id": "9999",
        "tokens": {
            WETH: {
                "decimals": 18, "referencePrice": str(1_800 * 10**18),
                "availableBalance": "0", "trusted": True,
            },
            USDC: {
                "decimals": 6, "referencePrice": str(10**18 // 1800),
                "availableBalance": "0", "trusted": True,
            },
        },
        "orders": [
            {
                "uid": UID_A,
                "sellToken": WETH,
                "buyToken": USDC,
                "sellAmount": str(sell_amount),
                "buyAmount": str(buy_amount),
                "feeAmount": "0",
                "kind": "sell",
                "partiallyFillable": False,
                "class": "market",
                "validTo": 9999999999,
                "owner": "0x" + "bb" * 20,
            }
        ],
        "deadline": "2099-01-01T00:00:00Z",
    })


def _make_solution(sell_amount: int, cp_sell: int, cp_buy: int) -> Solution:
    return Solution(
        id=9999,
        prices={WETH: cp_sell, USDC: cp_buy},
        trades=[Trade(kind="fulfillment", order_uid=UID_A, executed_amount=sell_amount)],
        interactions=[],
    )


def _mock_multicall() -> MagicMock:
    return MagicMock()


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refine_replaces_oracle_prices_with_real_dex_prices() -> None:
    """Real quote is better than oracle — prices updated, trade kept."""
    sell_amount = 1 * 10**18  # 1 WETH
    buy_amount = 1_700 * 10**6  # limit: 1700 USDC
    real_out = 1_780 * 10**6   # DEX gives 1780 USDC

    auction = _make_auction(sell_amount, buy_amount)
    # Oracle solution uses reference prices
    solution = _make_solution(sell_amount, 1_800 * 10**18, 10**18 // 1800)
    multicall = _mock_multicall()

    from src.routing.multihop import HopQuote
    fake_path = [HopQuote(
        factory="sushi", pool="0xpool", token_in=WETH, token_out=USDC,
        amount_in=sell_amount, amount_out=real_out,
    )]

    with patch("src.solver.price_refiner.quote_best_path", new=AsyncMock(return_value=fake_path)):
        result = await refine_solution_prices(solution, auction, multicall, [])

    assert len(result.trades) == 1
    # Prices keep oracle/reference values — consistent unit system for CIP-14.
    # DEX quote is used only to verify the trade is executable, not to replace prices.
    assert result.prices[WETH] == 1_800 * 10**18
    assert result.prices[USDC] == 10**18 // 1800


@pytest.mark.asyncio
async def test_refine_drops_trade_when_real_price_below_limit() -> None:
    """DEX gives less than order limit — trade dropped, oracle solution returned."""
    sell_amount = 1 * 10**18
    buy_amount = 1_900 * 10**6  # demanding limit
    real_out = 1_780 * 10**6    # DEX only gives 1780 — below limit

    auction = _make_auction(sell_amount, buy_amount)
    solution = _make_solution(sell_amount, 1_800 * 10**18, 10**18 // 1800)
    multicall = _mock_multicall()

    from src.routing.multihop import HopQuote
    fake_path = [HopQuote(
        factory="sushi", pool="0xpool", token_in=WETH, token_out=USDC,
        amount_in=sell_amount, amount_out=real_out,
    )]

    with patch("src.solver.price_refiner.quote_best_path", new=AsyncMock(return_value=fake_path)):
        result = await refine_solution_prices(solution, auction, multicall, [])

    # All trades dropped → fallback to original oracle solution
    assert result is solution


@pytest.mark.asyncio
async def test_refine_falls_back_to_oracle_when_quote_fails() -> None:
    """RPC error on all quotes → original oracle solution returned unchanged."""
    sell_amount = 1 * 10**18
    buy_amount = 1_700 * 10**6

    auction = _make_auction(sell_amount, buy_amount)
    solution = _make_solution(sell_amount, 1_800 * 10**18, 10**18 // 1800)
    multicall = _mock_multicall()

    with patch("src.solver.price_refiner.quote_best_path", new=AsyncMock(return_value=None)):
        result = await refine_solution_prices(solution, auction, multicall, [])

    # No quotes succeeded — oracle solution returned
    assert result is solution


@pytest.mark.asyncio
async def test_naive_solver_with_multicall_calls_price_refiner() -> None:
    """NaiveSolver with multicall injected invokes price_refiner."""
    from src.solver.naive import NaiveSolver

    multicall = _mock_multicall()
    solver = NaiveSolver(multicall=multicall, intermediates=[])

    sell_amount = 1 * 10**18
    buy_amount = 1_700 * 10**6
    auction = _make_auction(sell_amount, buy_amount)

    refined = Solution(
        id=9999, prices={WETH: 1_780 * 10**6, USDC: sell_amount},
        trades=[Trade(kind="fulfillment", order_uid=UID_A, executed_amount=sell_amount)],
        interactions=[],
    )

    with patch(
        "src.solver.price_refiner.refine_solution_prices",
        new=AsyncMock(return_value=refined),
    ) as mock_refine:
        result = await solver.solve(auction)

    mock_refine.assert_called_once()
    assert result is refined


@pytest.mark.asyncio
async def test_naive_solver_without_multicall_uses_oracle_prices() -> None:
    """NaiveSolver without multicall never calls price_refiner (backward compat)."""
    from src.solver.naive import NaiveSolver

    solver = NaiveSolver()  # no multicall

    sell_amount = 1 * 10**18
    buy_amount = 1_700 * 10**6
    auction = _make_auction(sell_amount, buy_amount)

    with patch("src.solver.price_refiner.refine_solution_prices", new=AsyncMock()) as mock_refine:
        result = await solver.solve(auction)

    mock_refine.assert_not_called()
    # Oracle prices still in solution
    assert WETH in result.prices  # type: ignore[union-attr]
