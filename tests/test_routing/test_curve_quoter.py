"""Tests for batched Curve stable-pool quoter (mocked Multicall3)."""
from unittest.mock import AsyncMock

import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from src.routing.curve_quoter import (
    CURVE_POOLS,
    GET_DY_SELECTOR,
    CurvePath,
    CurvePool,
    CurveQuote,
    _build_call,
    _decode_get_dy_return,
    _encode_get_dy,
    batched_curve_quote,
    make_curve_paths,
)
from src.routing.multicall import Call, CallResult, Multicall3

# The verified Arbitrum pools — used as fixtures so the tests double as a
# regression pin on the hardcoded table.
_TWO_POOL = CURVE_POOLS[0]
_NG_POOL = CURVE_POOLS[1]
_USDC_E = _TWO_POOL.coins[0]
_USDT = _TWO_POOL.coins[1]
_USDC = _NG_POOL.coins[0]


def _path(pool: CurvePool, i: int, j: int, amount_in: int = 100_000_000) -> CurvePath:
    return CurvePath(
        order_uid="o1",
        token_in=pool.coins[i],
        token_out=pool.coins[j],
        amount_in=amount_in,
        pool=pool,
        i=i,
        j=j,
    )


def test_get_dy_selector_matches_keccak() -> None:
    """Pin the selector against keccak of the canonical Vyper signature —
    int128 indices, NOT uint256 (a uint256 signature would produce a
    different selector and every quote would revert)."""
    assert keccak(b"get_dy(int128,int128,uint256)")[:4] == GET_DY_SELECTOR
    assert GET_DY_SELECTOR.hex() == "5e0d443f"


def test_pool_table_pins() -> None:
    """Regression-pin the verified pool table (addresses + coin order were
    verified on-chain via coins(0)/coins(1) + Curve API, 2026-06-12)."""
    assert _TWO_POOL.address == "0x7f90122BF0700F9E7e1F688fe926940E8839F353"
    assert _TWO_POOL.coins == (
        "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8",  # USDC.e index 0
        "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",  # USDT index 1
    )
    assert _TWO_POOL.is_ng is False
    assert _NG_POOL.address == "0x49b720F1Aab26260BEAec93A7BeB5BF2925b2A8F"
    assert _NG_POOL.coins == (
        "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # native USDC index 0
        "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",  # USDT index 1
    )
    assert _NG_POOL.is_ng is True


def test_make_curve_paths_both_directions() -> None:
    """USDC.e→USDT hits only the 2pool with (i=0, j=1); the reverse
    direction flips the indices."""
    sell = make_curve_paths("o1", _USDC_E, _USDT, 10**6)
    assert len(sell) == 1
    assert sell[0].pool is _TWO_POOL
    assert (sell[0].i, sell[0].j) == (0, 1)
    assert sell[0].order_uid == "o1"
    assert sell[0].amount_in == 10**6
    assert sell[0].exact_output is False  # ALWAYS exact-input

    buy_dir = make_curve_paths("o1", _USDT, _USDC_E, 10**6)
    assert len(buy_dir) == 1
    assert (buy_dir[0].i, buy_dir[0].j) == (1, 0)


def test_make_curve_paths_native_usdc_hits_ng_pool() -> None:
    paths = make_curve_paths("o1", _USDC, _USDT, 10**6)
    assert [p.pool.name for p in paths] == ["strategic-usd-reserves"]
    assert (paths[0].i, paths[0].j) == (0, 1)


def test_make_curve_paths_usdt_in_both_pools_yields_no_cross_match() -> None:
    """USDT appears in BOTH pools but USDC.e only in the 2pool — a
    USDC.e/USDT order must not produce a path through the NG pool."""
    paths = make_curve_paths("o1", _USDT, _USDC_E, 10**6)
    assert [p.pool.name for p in paths] == ["2pool"]


def test_make_curve_paths_case_insensitive() -> None:
    """Auction orders carry lowercase addresses; matching must not depend
    on the checksummed casing in the pool table."""
    paths = make_curve_paths("o1", _USDC_E.lower(), _USDT.lower(), 10**6)
    assert len(paths) == 1
    assert (paths[0].i, paths[0].j) == (0, 1)
    # Path keeps the caller's casing (settlement encoding normalises later).
    assert paths[0].token_in == _USDC_E.lower()


def test_make_curve_paths_unknown_token_is_empty() -> None:
    weth = "0x" + "ee" * 20
    assert make_curve_paths("o1", weth, _USDT, 10**18) == []
    assert make_curve_paths("o1", _USDT, weth, 10**18) == []


def test_encode_get_dy_golden() -> None:
    """Round-trip the encoded args through eth_abi.decode and assert
    selector + every field."""
    path = _path(_TWO_POOL, i=1, j=0, amount_in=100_000_000)
    cd = _encode_get_dy(path)
    assert cd.startswith("0x" + GET_DY_SELECTOR.hex())
    i, j, dx = decode(["int128", "int128", "uint256"], bytes.fromhex(cd[2 + 8 :]))
    assert (i, j, dx) == (1, 0, 100_000_000)


def test_build_call_targets_pool_and_allows_failure() -> None:
    call = _build_call(_path(_NG_POOL, i=0, j=1))
    assert isinstance(call, Call)
    assert call.target == _NG_POOL.address
    assert call.allow_failure is True
    assert call.call_data.startswith("0x" + GET_DY_SELECTOR.hex())


