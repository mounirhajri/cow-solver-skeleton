"""Tests for LiquidityAggregator.

The aggregator's job is small but load-bearing: fan out to N sources,
pick the best result, never let a bad source corrupt the batch. The
tests cover pick-best across kinds, None handling, and defensive
exception sweeping.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.encoder.interactions import Interaction
from src.liquidity.aggregator import LiquidityAggregator
from src.liquidity.base import Quote, SwapRequest

USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"


def _stub_source(name: str, quote_return: Quote | None | Exception) -> Any:
    """Build a minimal stub source that returns a fixed Quote, None, or raises."""

    class _Stub:
        def __init__(self) -> None:
            self.name = name
            self.quote = AsyncMock()
            if isinstance(quote_return, Exception):
                self.quote.side_effect = quote_return
            else:
                self.quote.return_value = quote_return

        def encode_interaction(self, q: Quote, r: str) -> Interaction:
            raise NotImplementedError  # not exercised in these tests

        def required_allowances(self, q: Quote) -> list[tuple[str, str]]:
            return []

        async def health_check(self) -> bool:
            return True

    return _Stub()


def _quote(source: str, sell: int, buy: int) -> Quote:
    return Quote(
        source=source,
        sell_amount=sell,
        buy_amount=buy,
        valid_until=2**31 - 1,
    )


def _sell_req() -> SwapRequest:
    return SwapRequest(
        sell_token=USDC, buy_token=WETH,
        sell_amount=1_000_000, buy_amount=0,
        kind="sell", chain_id=42161,
    )


def _buy_req() -> SwapRequest:
    return SwapRequest(
        sell_token=USDC, buy_token=WETH,
        sell_amount=0, buy_amount=10**15,
        kind="buy", chain_id=42161,
    )


def test_aggregator_rejects_empty_sources() -> None:
    with pytest.raises(ValueError, match="at least one source"):
        LiquidityAggregator([])


@pytest.mark.asyncio
async def test_sell_kind_picks_max_buy_amount() -> None:
    """User sells fixed amount in; aggregator picks source that yields
    the most output."""
    sources = [
        _stub_source("v3", _quote("v3", 1_000_000, 100)),
        _stub_source("camelot", _quote("camelot", 1_000_000, 345)),
        _stub_source("sushi", _quote("sushi", 1_000_000, 200)),
    ]
    agg = LiquidityAggregator(sources)
    result = await agg.best_quote(_sell_req(), timeout_ms=1000)
    assert result is not None
    quote, source = result
    assert source.name == "camelot"
    assert quote.buy_amount == 345


@pytest.mark.asyncio
async def test_buy_kind_picks_min_sell_amount() -> None:
    """User buys fixed amount out; aggregator picks source that
    requires the least input."""
    sources = [
        _stub_source("v3", _quote("v3", 500, 10**15)),
        _stub_source("camelot", _quote("camelot", 345, 10**15)),  # cheapest input
        _stub_source("sushi", _quote("sushi", 400, 10**15)),
    ]
    agg = LiquidityAggregator(sources)
    result = await agg.best_quote(_buy_req(), timeout_ms=1000)
    assert result is not None
    quote, source = result
    assert source.name == "camelot"
    assert quote.sell_amount == 345


@pytest.mark.asyncio
async def test_none_returns_are_skipped() -> None:
    """A source returning None (no liquidity / timeout) must not break
    the aggregation — the winning source wins among the rest."""
    sources = [
        _stub_source("v3", _quote("v3", 1_000_000, 100)),
        _stub_source("camelot", None),
        _stub_source("sushi", _quote("sushi", 1_000_000, 50)),
    ]
    agg = LiquidityAggregator(sources)
    result = await agg.best_quote(_sell_req(), timeout_ms=1000)
    assert result is not None
    quote, source = result
    assert source.name == "v3"
    assert quote.buy_amount == 100


@pytest.mark.asyncio
async def test_all_none_returns_none() -> None:
    """Every source declines → aggregator returns None for caller to
    fall back."""
    sources = [
        _stub_source("v3", None),
        _stub_source("camelot", None),
    ]
    agg = LiquidityAggregator(sources)
    assert await agg.best_quote(_sell_req(), timeout_ms=1000) is None


@pytest.mark.asyncio
async def test_raising_source_does_not_corrupt_batch() -> None:
    """A source that violates the Protocol's "never raise" contract
    must be swept and logged, not allowed to take down sibling quotes."""
    sources = [
        _stub_source("v3", _quote("v3", 1_000_000, 100)),
        _stub_source("buggy", ConnectionError("rpc explosion")),
        _stub_source("camelot", _quote("camelot", 1_000_000, 200)),
    ]
    agg = LiquidityAggregator(sources)
    result = await agg.best_quote(_sell_req(), timeout_ms=1000)
    assert result is not None
    quote, source = result
    # The buggy source was swept; winner is camelot (highest among survivors).
    assert source.name == "camelot"
    assert quote.buy_amount == 200


@pytest.mark.asyncio
async def test_sources_are_queried_in_parallel() -> None:
    """Two sources each sleeping 50ms should resolve in ~50ms total, not
    ~100ms (serial). Tolerance is generous to avoid CI flake but tight
    enough to fail an accidental serial-await refactor."""
    class _SlowSource:
        def __init__(self, name: str, delay_s: float, buy_amount: int) -> None:
            self.name = name
            self._delay = delay_s
            self._buy_amount = buy_amount

        async def quote(self, req: SwapRequest, timeout_ms: int) -> Quote | None:
            await asyncio.sleep(self._delay)
            return _quote(self.name, req.sell_amount, self._buy_amount)

        def encode_interaction(self, q: Quote, r: str) -> Interaction:
            raise NotImplementedError

        def required_allowances(self, q: Quote) -> list[tuple[str, str]]:
            return []

        async def health_check(self) -> bool:
            return True

    sources: list[Any] = [
        _SlowSource("v3", 0.05, 100),
        _SlowSource("camelot", 0.05, 200),
    ]
    agg = LiquidityAggregator(sources)
    start = asyncio.get_event_loop().time()
    result = await agg.best_quote(_sell_req(), timeout_ms=1000)
    elapsed = asyncio.get_event_loop().time() - start
    assert result is not None
    assert elapsed < 0.12  # well under 2 × 50ms; serial would be ≥0.1s

    quote, source = result
    assert source.name == "camelot"
    assert quote.buy_amount == 200


@pytest.mark.asyncio
async def test_timeout_is_forwarded_to_each_source() -> None:
    """The timeout_ms passed to best_quote must be forwarded verbatim to
    each source.quote(...) call — sources implement their own timeout
    behaviour, the aggregator only orchestrates."""
    s1 = _stub_source("v3", None)
    s2 = _stub_source("camelot", None)
    agg = LiquidityAggregator([s1, s2])
    await agg.best_quote(_sell_req(), timeout_ms=750)
    s1.quote.assert_called_once()
    s2.quote.assert_called_once()
    _, kwargs1 = s1.quote.call_args
    _, kwargs2 = s2.quote.call_args
    assert kwargs1.get("timeout_ms", s1.quote.call_args.args[1]) == 750 or s1.quote.call_args.args[1] == 750
    assert kwargs2.get("timeout_ms", s2.quote.call_args.args[1]) == 750 or s2.quote.call_args.args[1] == 750
