"""Tests for batched V4 quoter (mocked Multicall3)."""
from unittest.mock import AsyncMock

import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from src.routing.multicall import Call, CallResult, Multicall3
from src.routing.v4_quoter import (
    HOOKS_NONE,
    NATIVE_CURRENCY,
    QUOTE_EXACT_INPUT_SINGLE_V4_SELECTOR,
    QUOTE_EXACT_OUTPUT_SINGLE_V4_SELECTOR,
    STANDARD_V4_TIERS,
    WETH_ARBITRUM,
    V4BatchedQuote,
    V4Path,
    _build_call,
    _decode_quote_return,
    _encode_quote_exact_input_single,
    _encode_quote_exact_output_single,
    batched_v4_quote,
    make_v4_native_paths,
    make_v4_paths,
)

# Canonical Arbitrum USDC — a realistic "other side" for native-pool tests.
USDC_ARBITRUM = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"


def test_v4_selectors_match_keccak() -> None:
    """Pin selectors against keccak of the canonical IV4Quoter signatures.
    QuoteExactSingleParams is encoded as ONE tuple parameter wrapping the
    PoolKey struct — a flattened-param signature would produce a different
    selector and every quote would revert on-chain."""
    assert (
        keccak(
            b"quoteExactInputSingle("
            b"((address,address,uint24,int24,address),bool,uint128,bytes))"
        )[:4].hex()
        == QUOTE_EXACT_INPUT_SINGLE_V4_SELECTOR
    )
    assert (
        keccak(
            b"quoteExactOutputSingle("
            b"((address,address,uint24,int24,address),bool,uint128,bytes))"
        )[:4].hex()
        == QUOTE_EXACT_OUTPUT_SINGLE_V4_SELECTOR
    )


def test_currency_sorting_and_zero_for_one() -> None:
    """currency0 is the lower address as an integer; zero_for_one means
    token_in IS currency0 (swap currency0 → currency1)."""
    low = "0x" + "11" * 20
    high = "0x" + "22" * 20

    selling_low = V4Path(
        order_uid="o1", token_in=low, token_out=high, fee=500, tick_spacing=10, amount_in=1
    )
    assert selling_low.currency0 == low
    assert selling_low.currency1 == high
    assert selling_low.zero_for_one is True

    selling_high = V4Path(
        order_uid="o1", token_in=high, token_out=low, fee=500, tick_spacing=10, amount_in=1
    )
    assert selling_high.currency0 == low  # PoolKey ordering unchanged...
    assert selling_high.currency1 == high
    assert selling_high.zero_for_one is False  # ...only the direction flips


def test_zero_for_one_is_case_insensitive() -> None:
    """Addresses compare as integers, not strings — checksummed (mixed-case)
    input must not flip the ordering."""
    low_checksummed = "0x" + "AA" * 20
    high = "0x" + "bb" * 20
    path = V4Path(
        order_uid="o1",
        token_in=low_checksummed,
        token_out=high,
        fee=3000,
        tick_spacing=60,
        amount_in=1,
    )
    assert path.zero_for_one is True
    assert path.currency0 == low_checksummed


def test_encode_quote_exact_input_single_golden() -> None:
    """Round-trip the encoded params through eth_abi.decode and assert every
    QuoteExactSingleParams field, including hooks=zero and empty hookData."""
    token_in = "0x" + "22" * 20  # higher address → NOT currency0
    token_out = "0x" + "11" * 20
    path = V4Path(
        order_uid="o1",
        token_in=token_in,
        token_out=token_out,
        fee=3000,
        tick_spacing=60,
        amount_in=10**18,
    )
    cd = _encode_quote_exact_input_single(path)
    assert cd.startswith("0x" + QUOTE_EXACT_INPUT_SINGLE_V4_SELECTOR)

    args = bytes.fromhex(cd[2 + 8 :])
    ((pool_key, zero_for_one, exact_amount, hook_data),) = decode(
        ["((address,address,uint24,int24,address),bool,uint128,bytes)"], args
    )
    currency0, currency1, fee, tick_spacing, hooks = pool_key
    assert currency0 == "0x" + "11" * 20  # sorted: lower address first
    assert currency1 == "0x" + "22" * 20
    assert fee == 3000
    assert tick_spacing == 60
    assert hooks == HOOKS_NONE  # vanilla pools only
    assert zero_for_one is False  # token_in is currency1 → 1-for-0
    assert exact_amount == 10**18
    assert hook_data == b""


