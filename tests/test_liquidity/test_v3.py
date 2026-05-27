"""Tests for V3Source.

Strategy: mock Multicall3.aggregate to return crafted CallResult bytes that
mimic QuoterV2's reply shape, then assert V3Source's quote() and
encode_interaction() do the right thing with them. This keeps the tests
hermetic — no RPC dependency — while still exercising the real path
construction and the real V3 calldata encoders.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_abi import decode, encode

from src.encoder.v3_calldata import (
    EXACT_INPUT_SINGLE_SELECTOR,
    EXACT_OUTPUT_SINGLE_SELECTOR,
)
from src.encoder.v3_path import EXACT_INPUT_SELECTOR, EXACT_OUTPUT_SELECTOR
from src.liquidity.base import Quote, SwapRequest
from src.liquidity.v3 import V3Source
from src.routing.multicall import CallResult

USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
DAI = "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1"
ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
SETTLEMENT = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"


def _qv2_single_hop_return(amount_out: int) -> bytes:
    """Pack a QuoterV2 quoteExactInputSingle return tuple.

    QuoterV2 returns (uint256 amountOut, uint160 sqrtPriceX96After,
    uint32 initializedTicksCrossed, uint256 gasEstimate). We don't care
    about the latter three for V3Source's logic — zeros are fine."""
    return encode(
        ["uint256", "uint160", "uint32", "uint256"],
        [amount_out, 0, 0, 0],
    )


def _qv2_multi_hop_return(amount_out: int) -> bytes:
    """Pack a QuoterV2 quoteExactInput (path-based) return tuple.

    Returns (uint256 amountOut, uint160[] sqrtPriceX96AfterList,
    uint32[] initializedTicksCrossedList, uint256 gasEstimate). Same
    don't-care for everything except amountOut."""
    return encode(
        ["uint256", "uint160[]", "uint32[]", "uint256"],
        [amount_out, [], [], 0],
    )


def _make_source(
    intermediates: list[str] | None = None,
    slippage_bps: int = 50,
) -> tuple[V3Source, AsyncMock]:
    """Build a V3Source with a mocked Multicall3.aggregate.

    Returns the source and the AsyncMock so tests can configure
    aggregate.return_value with crafted CallResult lists.
    """
    multicall = MagicMock()
    multicall.aggregate = AsyncMock()
    source = V3Source(
        multicall=multicall,
        router_address=ROUTER,
        intermediate_tokens=intermediates if intermediates is not None else [WETH],
        slippage_bps=slippage_bps,
    )
    return source, multicall.aggregate


@pytest.mark.asyncio
async def test_sell_quote_picks_max_amount_out() -> None:
    """Among 4 fee-tier quotes for a direct hop, V3Source must pick the
    one with the highest output. Equal-amount paths should resolve to
    *some* viable quote (not None)."""
    source, aggregate = _make_source(intermediates=[])  # disable 2-hop
    # 4 direct fee-tier quotes — winner is fee tier with highest amount_out.
    aggregate.return_value = [
        CallResult(success=True, return_data=_qv2_single_hop_return(100)),  # tier 100
        CallResult(success=True, return_data=_qv2_single_hop_return(345)),  # tier 500
        CallResult(success=True, return_data=_qv2_single_hop_return(200)),  # tier 3000
        CallResult(success=True, return_data=_qv2_single_hop_return(150)),  # tier 10000
    ]
    req = SwapRequest(
        sell_token=USDC, buy_token=WETH,
        sell_amount=1_000_000, buy_amount=0,
        kind="sell", chain_id=42161,
    )
    quote = await source.quote(req, timeout_ms=1000)
    assert quote is not None
    assert quote.sell_amount == 1_000_000
    assert quote.buy_amount == 345
    # winning route must be the 0.05% direct hop
    meta = quote.route_metadata["v3_route"]
    assert meta.path.fee_tier_in == 500
    assert meta.path.intermediate is None


@pytest.mark.asyncio
async def test_buy_quote_picks_min_amount_in() -> None:
    """For buy-kind, the variable side is the input amount and we want
    it minimised. Verify V3Source picks the cheapest-input route."""
    source, aggregate = _make_source(intermediates=[])
    aggregate.return_value = [
        CallResult(success=True, return_data=_qv2_single_hop_return(500)),  # tier 100: 500 in
        CallResult(success=True, return_data=_qv2_single_hop_return(345)),  # tier 500: best
        CallResult(success=True, return_data=_qv2_single_hop_return(400)),  # tier 3000
        CallResult(success=True, return_data=_qv2_single_hop_return(450)),  # tier 10000
    ]
    req = SwapRequest(
        sell_token=USDC, buy_token=WETH,
        sell_amount=0, buy_amount=10**15,
        kind="buy", chain_id=42161,
    )
    quote = await source.quote(req, timeout_ms=1000)
    assert quote is not None
    assert quote.buy_amount == 10**15  # the fixed side
    assert quote.sell_amount == 345  # cheapest input we found
    meta = quote.route_metadata["v3_route"]
    assert meta.path.fee_tier_in == 500


