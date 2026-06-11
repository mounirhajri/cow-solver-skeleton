"""Tests for batched V3 quoter (mocked Multicall3)."""
from unittest.mock import AsyncMock

import pytest
from eth_abi import encode
from eth_utils import keccak

from src.routing.multicall import Call, CallResult, Multicall3
from src.routing.v3_batched import (
    QUOTE_EXACT_INPUT_SELECTOR,
    QUOTE_EXACT_OUTPUT_SELECTOR,
    QUOTE_EXACT_OUTPUT_SINGLE_SELECTOR,
    V3BatchedQuote,
    V3Path,
    _build_call,
    _encode_path_bytes,
    _encode_quote_exact_input,
    _encode_quote_exact_output,
    _encode_quote_exact_output_single,
    batched_v3_quote,
)


def test_quote_exact_input_selector_matches_keccak() -> None:
    expected = keccak(b"quoteExactInput(bytes,uint256)")[:4].hex()
    assert expected == QUOTE_EXACT_INPUT_SELECTOR


def test_quote_exact_output_selectors_match_keccak() -> None:
    """Selectors for QuoterV2's exact-output entry points. Mismatched
    selectors would silently route to the wrong function on-chain and
    revert — pin them against keccak to catch typos at unit-test time."""
    assert (
        keccak(b"quoteExactOutput(bytes,uint256)")[:4].hex()
        == QUOTE_EXACT_OUTPUT_SELECTOR
    )
    assert (
        keccak(
            b"quoteExactOutputSingle((address,address,uint256,uint24,uint160))"
        )[:4].hex()
        == QUOTE_EXACT_OUTPUT_SINGLE_SELECTOR
    )


def test_encode_quote_exact_output_single_calldata_prefix() -> None:
    cd = _encode_quote_exact_output_single("0x" + "11" * 20, "0x" + "22" * 20, 500, 3000)
    assert cd.startswith("0x" + QUOTE_EXACT_OUTPUT_SINGLE_SELECTOR)


def test_encode_quote_exact_output_calldata_prefix() -> None:
    path_bytes = bytes.fromhex("22" * 20 + "000bb8" + "11" * 20)  # reversed!
    cd = _encode_quote_exact_output(path_bytes, 500)
    assert cd.startswith("0x" + QUOTE_EXACT_OUTPUT_SELECTOR)


def test_build_call_exact_output_multihop_reverses_path() -> None:
    """For exact-output multi-hop, path bytes must be encoded
    tokenOut → fee_BC → intermediate → fee_AB → tokenIn (reverse of input).
    Verify by comparing calldata to a path-bytes call we encode manually."""
    token_in = "0x" + "11" * 20    # A (sell)
    intermediate = "0x" + "22" * 20  # B
    token_out = "0x" + "33" * 20   # C (buy)
    fee_ab = 500
    fee_bc = 3000
    amount_out = 7777

    path = V3Path(
        order_uid="o1",
        token_in=token_in,
        token_out=token_out,
        amount_in=amount_out,
        fee_tier_in=fee_ab,
        intermediate=intermediate,
        fee_tier_out=fee_bc,
        exact_output=True,
    )
    call = _build_call(path, "0x" + "9" * 40)
    # Build the expected reversed-path calldata directly.
    expected_path_bytes = _encode_path_bytes(
        token_out, fee_bc, intermediate, fee_ab, token_in
    )
    expected = _encode_quote_exact_output(expected_path_bytes, amount_out)
    assert call.call_data == expected


def test_encode_quote_exact_input_path() -> None:
    """WETH | 3000 | USDC path bytes: 20 + 3 + 20 = 43 bytes; fee encoded big-endian."""
    weth = "0x" + "11" * 20
    usdc = "0x" + "22" * 20
    path_bytes = _encode_path_bytes(weth, 3000, None, None, usdc)
    assert len(path_bytes) == 43
    assert path_bytes[:20] == bytes.fromhex("11" * 20)
    # 3000 = 0x0bb8 → big-endian 3-byte: 00 0b b8
    assert path_bytes[20:23] == b"\x00\x0b\xb8"
    assert path_bytes[23:43] == bytes.fromhex("22" * 20)


def test_encode_path_bytes_multi_hop_length() -> None:
    """2-hop: 20 + 3 + 20 + 3 + 20 = 66 bytes."""
    a = "0x" + "aa" * 20
    b = "0x" + "bb" * 20
    c = "0x" + "cc" * 20
    path_bytes = _encode_path_bytes(a, 500, b, 3000, c)
    assert len(path_bytes) == 66
    assert path_bytes[20:23] == (500).to_bytes(3, "big")
    assert path_bytes[43:46] == (3000).to_bytes(3, "big")