def test_encode_quote_exact_output_single_calldata() -> None:
    """Exact-output uses the SAME params encoding, different selector."""
    path = V4Path(
        order_uid="o1",
        token_in="0x" + "11" * 20,
        token_out="0x" + "22" * 20,
        fee=500,
        tick_spacing=10,
        amount_in=5000,
        exact_output=True,
    )
    cd = _encode_quote_exact_output_single(path)
    assert cd.startswith("0x" + QUOTE_EXACT_OUTPUT_SINGLE_V4_SELECTOR)
    cd_in = _encode_quote_exact_input_single(path)
    assert cd[2 + 8 :] == cd_in[2 + 8 :]  # identical args, only selector differs


def test_build_call_selects_entry_point_by_exact_output() -> None:
    quoter = "0x" + "9" * 40
    base = dict(
        order_uid="o1",
        token_in="0x" + "11" * 20,
        token_out="0x" + "22" * 20,
        fee=100,
        tick_spacing=1,
        amount_in=1,
    )
    sell_call = _build_call(V4Path(**base), quoter)  # type: ignore[arg-type]
    buy_call = _build_call(V4Path(**base, exact_output=True), quoter)  # type: ignore[arg-type]
    assert sell_call.call_data.startswith("0x" + QUOTE_EXACT_INPUT_SINGLE_V4_SELECTOR)
    assert buy_call.call_data.startswith("0x" + QUOTE_EXACT_OUTPUT_SINGLE_V4_SELECTOR)
    assert sell_call.allow_failure is True  # nonexistent pools revert
    assert buy_call.allow_failure is True


def test_decode_quote_return_happy_path() -> None:
    data = encode(["uint256", "uint256"], [123456, 70000])
    assert _decode_quote_return(data) == (123456, 70000)


def test_decode_quote_return_short_data_is_none() -> None:
    assert _decode_quote_return(b"") is None
    assert _decode_quote_return(b"\x00" * 63) is None


def test_make_v4_paths_one_per_standard_tier() -> None:
    paths = make_v4_paths("o1", "0x" + "11" * 20, "0x" + "22" * 20, 10**18)
    assert [(p.fee, p.tick_spacing) for p in paths] == list(STANDARD_V4_TIERS)
    assert [(p.fee, p.tick_spacing) for p in paths] == [
        (100, 1),
        (500, 10),
        (3000, 60),
        (10000, 200),
    ]
    assert all(not p.exact_output for p in paths)

    buy_paths = make_v4_paths("o1", "0x" + "11" * 20, "0x" + "22" * 20, 10**18, exact_output=True)
    assert all(p.exact_output for p in buy_paths)


@pytest.mark.asyncio
async def test_batched_v4_quote_decodes_amount_and_gas() -> None:
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    return_data = encode(["uint256", "uint256"], [123456, 50000])

    async def fake_aggregate(_calls: list[Call], block: str = "latest") -> list[CallResult]:
        return [CallResult(success=True, return_data=return_data)]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    path = V4Path(
        order_uid="o1",
        token_in="0x" + "11" * 20,
        token_out="0x" + "22" * 20,
        fee=500,
        tick_spacing=10,
        amount_in=10**18,
    )
    quotes = await batched_v4_quote(mc, [path])
    assert len(quotes) == 1
    assert quotes[0].amount_out == 123456
    assert quotes[0].gas_estimate == 50000
    assert quotes[0].path is path


