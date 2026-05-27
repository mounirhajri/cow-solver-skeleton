"""UniswapV2-style Router calldata builders.

Targets the canonical UniswapV2Router02 ABI, which Arbitrum DEXes Camelot,
Ramses, and SushiSwap all implement (or extend) verbatim. The shared shape
is what makes V2 routers cheap to support across multiple venues — we keep
one encoder and parametrise the router address per source.

Differences from V3 worth keeping in mind while reading this file:

  - **No fee tier param.** V2 has a single fee per pool baked into the pool
    contract (UniV2: 0.3 %, Sushi: 0.3 %, Camelot volatile pools: variable
    but routed by the pool itself). The router doesn't pick a tier.

  - **Path is ``address[]``**, not packed bytes. Standard ABI dynamic-array
    encoding — head pointer + length + addresses left-aligned in 32-byte
    slots. ``eth_abi`` handles the layout; we just pass a Python list.

  - **Path must start with ``token_in`` and end with ``token_out``**.
    For a direct A→B swap, ``path = [A, B]``. For A→WETH→B,
    ``path = [A, WETH, B]``. Multi-hop has no separate function name like
    V3's ``exactInput`` — same function, longer path.

  - **No ``sqrtPriceLimitX96``**. V2 reverts cleanly on insufficient
    liquidity; no partial-fill class of risk to defend against.

Fee-on-transfer tokens use a separate function pair
(``swapExactTokensForTokensSupportingFeeOnTransferTokens`` etc). Not in
this module — Phase 0b excludes FoT routing; ``TokenFeatures.has_transfer_tax``
must be checked upstream to skip those orders entirely. Encoding them with
the regular function reverts because UniV2 enforces post-swap balance
checks that fail for FoT pairs.

Camelot's volatile pools work with this ABI directly. Their stable-pool
swap path uses an extended router function with a ``referrer`` argument
and is out of scope until we know it actually wins surplus on Arbitrum.
"""

from __future__ import annotations

from eth_abi import encode
from eth_utils import keccak

_SIG_SWAP_EXACT_TOKENS_FOR_TOKENS = (
    "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)"
)
_SIG_SWAP_TOKENS_FOR_EXACT_TOKENS = (
    "swapTokensForExactTokens(uint256,uint256,address[],address,uint256)"
)

SWAP_EXACT_TOKENS_FOR_TOKENS_SELECTOR = keccak(text=_SIG_SWAP_EXACT_TOKENS_FOR_TOKENS)[:4]
SWAP_TOKENS_FOR_EXACT_TOKENS_SELECTOR = keccak(text=_SIG_SWAP_TOKENS_FOR_EXACT_TOKENS)[:4]

_ARGS_TYPES = ["uint256", "uint256", "address[]", "address", "uint256"]


def _validate_path(path: list[str], expected_in: str, expected_out: str) -> None:
    """Reject paths that won't survive the V2 router's invariants."""
    if len(path) < 2:
        raise ValueError(f"path must have at least 2 tokens, got {len(path)}")
    for i, addr in enumerate(path):
        if not (addr.startswith("0x") and len(addr) == 42):
            raise ValueError(f"path[{i}] is not a 0x-prefixed 20-byte address: {addr!r}")
    if path[0].lower() != expected_in.lower():
        raise ValueError(
            f"path[0] must equal token_in ({expected_in}), got {path[0]}"
        )
    if path[-1].lower() != expected_out.lower():
        raise ValueError(
            f"path[-1] must equal token_out ({expected_out}), got {path[-1]}"
        )


def encode_swap_exact_tokens_for_tokens(
    token_in: str,
    token_out: str,
    path: list[str],
    recipient: str,
    deadline: int,
    amount_in: int,
    amount_out_minimum: int,
) -> bytes:
    """Encode UniV2 ``swapExactTokensForTokens(...)`` calldata.

    For **sell-kind** orders. ``amount_in`` is fixed; the swap reverts if
    realised output falls below ``amount_out_minimum``.

    ``token_in`` and ``token_out`` are kept as explicit args (not just
    read from path) so the validator can confirm intent — encoding a path
    that doesn't match the swap is a class of bug that produces silent
    revert later instead of clear failure at call time.
    """
    _validate_path(path, expected_in=token_in, expected_out=token_out)
    args = encode(
        _ARGS_TYPES,
        [amount_in, amount_out_minimum, path, recipient, deadline],
    )
    return SWAP_EXACT_TOKENS_FOR_TOKENS_SELECTOR + args


def encode_swap_tokens_for_exact_tokens(
    token_in: str,
    token_out: str,
    path: list[str],
    recipient: str,
    deadline: int,
    amount_out: int,
    amount_in_maximum: int,
) -> bytes:
    """Encode UniV2 ``swapTokensForExactTokens(...)`` calldata.

    For **buy-kind** orders. ``amount_out`` is fixed (user gets exactly
    this); the swap reverts if it would consume more than
    ``amount_in_maximum`` of the sell token.
    """
    _validate_path(path, expected_in=token_in, expected_out=token_out)
    args = encode(
        _ARGS_TYPES,
        [amount_out, amount_in_maximum, path, recipient, deadline],
    )
    return SWAP_TOKENS_FOR_EXACT_TOKENS_SELECTOR + args