@pytest.mark.asyncio
async def test_returns_none_when_all_paths_revert() -> None:
    """Quoter returning zero on every path = no liquidity = quote() returns
    None. The aggregator relies on this to skip the source cleanly."""
    source, aggregate = _make_source(intermediates=[])
    aggregate.return_value = [
        CallResult(success=False, return_data=b""),
        CallResult(success=False, return_data=b""),
        CallResult(success=False, return_data=b""),
        CallResult(success=False, return_data=b""),
    ]
    req = SwapRequest(
        sell_token=USDC, buy_token=WETH,
        sell_amount=1_000_000, buy_amount=0,
        kind="sell", chain_id=42161,
    )
    assert await source.quote(req, timeout_ms=1000) is None


@pytest.mark.asyncio
async def test_returns_none_when_rpc_raises() -> None:
    """Protocol contract: never raise — RPC explosions return None."""
    source, aggregate = _make_source(intermediates=[])
    aggregate.side_effect = ConnectionError("rpc down")
    req = SwapRequest(
        sell_token=USDC, buy_token=WETH,
        sell_amount=1_000_000, buy_amount=0,
        kind="sell", chain_id=42161,
    )
    assert await source.quote(req, timeout_ms=1000) is None


@pytest.mark.asyncio
async def test_two_hop_is_picked_when_better_than_direct() -> None:
    """4 direct quotes + 16 two-hop quotes (4 × 4). If the best two-hop
    beats the best direct, V3Source must encode a multi-hop swap."""
    source, aggregate = _make_source(intermediates=[WETH])
    # Direct quotes (4 fee tiers) — modest output
    direct = [CallResult(success=True, return_data=_qv2_single_hop_return(100)) for _ in range(4)]
    # Two-hop quotes (4 × 4 = 16) — one is high
    two_hop = [CallResult(success=True, return_data=_qv2_multi_hop_return(50)) for _ in range(16)]
    # Make one two-hop the global winner
    two_hop[5] = CallResult(success=True, return_data=_qv2_multi_hop_return(999))
    aggregate.return_value = direct + two_hop

    req = SwapRequest(
        sell_token=USDC, buy_token=DAI,
        sell_amount=1_000_000, buy_amount=0,
        kind="sell", chain_id=42161,
    )
    quote = await source.quote(req, timeout_ms=1000)
    assert quote is not None
    assert quote.buy_amount == 999
    meta = quote.route_metadata["v3_route"]
    assert meta.path.intermediate is not None
    assert meta.path.intermediate.lower() == WETH.lower()


def test_encode_interaction_uses_exact_input_single_for_sell_direct() -> None:
    """Sell-kind direct hop → exactInputSingle. The encoder's selector +
    target must reflect this; downstream Tenderly tests then verify the
    full byte-level fidelity."""
    source, _ = _make_source(intermediates=[])
    from src.liquidity.v3 import _V3RouteMetadata
    from src.routing.v3_batched import V3Path

    quote = Quote(
        source="v3",
        sell_amount=1_000_000,
        buy_amount=345_000_000_000_000,
        valid_until=1_900_000_000,
        route_metadata={
            "v3_route": _V3RouteMetadata(
                path=V3Path(
                    order_uid="o", token_in=USDC, token_out=WETH,
                    amount_in=1_000_000, fee_tier_in=500, exact_output=False,
                ),
                deadline=1_900_000_000,
            )
        },
    )
    interaction = source.encode_interaction(quote, SETTLEMENT)
    assert interaction.target == ROUTER
    assert interaction.value == 0
    assert interaction.call_data[:4] == EXACT_INPUT_SINGLE_SELECTOR


def test_encode_interaction_uses_exact_output_single_for_buy_direct() -> None:
    source, _ = _make_source(intermediates=[])
    from src.liquidity.v3 import _V3RouteMetadata
    from src.routing.v3_batched import V3Path

    quote = Quote(
        source="v3",
        sell_amount=345,
        buy_amount=10**15,
        valid_until=1_900_000_000,
        route_metadata={
            "v3_route": _V3RouteMetadata(
                path=V3Path(
                    order_uid="o", token_in=USDC, token_out=WETH,
                    amount_in=10**15, fee_tier_in=500, exact_output=True,
                ),
                deadline=1_900_000_000,
            )
        },
    )
    interaction = source.encode_interaction(quote, SETTLEMENT)
    assert interaction.call_data[:4] == EXACT_OUTPUT_SINGLE_SELECTOR