@pytest.mark.asyncio
async def test_batched_v4_quote_revert_returns_zero_amount() -> None:
    """Mixed success/revert: reverted paths yield amount_out=0/gas_estimate=0
    and positional alignment with the input list is preserved."""
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    ok_data = encode(["uint256", "uint256"], [777, 60000])

    async def fake_aggregate(_calls: list[Call], block: str = "latest") -> list[CallResult]:
        return [
            CallResult(success=False, return_data=b""),  # revert (pool not found)
            CallResult(success=True, return_data=ok_data),
        ]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    paths = make_v4_paths("o1", "0x" + "11" * 20, "0x" + "22" * 20, 10**18)[:2]
    quotes = await batched_v4_quote(mc, paths)
    assert len(quotes) == 2
    assert quotes[0].amount_out == 0
    assert quotes[0].gas_estimate == 0
    assert quotes[0].path is paths[0]  # preserved index
    assert quotes[1].amount_out == 777
    assert quotes[1].gas_estimate == 60000
    assert quotes[1].path is paths[1]


@pytest.mark.asyncio
async def test_batched_v4_quote_chunks_to_stay_under_gas_cap() -> None:
    """12 paths at chunk=8 → 2 aggregate calls, not 12."""
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    ok_data = encode(["uint256", "uint256"], [1, 1])

    chunk_sizes: list[int] = []

    async def fake_aggregate(calls: list[Call], block: str = "latest") -> list[CallResult]:
        chunk_sizes.append(len(calls))
        return [CallResult(success=True, return_data=ok_data) for _ in calls]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    paths = [
        p
        for i in range(3)
        for p in make_v4_paths(f"o{i}", "0x" + "11" * 20, "0x" + "22" * 20, 10**18)
    ]
    quotes = await batched_v4_quote(mc, paths)
    assert chunk_sizes == [8, 4]  # _MAX_CALLS_PER_BATCH=8
    assert len(quotes) == 12


@pytest.mark.asyncio
async def test_batched_v4_quote_drops_chunk_on_non_gas_error() -> None:
    """A non-gas error from one chunk (rate-limit/timeout under contention)
    must drop ONLY that chunk's quotes — not abort the whole pass. 12 paths
    at chunk=8 → 2 chunks; the 2nd raises 429, its 4 quotes become
    amount_out=0, the first 8 resolve."""
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    ok_data = encode(["uint256", "uint256"], [55, 40000])
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
        p
        for i in range(3)
        for p in make_v4_paths(f"o{i}", "0x" + "11" * 20, "0x" + "22" * 20, 10**18)
    ]  # 12 paths → chunks of 8 + 4
    quotes = await batched_v4_quote(mc, paths)
    assert len(quotes) == 12  # nothing lost, positionally aligned
    assert sum(1 for q in quotes if q.amount_out == 55) == 8  # first chunk survived
    assert sum(1 for q in quotes if q.amount_out == 0) == 4  # dropped chunk → 0


@pytest.mark.asyncio
async def test_batched_v4_quote_bisects_on_out_of_gas() -> None:
    """A chunk that overflows the node gas cap is recursively bisected by
    aggregate_resilient so every quote still resolves."""
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    ok_data = encode(["uint256", "uint256"], [42, 30000])

    async def fake_aggregate(calls: list[Call], block: str = "latest") -> list[CallResult]:
        # Simulate the provider gas cap: any batch > 2 reverts the whole call.
        if len(calls) > 2:
            raise RuntimeError("RPC error -32000: out of gas")
        return [CallResult(success=True, return_data=ok_data) for _ in calls]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    paths = make_v4_paths("o1", "0x" + "11" * 20, "0x" + "22" * 20, 10**18)
    quotes = await batched_v4_quote(mc, paths)
    assert len(quotes) == 4
    assert all(q.amount_out == 42 for q in quotes)  # none lost to the overflow


@pytest.mark.asyncio
async def test_batched_v4_quote_empty_paths_skips_rpc() -> None:
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    calls_made = 0

    async def fake_aggregate(_calls: list[Call], block: str = "latest") -> list[CallResult]:
        nonlocal calls_made
        calls_made += 1
        return []

    mc.aggregate = fake_aggregate  # type: ignore[assignment]
    quotes = await batched_v4_quote(mc, [])
    assert quotes == []
    assert calls_made == 0


def test_v4batched_quote_default_values() -> None:
    path = V4Path(
        order_uid="x", token_in="0xa", token_out="0xb", fee=500, tick_spacing=10, amount_in=1
    )
    q = V4BatchedQuote(path=path, amount_out=0, gas_estimate=0)
    assert q.amount_out == 0
    assert q.gas_estimate == 0
    assert q.path.exact_output is False
    assert q.path.native_side is False  # new field defaults off


