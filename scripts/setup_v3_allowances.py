"""Diagnose-and-emit script for GPv2 Settlement → V3 Router allowances.

The CoW Settlement contract holds user tokens during ``settle()``. Before
the V3 router can pull them for a swap, Settlement must have approved
the router as a spender. We use **permanent (MAX_UINT) approvals** —
industry standard for solvers — which means each (token, router) pair
needs a one-time on-chain ``approve`` from the Settlement contract.

This script does NOT broadcast. It queries current allowances via RPC,
identifies pairs that are missing or below the threshold, and prints the
exact calldata that needs to be sent. Broadcasting is delegated to your
deployment pipeline (GitHub Actions, cast, foundry, etc.) so the script
never touches a private key.

Example::

    python -m scripts.setup_v3_allowances

    Checking 4 tokens against V3 router 0xE592427A...
      ✓ USDC      already approved (allowance = MAX)
      ✗ USDT      needs approve — call from GPv2 Settlement:
          target:   0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9
          calldata: 0x095ea7b3000000000000000000000000E592427A0AE...

Then use your usual deployment path to broadcast each approve() from
the Settlement contract owner (or executor) account. USDT specifically
needs ``approve(0)`` before approving non-zero — the script flags this.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from eth_abi import decode, encode
from eth_utils import keccak

from src.config import settings
from src.routing.multicall import Call, Multicall3
from src.routing.rpc import RpcClient

# Default token set: the canonical Arbitrum stablecoins + WETH/WBTC + ARB.
# Curated to cover ~80% of CoW Arbitrum volume; extend via --tokens for
# long-tail support. Lowercase to dodge any case-sensitivity downstream.
_DEFAULT_TOKENS = [
    ("WETH", "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"),
    ("USDC", "0xaf88d065e77c8cc2239327c5edb3a432268e5831"),
    ("USDT", "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"),
    ("DAI",  "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1"),
    ("WBTC", "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f"),
    ("ARB",  "0x912ce59144191c1204e64559fe8253a0e49e6548"),
]

# Threshold below which we treat the allowance as "needs refresh". An
# unbounded MAX_UINT approval will sit well above 2^200 — any value
# meaningfully smaller probably came from a per-tx approve that's about
# to run out.
_ALLOWANCE_THRESHOLD = 2**200

# ERC20 selectors. ``approve`` for the calldata we emit, ``allowance``
# for the read we batch.
_SEL_APPROVE = keccak(text="approve(address,uint256)")[:4]
_SEL_ALLOWANCE = keccak(text="allowance(address,address)")[:4]

_MAX_UINT256 = 2**256 - 1


@dataclass(frozen=True)
class AllowanceCheck:
    label: str
    token: str
    current: int
    needs_approve: bool
    # USDT and a handful of forks revert if you approve a non-zero value
    # over a non-zero existing allowance. We surface this so the broadcaster
    # can sequence approve(0) first.
    needs_zero_reset: bool


def _encode_allowance_call(owner: str, spender: str) -> str:
    args = encode(["address", "address"], [owner, spender])
    return "0x" + _SEL_ALLOWANCE.hex() + args.hex()


def _encode_approve_calldata(spender: str, amount: int) -> bytes:
    args = encode(["address", "uint256"], [spender, amount])
    return _SEL_APPROVE + args


async def _check_allowances(
    rpc_url: str, owner: str, spender: str, tokens: list[tuple[str, str]]
) -> list[AllowanceCheck]:
    """Batch-read ``allowance(owner, spender)`` for each token in one Multicall3."""
    rpc = RpcClient(rpc_url)
    multicall = Multicall3(rpc)
    calls = [
        Call(target=addr, call_data=_encode_allowance_call(owner, spender))
        for _label, addr in tokens
    ]
    results = await multicall.aggregate(calls)

    out: list[AllowanceCheck] = []
    for (label, token), result in zip(tokens, results, strict=True):
        if not result.success or len(result.return_data) < 32:
            # Treat read failure as "needs approve" — safer to attempt the
            # approval than to assume the token is already covered. Real
            # cause is usually a non-existent contract at the address,
            # which the broadcasting layer will catch on the actual send.
            out.append(
                AllowanceCheck(
                    label=label,
                    token=token,
                    current=0,
                    needs_approve=True,
                    needs_zero_reset=False,
                )
            )
            continue
        (current,) = decode(["uint256"], result.return_data)
        needs_approve = current < _ALLOWANCE_THRESHOLD
        # USDT-style reset needed when we'd be writing a non-zero on top of
        # a non-zero existing value. The "is USDT" heuristic is brittle —
        # we apply the reset rule whenever current is between 1 and the
        # threshold, which covers USDT plus any other token with the
        # same approval semantics.
        needs_zero_reset = needs_approve and current > 0
        out.append(
            AllowanceCheck(
                label=label,
                token=token,
                current=int(current),
                needs_approve=needs_approve,
                needs_zero_reset=needs_zero_reset,
            )
        )
    return out


def _format_pending(check: AllowanceCheck, spender: str) -> str:
    """Render the calldata + context an executor needs to broadcast."""
    lines = [
        f"  ✗ {check.label:<6} needs approve — call from GPv2 Settlement:",
        f"      target:   {check.token}",
    ]
    if check.needs_zero_reset:
        zero_cd = _encode_approve_calldata(spender, 0)
        lines.append(
            f"      zero-reset (USDT-style; broadcast FIRST):"
        )
        lines.append(f"        calldata: 0x{zero_cd.hex()}")
    cd = _encode_approve_calldata(spender, _MAX_UINT256)
    lines.append(f"      max-approve calldata: 0x{cd.hex()}")
    return "\n".join(lines)


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rpc-url",
        default=settings.rpc_arbitrum,
        help="Arbitrum RPC endpoint (default: settings.rpc_arbitrum)",
    )
    parser.add_argument(
        "--owner",
        default=settings.gpv2_settlement,
        help="Address holding allowances (default: GPv2 Settlement)",
    )
    parser.add_argument(
        "--spender",
        default=settings.v3_swap_router,
        help="Address being approved as spender (default: V3 SwapRouter)",
    )
    args = parser.parse_args()

    print(f"Checking {len(_DEFAULT_TOKENS)} tokens against {args.spender}")
    print(f"  owner   = {args.owner}")
    print(f"  spender = {args.spender}")
    print()

    checks = await _check_allowances(
        args.rpc_url, args.owner, args.spender, _DEFAULT_TOKENS
    )

    pending = [c for c in checks if c.needs_approve]
    for check in checks:
        if not check.needs_approve:
            print(f"  ✓ {check.label:<6} already approved (allowance ≈ MAX)")

    if not pending:
        print("\nNothing to do — all tokens already approved.")
        return 0

    print()
    for check in pending:
        print(_format_pending(check, args.spender))
        print()

    print(
        f"{len(pending)} approvals pending — broadcast each `target.callData` "
        f"from {args.owner}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