def test_decode_get_dy_return_happy_path() -> None:
    assert _decode_get_dy_return(encode(["uint256"], [99_896_882])) == 99_896_882


def test_decode_get_dy_return_short_data_is_none() -> None:
    assert _decode_get_dy_return(b"") is None
    assert _decode_get_dy_return(b"\x00" * 31) is None


@pytest.mark.asyncio
async def test_batched_curve_quote_decodes_amount() -> None:
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    return_data = encode(["uint256"], [99_896_882])

    async def fake_aggregate(_calls: list[Call], block: str = "latest") -> list[CallResult]:
        return [CallResult(success=True, return_data=return_data)]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    path = _path(_TWO_POOL, i=1, j=0)
    quotes = await batched_curve_quote(mc, [path])
    assert len(quotes) == 1
    assert quotes[0].amount_out == 99_896_882
    assert quotes[0].gas_estimate == 0  # get_dy carries no gas figure
    assert quotes[0].path is path


@pytest.mark.asyncio
async def test_batched_curve_quote_revert_returns_zero_and_keeps_alignment() -> None:
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    ok_data = encode(["uint256"], [777])

    async def fake_aggregate(_calls: list[Call], block: str = "latest") -> list[CallResult]:
        return [
            CallResult(success=False, return_data=b""),  # revert
            CallResult(success=True, return_data=ok_data),
        ]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    paths = [_path(_TWO_POOL, i=0, j=1), _path(_NG_POOL, i=0, j=1)]
    quotes = await batched_curve_quote(mc, paths)
    assert len(quotes) == 2
    assert quotes[0].amount_out == 0
    assert quotes[0].path is paths[0]  # preserved index
    assert quotes[1].amount_out == 777
    assert quotes[1].path is paths[1]


@pytest.mark.asyncio
async def test_batched_curve_quote_undecodable_success_is_zero() -> None:
    """success=True but garbage/short return data must yield 0, not raise."""
    rpc = AsyncMock()
    mc = Multicall3(rpc)

    async def fake_aggregate(_calls: list[Call], block: str = "latest") -> list[CallResult]:
        return [CallResult(success=True, return_data=b"\x01\x02")]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    quotes = await batched_curve_quote(mc, [_path(_TWO_POOL, i=0, j=1)])
    assert quotes[0].amount_out == 0


@pytest.mark.asyncio
async def test_batched_curve_quote_chunks() -> None:
    """12 paths at chunk=8 → 2 aggregate calls, not 12."""
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    ok_data = encode(["uint256"], [1])
    chunk_sizes: list[int] = []

    async def fake_aggregate(calls: list[Call], block: str = "latest") -> list[CallResult]:
        chunk_sizes.append(len(calls))
        return [CallResult(success=True, return_data=ok_data) for _ in calls]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    paths = [_path(_TWO_POOL, i=0, j=1, amount_in=k + 1) for k in range(12)]
    quotes = await batched_curve_quote(mc, paths)
    assert chunk_sizes == [8, 4]  # _MAX_CALLS_PER_BATCH=8
    assert len(quotes) == 12


@pytest.mark.asyncio
async def test_batched_curve_quote_drops_chunk_on_non_gas_error() -> None:
    """A non-gas error from one chunk (rate-limit/timeout) drops ONLY that
    chunk's quotes — positionally aligned zeros — not the whole pass."""
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    ok_data = encode(["uint256"], [55])
    seen = {"n": 0}

    async def fake_aggregate_resilient(
        calls: list[Call], block: str = "latest"
    ) -> list[CallResult]:
        seen["n"] += 1
        if seen["n"] == 2:  # second chunk fails with a non-gas error
            raise RuntimeError("RPC error 429: Too Many Requests")
        return [CallResult(success=True, return_data=ok_data) for _ in calls]

    mc.aggregate_resilient = fake_aggregate_resilient  # type: ignore[assignment]

    paths = [_path(_TWO_POOL, i=0, j=1, amount_in=k + 1) for k in range(12)]
    quotes = await batched_curve_quote(mc, paths)
    assert len(quotes) == 12  # nothing lost, positionally aligned
    assert sum(1 for q in quotes if q.amount_out == 55) == 8  # first chunk survived
    assert sum(1 for q in quotes if q.amount_out == 0) == 4  # dropped chunk → 0


@pytest.mark.asyncio
async def test_batched_curve_quote_empty_paths_skips_rpc() -> None:
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    calls_made = 0

    async def fake_aggregate(_calls: list[Call], block: str = "latest") -> list[CallResult]:
        nonlocal calls_made
        calls_made += 1
        return []

    mc.aggregate = fake_aggregate  # type: ignore[assignment]
    assert await batched_curve_quote(mc, []) == []
    assert calls_made == 0


def test_curve_quote_duck_types_selection_surface() -> None:
    """The router's _select_best_quote_per_order reads path.order_uid,
    path.amount_in, path.exact_output and quote.amount_out — pin that the
    Curve types expose exactly those attributes with exact-input semantics."""
    path = _path(_TWO_POOL, i=0, j=1, amount_in=42)
    q = CurveQuote(path=path, amount_out=41)
    assert q.path.order_uid == "o1"
    assert q.path.amount_in == 42
    assert q.path.exact_output is False  # always: get_dy is exact-input only
    assert q.amount_out == 41
    assert q.gas_estimate == 0  # default