def test_encode_quote_exact_input_calldata_prefix() -> None:
    path_bytes = bytes.fromhex("11" * 20 + "000bb8" + "22" * 20)
    calldata = _encode_quote_exact_input(path_bytes, 10**18)
    assert calldata.startswith("0x" + QUOTE_EXACT_INPUT_SELECTOR)


@pytest.mark.asyncio
async def test_batched_v3_quote_single_hop_decode() -> None:
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    return_data = encode(
        ["uint256", "uint160", "uint32", "uint256"],
        [123456, 0, 0, 50000],
    )

    async def fake_aggregate(_calls: list[Call], block: str = "latest") -> list[CallResult]:
        return [CallResult(success=True, return_data=return_data)]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    path = V3Path(
        order_uid="o1",
        token_in="0x" + "11" * 20,
        token_out="0x" + "22" * 20,
        amount_in=10**18,
        fee_tier_in=500,
    )
    quotes = await batched_v3_quote(mc, [path])
    assert len(quotes) == 1
    assert quotes[0].amount_out == 123456
    assert quotes[0].path is path


@pytest.mark.asyncio
async def test_batched_v3_quote_multi_hop_decode() -> None:
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    # quoteExactInput returns (uint256, uint160[], uint32[], uint256)
    return_data = encode(
        ["uint256", "uint160[]", "uint32[]", "uint256"],
        [999999, [1, 2], [3, 4], 80000],
    )

    async def fake_aggregate(_calls: list[Call], block: str = "latest") -> list[CallResult]:
        return [CallResult(success=True, return_data=return_data)]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    path = V3Path(
        order_uid="o2",
        token_in="0x" + "11" * 20,
        token_out="0x" + "33" * 20,
        amount_in=10**18,
        fee_tier_in=500,
        intermediate="0x" + "22" * 20,
        fee_tier_out=3000,
    )
    quotes = await batched_v3_quote(mc, [path])
    assert len(quotes) == 1
    assert quotes[0].amount_out == 999999


@pytest.mark.asyncio
async def test_batched_v3_quote_revert_returns_zero_amount() -> None:
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    ok_data = encode(
        ["uint256", "uint160", "uint32", "uint256"],
        [777, 0, 0, 0],
    )

    async def fake_aggregate(_calls: list[Call], block: str = "latest") -> list[CallResult]:
        return [
            CallResult(success=False, return_data=b""),  # revert
            CallResult(success=True, return_data=ok_data),
        ]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    paths = [
        V3Path(
            order_uid="o1",
            token_in="0x" + "11" * 20,
            token_out="0x" + "22" * 20,
            amount_in=10**18,
            fee_tier_in=100,
        ),
        V3Path(
            order_uid="o1",
            token_in="0x" + "11" * 20,
            token_out="0x" + "22" * 20,
            amount_in=10**18,
            fee_tier_in=500,
        ),
    ]
    quotes = await batched_v3_quote(mc, paths)
    assert len(quotes) == 2
    assert quotes[0].amount_out == 0
    assert quotes[0].path is paths[0]  # preserved index
    assert quotes[1].amount_out == 777


@pytest.mark.asyncio
async def test_batched_v3_quote_chunks_to_stay_under_gas_cap() -> None:
    """Paths are batched (few round-trips) but chunked to stay under the node
    eth_call gas cap — 12 paths at chunk=8 → 2 aggregate calls, not 12."""
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    ok_data = encode(["uint256", "uint160", "uint32", "uint256"], [1, 0, 0, 0])

    chunk_sizes: list[int] = []

    async def fake_aggregate(calls: list[Call], block: str = "latest") -> list[CallResult]:
        chunk_sizes.append(len(calls))
        return [CallResult(success=True, return_data=ok_data) for _ in calls]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    paths = [
        V3Path(
            order_uid=f"o{i}",
            token_in="0x" + "11" * 20,
            token_out="0x" + "22" * 20,
            amount_in=10**18,
            fee_tier_in=fee,
        )
        for i in range(3)
        for fee in (100, 500, 3000, 10000)
    ]
    quotes = await batched_v3_quote(mc, paths)
    assert chunk_sizes == [8, 4]  # _MAX_CALLS_PER_BATCH=8
    assert len(quotes) == 12


@pytest.mark.asyncio
async def test_batched_v3_quote_bisects_on_out_of_gas() -> None:
    """A chunk that overflows the node gas cap is recursively bisected so the
    whole quote pass doesn't abort — every quote still resolves once the
    sub-batch is small enough to fit."""
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    ok_data = encode(["uint256", "uint160", "uint32", "uint256"], [42, 0, 0, 0])

    async def fake_aggregate(calls: list[Call], block: str = "latest") -> list[CallResult]:
        # Simulate the provider gas cap: any batch > 2 reverts the whole call.
        if len(calls) > 2:
            raise RuntimeError("RPC error -32000: out of gas")
        return [CallResult(success=True, return_data=ok_data) for _ in calls]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    paths = [
        V3Path(
            order_uid=f"o{i}",
            token_in="0x" + "11" * 20,
            token_out="0x" + "22" * 20,
            amount_in=10**18,
            fee_tier_in=500,
        )
        for i in range(5)
    ]
    quotes = await batched_v3_quote(mc, paths)
    assert len(quotes) == 5
    assert all(q.amount_out == 42 for q in quotes)  # none lost to the overflow


