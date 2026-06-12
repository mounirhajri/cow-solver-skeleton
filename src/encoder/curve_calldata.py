"""Curve stable-pool swap encoder — direct pool ``exchange`` path.

Curve pools are swapped by calling the pool contract directly; there is no
router indirection and no Permit2. A settlement therefore needs TWO
interactions per Curve exact-input swap:

  1. ``sell_token.approve(pool, amount_in)``
     The pool pulls the input via ``transferFrom(msg.sender, self, dx)``
     and the GPv2 settlement holds no standing allowances (same
     hard-learned lesson as the V3 ``STF`` failure — see
     ``src/encoder/erc20.py``), so each settlement grants exactly the
     swap's input amount.

  2. ``pool.exchange(i, j, dx, min_dy)``

Why the 4-arg ``exchange`` form: both pool generations we route (classic
StableSwap 2pool AND stableswap-NG, blockscout-verified sources 2026-06-12)
declare ``exchange(i, j, _dx, _min_dy, _receiver: address = msg.sender)`` —
a Vyper default argument, which compiles to BOTH a 4-arg and a 5-arg ABI
entry point. We deliberately use the 4-arg selector: the output then goes to
``_receiver = msg.sender``, and ``msg.sender`` of the interaction IS the
GPv2 settlement contract, which is exactly where the buy token must land
for the settlement's trade payouts. Passing the settlement explicitly via
the 5-arg form would be equivalent but adds a word of calldata and another
address to get wrong.

Token-flow accounting mirrors ``v4_calldata.py`` (read its docstring): the
DECLARED interaction output must be the PROMISED amount (``executed_buy`` —
what the clearing prices pay the trader), NOT the slippage-reduced
``min_amount_out``, which lives only INSIDE the calldata as the on-chain
revert guard. Declaring the minimum would make the solution's token flows
look insolvent to the CoW driver (declared inflow < trade payout) and risk
whole-solution rejection — the 2026-06-08 non-conformance failure mode.

Selectors are precomputed module-level constants — they never change for a
given signature and recomputing them per call adds keccak overhead for no
reason.
"""

from __future__ import annotations

from eth_abi import encode
from eth_utils import keccak

from src.encoder.erc20 import encode_approve
from src.encoder.interactions import Interaction

# Vyper signature kept verbatim so a grep finds matching artefacts.
_SIG_EXCHANGE = "exchange(int128,int128,uint256,uint256)"

# keccak256("exchange(int128,int128,uint256,uint256)")[:4] == 0x3df02124.
EXCHANGE_SELECTOR = keccak(text=_SIG_EXCHANGE)[:4]

_INT128_MIN, _INT128_MAX = -(2**127), 2**127 - 1
_UINT256_MAX = 2**256 - 1


def encode_exchange(i: int, j: int, dx: int, min_dy: int) -> bytes:
    """Encode ``pool.exchange(i, j, dx, min_dy)`` calldata.

    Exact-input only (Curve semantics): ``dx`` is fixed, ``min_dy`` is the
    on-chain revert guard against pool drift between quote and settlement.
    """
    if not 0 <= i <= _INT128_MAX or not 0 <= j <= _INT128_MAX:
        # int128 on-chain; negative or oversized indices are caller bugs.
        raise ValueError(f"coin indices must be non-negative int128, got i={i}, j={j}")
    if i == j:
        raise ValueError(f"coin indices must differ, got i == j == {i}")
    if not 0 < dx <= _UINT256_MAX:
        raise ValueError(f"dx must be positive uint256, got {dx}")
    if not 0 <= min_dy <= _UINT256_MAX:
        raise ValueError(f"min_dy must fit uint256, got {min_dy}")
    return EXCHANGE_SELECTOR + encode(
        ["int128", "int128", "uint256", "uint256"], [i, j, dx, min_dy]
    )


def encode_curve_swap_interactions(
    *,
    pool_address: str,
    i: int,
    j: int,
    amount_in: int,
    min_amount_out: int,
    sell_token: str,
    buy_token: str,
    executed_buy: int,
) -> list[Interaction]:
    """Build the ordered interaction list for one Curve exact-input swap.

    Returns ``[erc20_approve, exchange]`` — order matters: the allowance
    must exist before the pool's ``transferFrom`` consumes it. Keyword-only
    — too many similarly-typed ints in one signature to trust positional
    order.

    The approve moves no tokens itself so it carries empty
    ``inputs``/``outputs``; the ``exchange`` interaction consumes
    ``amount_in`` of ``sell_token`` from the settlement and returns
    ``buy_token`` to it (4-arg form → ``_receiver = msg.sender`` = the
    settlement, see module docstring). The declared output is
    ``executed_buy`` (the promised amount), never ``min_amount_out`` —
    see module docstring for why.
    """
    if executed_buy <= 0:
        raise ValueError(f"executed_buy must be positive, got {executed_buy}")
    if min_amount_out > executed_buy:
        # The revert guard should never exceed the promise — that would
        # revert on-chain even when the promised amount is delivered.
        raise ValueError(
            f"min_amount_out ({min_amount_out}) must not exceed executed_buy ({executed_buy})"
        )

    erc20_approve = Interaction(
        target=sell_token,
        value=0,
        call_data=encode_approve(pool_address, amount_in),
    )
    exchange = Interaction(
        target=pool_address,
        value=0,
        call_data=encode_exchange(i, j, amount_in, min_amount_out),
        inputs=((sell_token, amount_in),),
        outputs=((buy_token, executed_buy),),
    )
    return [erc20_approve, exchange]