def test_make_v4_native_paths_weth_in() -> None:
    """Selling WETH: one native path per standard tier, original addresses kept."""
    paths = make_v4_native_paths("o1", WETH_ARBITRUM, USDC_ARBITRUM, 10**18)
    assert [(p.fee, p.tick_spacing) for p in paths] == list(STANDARD_V4_TIERS)
    assert all(p.native_side for p in paths)
    assert all(not p.exact_output for p in paths)
    # token_in/token_out keep the ORIGINAL WETH address for order mapping...
    assert all(p.token_in == WETH_ARBITRUM for p in paths)
    assert all(p.token_out == USDC_ARBITRUM for p in paths)
    # ...only the derived PoolKey currencies see the substitution.
    assert all(p.currency_in == NATIVE_CURRENCY for p in paths)
    assert all(p.currency_out == USDC_ARBITRUM for p in paths)


def test_make_v4_native_paths_weth_out() -> None:
    """Buying WETH (WETH as token_out) substitutes the OUTPUT side."""
    paths = make_v4_native_paths("o1", USDC_ARBITRUM, WETH_ARBITRUM, 5000, exact_output=True)
    assert len(paths) == len(STANDARD_V4_TIERS)
    assert all(p.native_side for p in paths)
    assert all(p.exact_output for p in paths)
    assert all(p.token_out == WETH_ARBITRUM for p in paths)
    assert all(p.currency_in == USDC_ARBITRUM for p in paths)
    assert all(p.currency_out == NATIVE_CURRENCY for p in paths)


def test_make_v4_native_paths_requires_exactly_one_weth_side() -> None:
    """Non-WETH pair has no native variant; WETH/WETH is nonsensical → []."""
    assert make_v4_native_paths("o1", "0x" + "11" * 20, "0x" + "22" * 20, 1) == []
    assert make_v4_native_paths("o1", WETH_ARBITRUM, WETH_ARBITRUM, 1) == []


def test_make_v4_native_paths_weth_match_is_case_insensitive() -> None:
    """A lowercased (non-checksummed) WETH address must still gate AND
    substitute — addresses compare as integers, not strings."""
    paths = make_v4_native_paths("o1", WETH_ARBITRUM.lower(), USDC_ARBITRUM, 1)
    assert len(paths) == len(STANDARD_V4_TIERS)
    assert all(p.currency_in == NATIVE_CURRENCY for p in paths)


def test_native_encode_golden_weth_in_zero_for_one_true() -> None:
    """Selling WETH→TOKEN via the native pool: the substituted input is the
    zero address, which always sorts first → currency0=native, zeroForOne=True."""
    path = V4Path(
        order_uid="o1",
        token_in=WETH_ARBITRUM,
        token_out=USDC_ARBITRUM,
        fee=500,
        tick_spacing=10,
        amount_in=10**18,
        native_side=True,
    )
    cd = _encode_quote_exact_input_single(path)
    assert cd.startswith("0x" + QUOTE_EXACT_INPUT_SINGLE_V4_SELECTOR)

    args = bytes.fromhex(cd[2 + 8 :])
    ((pool_key, zero_for_one, exact_amount, hook_data),) = decode(
        ["((address,address,uint24,int24,address),bool,uint128,bytes)"], args
    )
    currency0, currency1, fee, tick_spacing, hooks = pool_key
    assert currency0 == NATIVE_CURRENCY  # native side replaces WETH, sorts first
    assert currency1 == USDC_ARBITRUM.lower()  # decode returns lowercase hex
    assert zero_for_one is True  # input IS the native side → 0-for-1
    assert fee == 500
    assert tick_spacing == 10
    assert hooks == HOOKS_NONE  # native pools quoted vanilla-only too
    assert exact_amount == 10**18
    assert hook_data == b""