@pytest.mark.asyncio
async def test_batched_v3_quote_drops_only_the_overcap_quote() -> None:
    """A single quote that overflows even alone is dropped (amount_out 0)
    rather than aborting the batch; its siblings still resolve."""
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    ok_data = encode(["uint256", "uint160", "uint32", "uint256"], [99, 0, 0, 0])
    poison_uid = "o-poison"

    async def fake_aggregate(calls: list[Call], block: str = "latest") -> list[CallResult]:
        # The poison path overflows even as a singleton; mixing it into any
        # multi-call batch overflows the whole batch.
        if len(calls) > 1:
            raise RuntimeError("RPC error -32000: out of gas")
        # Singleton: the poison call still overflows; others succeed.
        if calls[0].call_data == _poison_calldata:
            raise RuntimeError("RPC error -32000: out of gas")
        return [CallResult(success=True, return_data=ok_data)]

    paths = [
        V3Path(order_uid="o0", token_in="0x" + "11" * 20, token_out="0x" + "22" * 20,
               amount_in=10**18, fee_tier_in=500),
        V3Path(order_uid=poison_uid, token_in="0x" + "33" * 20, token_out="0x" + "44" * 20,
               amount_in=10**18, fee_tier_in=500),
        V3Path(order_uid="o2", token_in="0x" + "55" * 20, token_out="0x" + "66" * 20,
               amount_in=10**18, fee_tier_in=500),
    ]
    _poison_calldata = _build_call(paths[1], "0x" + "9" * 40).call_data
    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    quotes = await batched_v3_quote(mc, paths, quoter_address="0x" + "9" * 40)
    assert len(quotes) == 3
    by_uid = {q.path.order_uid: q.amount_out for q in quotes}
    assert by_uid["o0"] == 99
    assert by_uid["o2"] == 99
    assert by_uid[poison_uid] == 0  # dropped, not crashed


@pytest.mark.asyncio
async def test_batched_v3_quote_drops_chunk_on_non_gas_error() -> None:
    """A non-gas error from one chunk (rate-limit/timeout under contention) must
    drop ONLY that chunk's quotes — not abort the whole pass — so the router
    still gets the surviving chunks. 12 paths at chunk=8 → 2 chunks; the 2nd
    raises 429, its 4 quotes become amount_out=0, the first 8 resolve."""
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    ok_data = encode(["uint256", "uint160", "uint32", "uint256"], [55, 0, 0, 0])
    seen = {"n": 0}

    async def fake_aggregate_resilient(
        calls: list[Call], block: str = "latest"
    ) -> list[CallResult]:
        seen["n"] += 1
        if seen["n"] == 2:  # second chunk fails with a non-gas error
            raise RuntimeError("RPC error 429: Too Many Requests")
        return [CallResult(success=True, return_data=ok_data) for _ in calls]

    mc.aggregate_resilient = fake_aggregate_resilient  # type: ignore[assignment]

    paths = [
        V3Path(order_uid=f"o{i}", token_in="0x" + "11" * 20, token_out="0x" + "22" * 20,
               amount_in=10**18, fee_tier_in=fee)
        for i in range(3) for fee in (100, 500, 3000, 10000)
    ]  # 12 paths → chunks of 8 + 4
    quotes = await batched_v3_quote(mc, paths)
    assert len(quotes) == 12  # nothing lost, positionally aligned
    assert sum(1 for q in quotes if q.amount_out == 55) == 8   # first chunk survived
    assert sum(1 for q in quotes if q.amount_out == 0) == 4    # dropped chunk → 0


@pytest.mark.asyncio
async def test_batched_v3_quote_empty_paths_skips_rpc() -> None:
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    calls_made = 0

    async def fake_aggregate(_calls: list[Call], block: str = "latest") -> list[CallResult]:
        nonlocal calls_made
        calls_made += 1
        return []

    mc.aggregate = fake_aggregate  # type: ignore[assignment]
    quotes = await batched_v3_quote(mc, [])
    assert quotes == []
    assert calls_made == 0


def test_v3batched_quote_default_values() -> None:
    path = V3Path(
        order_uid="x", token_in="0xa", token_out="0xb", amount_in=1, fee_tier_in=500
    )
    q = V3BatchedQuote(path=path, amount_out=0)
    assert q.amount_out == 0
    assert q.path.intermediate is None
    assert q.path.fee_tier_out is None