def test_encode_interaction_uses_exact_input_for_multihop_sell() -> None:
    source, _ = _make_source(intermediates=[])
    from src.liquidity.v3 import _V3RouteMetadata
    from src.routing.v3_batched import V3Path

    quote = Quote(
        source="v3",
        sell_amount=1_000_000,
        buy_amount=99_000_000_000_000_000_000,
        valid_until=1_900_000_000,
        route_metadata={
            "v3_route": _V3RouteMetadata(
                path=V3Path(
                    order_uid="o", token_in=USDC, token_out=DAI,
                    amount_in=1_000_000, fee_tier_in=500,
                    intermediate=WETH, fee_tier_out=3000, exact_output=False,
                ),
                deadline=1_900_000_000,
            )
        },
    )
    interaction = source.encode_interaction(quote, SETTLEMENT)
    assert interaction.call_data[:4] == EXACT_INPUT_SELECTOR


def test_encode_interaction_uses_exact_output_for_multihop_buy() -> None:
    source, _ = _make_source(intermediates=[])
    from src.liquidity.v3 import _V3RouteMetadata
    from src.routing.v3_batched import V3Path

    quote = Quote(
        source="v3",
        sell_amount=1_000_000,
        buy_amount=99_000_000_000_000_000_000,
        valid_until=1_900_000_000,
        route_metadata={
            "v3_route": _V3RouteMetadata(
                path=V3Path(
                    order_uid="o", token_in=USDC, token_out=DAI,
                    amount_in=99_000_000_000_000_000_000, fee_tier_in=500,
                    intermediate=WETH, fee_tier_out=3000, exact_output=True,
                ),
                deadline=1_900_000_000,
            )
        },
    )
    interaction = source.encode_interaction(quote, SETTLEMENT)
    assert interaction.call_data[:4] == EXACT_OUTPUT_SELECTOR


def test_slippage_applied_to_amount_out_minimum_on_sell() -> None:
    """50bps slippage → amountOutMinimum is 99.5% of quoted buy_amount."""
    source, _ = _make_source(intermediates=[], slippage_bps=50)
    from src.liquidity.v3 import _V3RouteMetadata
    from src.routing.v3_batched import V3Path

    quote = Quote(
        source="v3",
        sell_amount=1_000_000,
        buy_amount=10_000,
        valid_until=1_900_000_000,
        route_metadata={
            "v3_route": _V3RouteMetadata(
                path=V3Path(
                    order_uid="o", token_in=USDC, token_out=WETH,
                    amount_in=1_000_000, fee_tier_in=500, exact_output=False,
                ),
                deadline=1_900_000_000,
            )
        },
    )
    interaction = source.encode_interaction(quote, SETTLEMENT)
    # Decode the struct and read amountOutMinimum (slot 6 of the tuple)
    struct_t = "(address,address,uint24,address,uint256,uint256,uint256,uint160)"
    (params,) = decode([struct_t], interaction.call_data[4:])
    _, _, _, _, _, _, amount_out_minimum, _ = params
    # 10_000 * (10000 - 50) / 10000 = 9950
    assert amount_out_minimum == 9950


def test_required_allowances_returns_sell_token_router_pair() -> None:
    source, _ = _make_source(intermediates=[])
    from src.liquidity.v3 import _V3RouteMetadata
    from src.routing.v3_batched import V3Path

    quote = Quote(
        source="v3",
        sell_amount=1_000_000,
        buy_amount=10_000,
        valid_until=1_900_000_000,
        route_metadata={
            "v3_route": _V3RouteMetadata(
                path=V3Path(
                    order_uid="o", token_in=USDC, token_out=WETH,
                    amount_in=1_000_000, fee_tier_in=500, exact_output=False,
                ),
                deadline=1_900_000_000,
            )
        },
    )
    assert source.required_allowances(quote) == [(USDC, ROUTER)]


def test_encode_interaction_rejects_alien_route_metadata() -> None:
    """Catching the case where a quote produced by another source is
    accidentally fed into V3Source.encode_interaction."""
    source, _ = _make_source(intermediates=[])
    bad_quote = Quote(
        source="v3",
        sell_amount=1, buy_amount=1, valid_until=1,
        route_metadata={},  # no 'v3_route' key
    )
    with pytest.raises(ValueError, match="v3_route"):
        source.encode_interaction(bad_quote, SETTLEMENT)