def test_native_encode_golden_weth_out_zero_for_one_false() -> None:
    """Selling TOKEN→WETH via the native pool: the substituted OUTPUT is the
    zero address → currency0=native is the output side, zeroForOne=False."""
    path = V4Path(
        order_uid="o1",
        token_in=USDC_ARBITRUM,
        token_out=WETH_ARBITRUM,
        fee=3000,
        tick_spacing=60,
        amount_in=5000,
        native_side=True,
    )
    args = bytes.fromhex(_encode_quote_exact_input_single(path)[2 + 8 :])
    ((pool_key, zero_for_one, _exact_amount, _hook_data),) = decode(
        ["((address,address,uint24,int24,address),bool,uint128,bytes)"], args
    )
    currency0, currency1, _fee, _tick_spacing, _hooks = pool_key
    assert currency0 == NATIVE_CURRENCY
    assert currency1 == USDC_ARBITRUM.lower()
    assert zero_for_one is False  # input is the TOKEN (currency1) → 1-for-0


def test_native_side_false_keeps_vanilla_encoding_byte_identical() -> None:
    """Regression pin: the new field must not change non-native encodings —
    a default-constructed path and an explicit native_side=False path encode
    byte-identically, and a WETH-paired path WITHOUT native_side keeps the
    real WETH address in the PoolKey (no accidental substitution)."""
    kwargs = dict(
        order_uid="o1",
        token_in=WETH_ARBITRUM,
        token_out=USDC_ARBITRUM,
        fee=500,
        tick_spacing=10,
        amount_in=10**18,
    )
    default_path = V4Path(**kwargs)  # type: ignore[arg-type]
    explicit_path = V4Path(**kwargs, native_side=False)  # type: ignore[arg-type]
    cd = _encode_quote_exact_input_single(default_path)
    assert cd == _encode_quote_exact_input_single(explicit_path)

    args = bytes.fromhex(cd[2 + 8 :])
    ((pool_key, zero_for_one, _amount, _hd),) = decode(
        ["((address,address,uint24,int24,address),bool,uint128,bytes)"], args
    )
    currency0, currency1, _f, _ts, _h = pool_key
    # WETH (0x82af...) < USDC (0xaf88...) as integers → WETH is currency0.
    assert currency0 == WETH_ARBITRUM.lower()
    assert currency1 == USDC_ARBITRUM.lower()
    assert zero_for_one is True


@pytest.mark.asyncio
async def test_batched_v4_quote_mixed_native_and_vanilla_alignment() -> None:
    """A mixed batch (native + vanilla paths) flows through _build_call with
    no special-casing in batched_v4_quote: per-path calldata reflects each
    path's native_side and results stay positionally aligned."""
    rpc = AsyncMock()
    mc = Multicall3(rpc)
    seen_calls: list[Call] = []

    async def fake_aggregate(calls: list[Call], block: str = "latest") -> list[CallResult]:
        seen_calls.extend(calls)
        return [
            CallResult(success=True, return_data=encode(["uint256", "uint256"], [i + 1, 10]))
            for i in range(len(calls))
        ]

    mc.aggregate = fake_aggregate  # type: ignore[assignment]

    native = V4Path(
        order_uid="o1",
        token_in=WETH_ARBITRUM,
        token_out=USDC_ARBITRUM,
        fee=500,
        tick_spacing=10,
        amount_in=10**18,
        native_side=True,
    )
    vanilla = V4Path(
        order_uid="o2",
        token_in="0x" + "11" * 20,
        token_out="0x" + "22" * 20,
        fee=500,
        tick_spacing=10,
        amount_in=10**18,
    )
    quotes = await batched_v4_quote(mc, [native, vanilla])
    assert [q.path for q in quotes] == [native, vanilla]
    assert [q.amount_out for q in quotes] == [1, 2]

    def decoded_currency0(call: Call) -> str:
        args = bytes.fromhex(call.call_data[2 + 8 :])
        ((pool_key, *_rest),) = decode(
            ["((address,address,uint24,int24,address),bool,uint128,bytes)"], args
        )
        return str(pool_key[0])

    assert decoded_currency0(seen_calls[0]) == NATIVE_CURRENCY  # native path substituted
    assert decoded_currency0(seen_calls[1]) == "0x" + "11" * 20  # vanilla untouched
