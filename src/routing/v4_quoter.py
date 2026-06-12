"""Batched Uniswap V4 quoter: vanilla-pool quotes through the official V4Quoter.

V4 replaces per-pool contracts with a singleton PoolManager; a pool is
identified by its PoolKey struct ``(currency0, currency1, fee, tickSpacing,
hooks)`` with ``currency0 < currency1`` (addresses compared as integers).
We quote ONLY vanilla pools (hooks = zero address) at the four standard
fee/tick-spacing tiers — hooked pools can re-price arbitrarily and are out
of scope for the router.

V4 additionally treats native ETH as a first-class currency (the zero
address). For WETH-paired orders ``make_v4_native_paths`` builds quote-only
candidates against the NATIVE pool variant (PoolKey substitutes the zero
address for the WETH side) — measurement input for the router, no encoder
support yet.

Same shape as ``v3_batched``: every candidate (order, tier) pair is encoded
up front and submitted via chunked ``Multicall3.aggregate_resilient`` calls.
The V4Quoter *simulates* the swap (PoolManager unlock + revert-with-quote),
so the same eth_call gas-cap defences apply: conservative chunks, bisecting
aggregation, and per-chunk drop tolerance on non-gas errors.

Exact-output (buy-order) variants reuse the same ``QuoteExactSingleParams``
encoding — ``zeroForOne`` is always the swap direction and ``exactAmount``
is interpreted per entry point — so only the selector differs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_abi import decode, encode

from src.log import get_logger
from src.routing.multicall import Call, CallResult, Multicall3

log = get_logger(__name__)

# Arbitrum One singleton PoolManager (all V4 pools live in this one contract).
# Source: https://developers.uniswap.org/contracts/v4/deployments
POOL_MANAGER_ARBITRUM = "0x360e68faccca8ca495c1b759fd9eee466db9fb32"
# Arbitrum One official V4Quoter lens (quotes via simulate-and-revert).
# Source: https://developers.uniswap.org/contracts/v4/deployments
V4_QUOTER_ARBITRUM = "0x3972c00f7ed4885e145823eb7c655375d275a1c5"

# IV4Quoter.QuoteExactSingleParams: ((PoolKey poolKey), bool zeroForOne,
# uint128 exactAmount, bytes hookData) where PoolKey =
# (address currency0, address currency1, uint24 fee, int24 tickSpacing, address hooks).
# keccak256 of
# "quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))"
# [:4] = aa9d21cb (verified against v4-periphery IV4Quoter + openchain.xyz)
QUOTE_EXACT_INPUT_SINGLE_V4_SELECTOR = "aa9d21cb"
# keccak256 of
# "quoteExactOutputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))"
# [:4] = 58733073 (verified against v4-periphery IV4Quoter + openchain.xyz)
QUOTE_EXACT_OUTPUT_SINGLE_V4_SELECTOR = "58733073"

# eth_abi type string for QuoteExactSingleParams (struct encoded as one tuple param).
_QUOTE_EXACT_SINGLE_PARAMS_TYPE = "((address,address,uint24,int24,address),bool,uint128,bytes)"

# Vanilla pools only: hooks = zero address.
HOOKS_NONE = "0x" + "00" * 20

# V4 treats native ETH as a first-class currency: the zero address. Most V4
# liquidity on Arbitrum sits in NATIVE pools rather than WETH pools, so for
# WETH-paired orders we additionally quote the native-pool variant.
NATIVE_CURRENCY = "0x0000000000000000000000000000000000000000"
# Canonical Arbitrum One WETH.
# Source: https://arbiscan.io/token/0x82af49447d8a07e3bd95bd0d56f35241523fbab1
WETH_ARBITRUM = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"

# The four standard (fee, tickSpacing) tiers V4 pools are deployed at when
# created without hooks; mirrors V3's FEE_TIERS but tick spacing is part of
# the pool identity in V4 so it must be carried explicitly.
STANDARD_V4_TIERS: tuple[tuple[int, int], ...] = ((100, 1), (500, 10), (3000, 60), (10000, 200))


@dataclass(frozen=True)
class V4Path:
    """One candidate single-hop quote against a vanilla V4 pool tier.

    ``token_in`` / ``token_out`` always refer to the swap direction from the
    user's perspective (sell-token → buy-token); PoolKey currency ordering
    is derived (``currency0 < currency1`` as integers).

    When ``exact_output=False`` (default, sell orders), ``amount_in`` is the
    exact input — passed to ``quoteExactInputSingle``. When
    ``exact_output=True`` (buy orders), ``amount_in`` is the exact OUTPUT —
    passed to ``quoteExactOutputSingle``; the quoter returns ``amountIn``.

    When ``native_side=True`` the path targets the NATIVE-ETH pool variant:
    the PoolKey substitutes ``NATIVE_CURRENCY`` for whichever of
    ``token_in`` / ``token_out`` equals ``WETH_ARBITRUM`` (case-insensitive).
    ``token_in`` / ``token_out`` keep the ORIGINAL (WETH) addresses so the
    router can map quotes back to the order; only the derived PoolKey
    currencies and ``zero_for_one`` see the substitution. The zero address
    compares lower than any token, so the native side is always currency0.
    """

    order_uid: str
    token_in: str
    token_out: str
    fee: int
    tick_spacing: int
    amount_in: int
    exact_output: bool = False
    native_side: bool = False

    def _currency(self, token: str) -> str:
        """``token`` as the PoolKey sees it: NATIVE_CURRENCY in place of WETH
        when this is a native-pool path, the token itself otherwise."""
        if self.native_side and _strip_0x(token).lower() == _strip_0x(WETH_ARBITRUM).lower():
            return NATIVE_CURRENCY
        return token

    @property
    def currency_in(self) -> str:
        """The PoolKey currency on the input side (post native substitution)."""
        return self._currency(self.token_in)

    @property
    def currency_out(self) -> str:
        """The PoolKey currency on the output side (post native substitution)."""
        return self._currency(self.token_out)

    @property
    def currency0(self) -> str:
        """The lower of the two pool currencies (PoolKey.currency0)."""
        return min(self.currency_in, self.currency_out, key=lambda a: int(_strip_0x(a), 16))

    @property
    def currency1(self) -> str:
        """The higher of the two pool currencies (PoolKey.currency1)."""
        return max(self.currency_in, self.currency_out, key=lambda a: int(_strip_0x(a), 16))

    @property
    def zero_for_one(self) -> bool:
        """True when ``currency_in`` is currency0, i.e. the swap goes 0 → 1."""
        return int(_strip_0x(self.currency_in), 16) < int(_strip_0x(self.currency_out), 16)


@dataclass(frozen=True)
class V4BatchedQuote:
    path: V4Path
    # The quoter's variable-side amount: ``amountOut`` for exact-input paths
    # and ``amountIn`` for exact-output paths (mirrors V3BatchedQuote
    # semantics). 0 on revert / pool-not-found.
    amount_out: int
    gas_estimate: int


def _strip_0x(addr: str) -> str:
    return addr[2:] if addr.startswith("0x") or addr.startswith("0X") else addr


def _encode_quote_params(path: V4Path) -> bytes:
    """ABI-encode QuoteExactSingleParams for ``path`` (vanilla pool, no hookData).

    ``currency0`` / ``currency1`` / ``zero_for_one`` are derived properties,
    so native-pool paths (``native_side=True``) get the WETH → NATIVE_CURRENCY
    substitution here without any extra branching.
    """
    pool_key = (path.currency0, path.currency1, path.fee, path.tick_spacing, HOOKS_NONE)
    return encode(
        [_QUOTE_EXACT_SINGLE_PARAMS_TYPE],
        [(pool_key, path.zero_for_one, path.amount_in, b"")],
    )


def _encode_quote_exact_input_single(path: V4Path) -> str:
    """Encode quoteExactInputSingle(QuoteExactSingleParams) calldata. Selector aa9d21cb."""
    return "0x" + QUOTE_EXACT_INPUT_SINGLE_V4_SELECTOR + _encode_quote_params(path).hex()


def _encode_quote_exact_output_single(path: V4Path) -> str:
    """Encode quoteExactOutputSingle(QuoteExactSingleParams) calldata. Selector 58733073.

    Param layout is identical to the exact-input variant — ``zeroForOne`` is
    still the swap direction; only ``exactAmount`` is reinterpreted as the
    desired OUTPUT and the call returns ``(amountIn, gasEstimate)``.
    """
    return "0x" + QUOTE_EXACT_OUTPUT_SINGLE_V4_SELECTOR + _encode_quote_params(path).hex()


def _decode_quote_return(data: bytes) -> tuple[int, int] | None:
    """Decode ``(uint256 amount, uint256 gasEstimate)``, else None.

    Same return shape for both entry points: ``amount`` is ``amountOut`` for
    exact-input and ``amountIn`` for exact-output — caller distinguishes via
    ``exact_output``.
    """
    # 2 static-typed 32-byte fields = 64 bytes minimum.
    if len(data) < 64:
        return None
    try:
        amount, gas_estimate = decode(["uint256", "uint256"], data)
    except Exception as e:  # noqa: BLE001
        log.debug("v4_quote_decode_failed", error=str(e), nbytes=len(data))
        return None
    return int(amount), int(gas_estimate)


def _build_call(path: V4Path, quoter_address: str) -> Call:
    if path.exact_output:
        call_data = _encode_quote_exact_output_single(path)
    else:
        call_data = _encode_quote_exact_input_single(path)
    # allow_failure: pools that don't exist revert; we want amount_out=0 not a crash.
    return Call(target=quoter_address, call_data=call_data, allow_failure=True)


# The V4Quoter, like QuoterV2, SIMULATES the swap (unlock + revert-with-quote),
# so a quote over a deep/volatile tick range can cost far more gas than nominal
# and a batch can blow the provider's eth_call gas cap. Same two defences as
# v3_batched: a conservative initial chunk, plus ``aggregate_resilient``
# bisecting any chunk that still overflows down to single calls.
_MAX_CALLS_PER_BATCH = 8


def make_v4_paths(
    order_uid: str,
    token_in: str,
    token_out: str,
    amount_in: int,
    *,
    exact_output: bool = False,
) -> list[V4Path]:
    """One candidate V4Path per standard vanilla tier for a single hop."""
    return [
        V4Path(
            order_uid=order_uid,
            token_in=token_in,
            token_out=token_out,
            fee=fee,
            tick_spacing=tick_spacing,
            amount_in=amount_in,
            exact_output=exact_output,
        )
        for fee, tick_spacing in STANDARD_V4_TIERS
    ]


def make_v4_native_paths(
    order_uid: str,
    token_in: str,
    token_out: str,
    amount_in: int,
    *,
    weth: str = WETH_ARBITRUM,
    exact_output: bool = False,
) -> list[V4Path]:
    """Native-pool variants for a WETH-paired hop: one V4Path per standard tier.

    Returns ``[]`` unless EXACTLY one of ``token_in`` / ``token_out`` equals
    ``weth`` (case-insensitive) — a non-WETH pair has no native variant, and
    WETH/WETH is nonsensical. The returned paths keep the original token
    addresses and carry ``native_side=True``; the WETH → NATIVE_CURRENCY
    substitution happens at encode time (note: against ``WETH_ARBITRUM``, so
    overriding ``weth`` only changes the gate, not the substitution).
    """
    in_is_weth = _strip_0x(token_in).lower() == _strip_0x(weth).lower()
    out_is_weth = _strip_0x(token_out).lower() == _strip_0x(weth).lower()
    if in_is_weth == out_is_weth:  # neither side or both sides — no native variant
        return []
    return [
        V4Path(
            order_uid=order_uid,
            token_in=token_in,
            token_out=token_out,
            fee=fee,
            tick_spacing=tick_spacing,
            amount_in=amount_in,
            exact_output=exact_output,
            native_side=True,
        )
        for fee, tick_spacing in STANDARD_V4_TIERS
    ]


async def batched_v4_quote(
    multicall: Multicall3,
    paths: list[V4Path],
    *,
    quoter_address: str = V4_QUOTER_ARBITRUM,
    max_calls_per_batch: int = _MAX_CALLS_PER_BATCH,
) -> list[V4BatchedQuote]:
    """Submit paths in batched Multicall3.aggregate() calls and decode.

    Returns one V4BatchedQuote per input V4Path, in the same order. Reverted
    calls (pool not found, etc.) yield amount_out=0 / gas_estimate=0 — the
    result list always matches the input list length so callers can
    correlate positionally.

    Paths are chunked into batches of ``max_calls_per_batch`` to stay under
    provider-side eth_call gas caps.
    """
    if not paths:
        return []

    calls = [_build_call(p, quoter_address) for p in paths]
    results: list[Any] = []
    for i in range(0, len(calls), max_calls_per_batch):
        chunk = calls[i : i + max_calls_per_batch]
        try:
            results.extend(await multicall.aggregate_resilient(chunk))
        except Exception as exc:  # noqa: BLE001
            # aggregate_resilient swallows gas-overflow but RE-RAISES other
            # errors (rate-limit / timeout under RPC contention). Drop only
            # THIS chunk's quotes — positionally aligned (success=False →
            # amount_out 0) — rather than aborting the whole pass, so the
            # router still produces a partial solution from the chunks that
            # succeeded.
            log.warning(
                "v4_chunk_dropped", error=str(exc), chunk_start=i, chunk_len=len(chunk)
            )
            results.extend(CallResult(success=False, return_data=b"") for _ in chunk)

    quotes: list[V4BatchedQuote] = []
    for path, result in zip(paths, results, strict=True):
        if not result.success:
            quotes.append(V4BatchedQuote(path=path, amount_out=0, gas_estimate=0))
            continue
        decoded = _decode_quote_return(result.return_data)
        if decoded is None:
            quotes.append(V4BatchedQuote(path=path, amount_out=0, gas_estimate=0))
            continue
        amount, gas_estimate = decoded
        quotes.append(V4BatchedQuote(path=path, amount_out=amount, gas_estimate=gas_estimate))
    return quotes
