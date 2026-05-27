"""High-level V3 swap encoder.

One free function that takes the primitive routing inputs (token pair,
fees, optional intermediate, kind, amounts, recipient) and emits a
GPv2 Interaction. Dispatches between the four SwapRouter entry points:

  - single-hop sell  → exactInputSingle
  - single-hop buy   → exactOutputSingle
  - multi-hop  sell  → exactInput
  - multi-hop  buy   → exactOutput

Used by both V3Source (LiquiditySource API) and RouterSolver (direct
batched path). Keeping the dispatch in one place means the slippage math
and the single/multi/sell/buy table can't drift between the two callers.
"""

from __future__ import annotations

from src.encoder.interactions import Interaction
from src.encoder.v3_calldata import (
    encode_exact_input_single,
    encode_exact_output_single,
)
from src.encoder.v3_path import (
    encode_exact_input,
    encode_exact_output,
    pack_v3_path,
)


def encode_v3_swap(
    *,
    token_in: str,
    token_out: str,
    fee_in: int,
    intermediate: str | None,
    fee_out: int | None,
    exact_output: bool,
    executed_sell: int,
    executed_buy: int,
    recipient: str,
    deadline: int,
    slippage_bps: int,
    router_address: str,
) -> Interaction:
    """Build a V3 swap Interaction. Keyword-only — too many similarly-typed
    ints in one signature to trust positional order.

    Slippage applies asymmetrically: sell-kind sets ``amountOutMinimum``
    (we accept up to ``slippage_bps`` less output than quoted), buy-kind
    sets ``amountInMaximum`` (we accept up to ``slippage_bps`` more input
    consumed). Buy-kind rounds up to keep integer-truncation from tightening
    the cap below the intended safety margin.
    """
    if intermediate is None:
        if exact_output:
            calldata = encode_exact_output_single(
                token_in=token_in,
                token_out=token_out,
                fee=fee_in,
                recipient=recipient,
                deadline=deadline,
                amount_out=executed_buy,
                amount_in_maximum=apply_slippage_up(executed_sell, slippage_bps),
            )
        else:
            calldata = encode_exact_input_single(
                token_in=token_in,
                token_out=token_out,
                fee=fee_in,
                recipient=recipient,
                deadline=deadline,
                amount_in=executed_sell,
                amount_out_minimum=apply_slippage_down(executed_buy, slippage_bps),
            )
    else:
        if fee_out is None:
            raise ValueError("fee_out required when intermediate is set")
        packed = pack_v3_path(
            tokens=[token_in, intermediate, token_out],
            fees=[fee_in, fee_out],
        )
        if exact_output:
            calldata = encode_exact_output(
                path=packed,
                recipient=recipient,
                deadline=deadline,
                amount_out=executed_buy,
                amount_in_maximum=apply_slippage_up(executed_sell, slippage_bps),
            )
        else:
            calldata = encode_exact_input(
                path=packed,
                recipient=recipient,
                deadline=deadline,
                amount_in=executed_sell,
                amount_out_minimum=apply_slippage_down(executed_buy, slippage_bps),
            )

    return Interaction(target=router_address, value=0, call_data=calldata)


def apply_slippage_down(amount: int, slippage_bps: int) -> int:
    """Reduce ``amount`` by ``slippage_bps`` for amountOutMinimum (sell-kind)."""
    return amount * (10_000 - slippage_bps) // 10_000


def apply_slippage_up(amount: int, slippage_bps: int) -> int:
    """Increase ``amount`` by ``slippage_bps`` for amountInMaximum (buy-kind).

    Rounded up so the cap can't drift tighter than intended via integer
    truncation — a cap below the actual required input would cause a
    spurious revert on swaps that quoted exactly at the edge.
    """
    return (amount * (10_000 + slippage_bps) + 9_999) // 10_000
