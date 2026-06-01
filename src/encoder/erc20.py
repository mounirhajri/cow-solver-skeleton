"""ERC20 calldata builders.

Currently just ``approve(address,uint256)`` — needed so a settle() can grant
the Uniswap V3 SwapRouter permission to pull the sell token out of the
GPv2Settlement contract. Without a Settlement→Router approval the router's
``tokenIn.transferFrom(settlement → pool)`` reverts ``STF`` /
``ERC20: transfer amount exceeds allowance`` and every V3 swap fails.

The Settlement holds no standing allowance to any router (verified on-chain:
``allowance(0x9008…ab41, SwapRouter) == 0`` for WETH/USDC/USDT), and we cannot
broadcast a tx "from" the Settlement to set one — allowances are only granted
from inside ``settle()`` via an approve interaction. So each settlement that
contains a V3 swap must emit its own approve as a pre/intra interaction.

The selector is a precomputed module-level constant — it never changes for a
given Solidity signature and recomputing it per call adds keccak overhead for
no reason.
"""

from __future__ import annotations

from eth_abi import encode
from eth_utils import keccak

# Solidity signature kept verbatim so a grep finds matching artefacts.
_SIG_APPROVE = "approve(address,uint256)"

# keccak256("approve(address,uint256)")[:4] == 0x095ea7b3 (canonical ERC20).
APPROVE_SELECTOR = keccak(text=_SIG_APPROVE)[:4]


def encode_approve(spender: str, amount: int) -> bytes:
    """Encode ``approve(spender, amount)`` calldata.

    ``amount`` is an absolute allowance (approve overwrites, it does not
    increment). Callers pass the exact upper bound the spender may pull —
    ``executed_sell`` for sell-kind swaps, ``amountInMaximum`` for buy-kind —
    so the grant is no larger than the swap requires.
    """
    return APPROVE_SELECTOR + encode(["address", "uint256"], [spender, amount])
