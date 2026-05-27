"""Tests for V2Source.

Same mocking strategy as V3Source tests: Multicall3.aggregate is replaced
with an AsyncMock and we feed it crafted CallResult bytes that mimic
factory.getPair / pool.getReserves / pool.token0 returns. Real V2Source
math (closed-form direct + inverse) runs against these reserves.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_abi import decode

from src.encoder.v2_calldata import (
    SWAP_EXACT_TOKENS_FOR_TOKENS_SELECTOR,
    SWAP_TOKENS_FOR_EXACT_TOKENS_SELECTOR,
)
from src.liquidity.base import Quote, SwapRequest
from src.liquidity.v2 import V2Source, _get_amount_in, _V2RouteMetadata
from src.routing.amm_v2 import quote_v2_swap
from src.routing.multicall import CallResult

USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
DAI = "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1"
ROUTER = "0xc873fEcbd354f5A56E00E710B90EF4201db2448d"  # Camelot
FACTORY = "0x6EcCab422D763aC031210895C81787E87B43A652"
SETTLEMENT = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"


def _address_return(addr: str) -> bytes:
    """factory.getPair-style return: a single address left-padded into 32 bytes."""
    return b"\x00" * 12 + bytes.fromhex(addr[2:])


def _zero_address_return() -> bytes:
    """Empty pool (factory.getPair returns address(0))."""
    return b"\x00" * 32


def _reserves_return(reserve0: int, reserve1: int) -> bytes:
    """UniV2 pool.getReserves return: (uint112, uint112, uint32) into 96 bytes."""
    return (
        reserve0.to_bytes(32, "big")
        + reserve1.to_bytes(32, "big")
        + (0).to_bytes(32, "big")  # block timestamp last
    )


def _token0_return(token: str) -> bytes:
    return b"\x00" * 12 + bytes.fromhex(token[2:].lower())


def _make_source(
    intermediates: list[str] | None = None,
    slippage_bps: int = 50,
) -> tuple[V2Source, AsyncMock]:
    multicall = MagicMock()
    multicall.aggregate = AsyncMock()
    source = V2Source(
        name="camelot",
        multicall=multicall,
        router_address=ROUTER,
        factory_address=FACTORY,
        intermediate_tokens=intermediates if intermediates is not None else [WETH],
        slippage_bps=slippage_bps,
    )
    return source, multicall.aggregate


@pytest.mark.asyncio
async def test_sell_quote_direct_path_uses_constant_product() -> None:
    """One pool, one path. V2Source must call the same quote_v2_swap
    math the on-chain router will execute, so quoted output matches what
    the user will actually receive (modulo slippage)."""
    source, aggregate = _make_source(intermediates=[])
    pool_addr = "0x" + "ab" * 20
    # Two RPC round-trips: (1) getPair, (2) getReserves+token0.
    aggregate.side_effect = [
        # call 1: factory.getPair for (USDC, WETH)
        [CallResult(success=True, return_data=_address_return(pool_addr))],
        # call 2: pool.getReserves + pool.token0
        [
            CallResult(
                success=True,
                return_data=_reserves_return(1_000_000_000_000, 500_000_000_000_000_000_000),
            ),
            CallResult(success=True, return_data=_token0_return(USDC)),
        ],
    ]
    req = SwapRequest(
        sell_token=USDC, buy_token=WETH,
        sell_amount=1_000_000, buy_amount=0,
        kind="sell", chain_id=42161,
    )
    quote = await source.quote(req, timeout_ms=1000)
    assert quote is not None
    assert quote.sell_amount == 1_000_000
    # Verify against the same math V2Source uses internally so a future
    # change to fee_bps in either spot fails this assertion together.
    expected = quote_v2_swap(1_000_000, 1_000_000_000_000, 500_000_000_000_000_000_000, fee_bps=30)
    assert quote.buy_amount == expected
    meta = quote.route_metadata["v2_route"]
    assert meta.path == [USDC, WETH]


@pytest.mark.asyncio
async def test_no_pool_returns_none() -> None:
    """getPair returning address(0) for every candidate hop = no liquidity =
    quote() returns None."""
    source, aggregate = _make_source(intermediates=[])
    aggregate.side_effect = [
        [CallResult(success=True, return_data=_zero_address_return())],
    ]
    req = SwapRequest(
        sell_token=USDC, buy_token=WETH,
        sell_amount=1_000_000, buy_amount=0,
        kind="sell", chain_id=42161,
    )
    assert await source.quote(req, timeout_ms=1000) is None


@pytest.mark.asyncio
async def test_two_hop_uses_intermediate() -> None:
    """When no direct USDC/DAI pool exists but USDC/WETH and WETH/DAI do,
    V2Source must find the 2-hop path. Verifies the multi-hop math runs
    through the same pools in order."""
    source, aggregate = _make_source(intermediates=[WETH])
    usdc_weth_pool = "0x" + "11" * 20
    weth_dai_pool = "0x" + "22" * 20
    aggregate.side_effect = [
        # getPair for (USDC, DAI), (USDC, WETH), (WETH, DAI)
        [
            CallResult(success=True, return_data=_zero_address_return()),  # no direct
            CallResult(success=True, return_data=_address_return(usdc_weth_pool)),
            CallResult(success=True, return_data=_address_return(weth_dai_pool)),
        ],
        # reserves + token0 for each found pool
        [
            CallResult(success=True, return_data=_reserves_return(10**12, 10**18)),  # USDC/WETH
            CallResult(success=True, return_data=_token0_return(USDC)),
            CallResult(success=True, return_data=_reserves_return(10**18, 10**21)),  # WETH/DAI
            CallResult(success=True, return_data=_token0_return(WETH)),
        ],
    ]
    req = SwapRequest(
        sell_token=USDC, buy_token=DAI,
        sell_amount=1_000_000, buy_amount=0,
        kind="sell", chain_id=42161,
    )
    quote = await source.quote(req, timeout_ms=1000)
    assert quote is not None
    meta = quote.route_metadata["v2_route"]
    assert meta.path == [USDC, WETH, DAI]


@pytest.mark.asyncio
async def test_buy_kind_uses_closed_form_inverse() -> None:
    """Buy-kind quotes walk the path backwards via _get_amount_in. Verify
    the result matches what the on-chain getAmountIn formula would
    produce — anything else means we'd quote one number and the router
    would consume a different one."""
    source, aggregate = _make_source(intermediates=[])
    pool_addr = "0x" + "ab" * 20
    aggregate.side_effect = [
        [CallResult(success=True, return_data=_address_return(pool_addr))],
        [
            CallResult(
                success=True,
                return_data=_reserves_return(1_000_000_000_000, 500_000_000_000_000_000_000),
            ),
            CallResult(success=True, return_data=_token0_return(USDC)),
        ],
    ]
    req = SwapRequest(
        sell_token=USDC, buy_token=WETH,
        sell_amount=0, buy_amount=10**14,
        kind="buy", chain_id=42161,
    )
    quote = await source.quote(req, timeout_ms=1000)
    assert quote is not None
    # Inverse math: input USDC needed to buy 10^14 wei WETH.
    expected = _get_amount_in(10**14, 1_000_000_000_000, 500_000_000_000_000_000_000, fee_bps=30)
    assert quote.sell_amount == expected
    assert quote.buy_amount == 10**14
    # The ":buy" suffix is how encode_interaction discriminates kind without
    # having to re-read it from the SwapRequest (which we don't have there).
    assert quote.source.endswith(":buy")


def test_encode_interaction_uses_swap_exact_tokens_for_sell() -> None:
    source, _ = _make_source(intermediates=[])
    quote = Quote(
        source="camelot",
        sell_amount=1_000_000,
        buy_amount=345_000_000_000_000,
        valid_until=1_900_000_000,
        route_metadata={
            "v2_route": _V2RouteMetadata(path=[USDC, WETH], deadline=1_900_000_000),
        },
    )
    interaction = source.encode_interaction(quote, SETTLEMENT)
    assert interaction.target == ROUTER
    assert interaction.call_data[:4] == SWAP_EXACT_TOKENS_FOR_TOKENS_SELECTOR


def test_encode_interaction_uses_swap_tokens_for_exact_for_buy() -> None:
    """The ":buy" suffix on quote.source is what tells the encoder to pick
    the exactOutput variant. Drop the suffix → wrong calldata → revert."""
    source, _ = _make_source(intermediates=[])
    quote = Quote(
        source="camelot:buy",
        sell_amount=345,
        buy_amount=10**15,
        valid_until=1_900_000_000,
        route_metadata={
            "v2_route": _V2RouteMetadata(path=[USDC, WETH], deadline=1_900_000_000),
        },
    )
    interaction = source.encode_interaction(quote, SETTLEMENT)
    assert interaction.call_data[:4] == SWAP_TOKENS_FOR_EXACT_TOKENS_SELECTOR


def test_slippage_applied_to_amount_out_minimum_on_sell() -> None:
    source, _ = _make_source(intermediates=[], slippage_bps=50)
    quote = Quote(
        source="camelot",
        sell_amount=1_000_000,
        buy_amount=10_000,
        valid_until=1_900_000_000,
        route_metadata={
            "v2_route": _V2RouteMetadata(path=[USDC, WETH], deadline=1_900_000_000),
        },
    )
    interaction = source.encode_interaction(quote, SETTLEMENT)
    arg_types = ["uint256", "uint256", "address[]", "address", "uint256"]
    amount_in, amount_out_minimum, _, _, _ = decode(arg_types, interaction.call_data[4:])
    assert amount_in == 1_000_000
    assert amount_out_minimum == 9_950  # 10_000 * (10000 - 50) / 10000


def test_required_allowances_returns_path_first_token_pair() -> None:
    source, _ = _make_source(intermediates=[])
    quote = Quote(
        source="camelot",
        sell_amount=1_000_000,
        buy_amount=10_000,
        valid_until=1_900_000_000,
        route_metadata={
            "v2_route": _V2RouteMetadata(path=[USDC, WETH], deadline=1_900_000_000),
        },
    )
    # Even though path has multiple tokens, only the first one needs
    # allowance — the router holds intermediates internally during multi-hop.
    assert source.required_allowances(quote) == [(USDC, ROUTER)]


def test_encode_interaction_rejects_alien_route_metadata() -> None:
    source, _ = _make_source(intermediates=[])
    bad_quote = Quote(
        source="camelot",
        sell_amount=1, buy_amount=1, valid_until=1,
        route_metadata={},
    )
    with pytest.raises(ValueError, match="v2_route"):
        source.encode_interaction(bad_quote, SETTLEMENT)


def test_get_amount_in_returns_zero_when_output_exceeds_reserve() -> None:
    """Edge case: caller asks for more output than the pool contains.
    Closed-form formula would divide by negative; we explicitly return 0
    so the caller skips the path."""
    assert _get_amount_in(
        amount_out=1_000_001,
        reserve_in=10**18,
        reserve_out=1_000_000,
        fee_bps=30,
    ) == 0


def test_get_amount_in_matches_uniswap_v2_formula() -> None:
    """Verify against a hand-computed example so future refactors of
    the formula can't drift silently.

    For amount_out=1000, reserve_in=10000, reserve_out=10000, fee=30bps:
      numerator   = 10000 * 1000 * 10000           = 1e11
      denominator = (10000 - 1000) * (10000 - 30) = 9000 * 9970 = 89_730_000
      amount_in   = 1e11 // 89_730_000 + 1        = 1114 + 1 = 1115
    """
    assert _get_amount_in(1000, 10000, 10000, 30) == 1115
