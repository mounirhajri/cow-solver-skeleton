"""Diagnostic probe: confirm (or refute) the clearing-price over-promise hypothesis.

After the V3-approve fix deployed, ~87% of remaining ``router-v2`` phantoms revert
with a *bare* ``"execution reverted"`` (no reason string). Leading hypothesis:
**clearing-price over-promise** — the solver sets ``prices[sell_token] = raw quote``
(the buy amount GPv2 owes the user), but the emitted V3 swap only guarantees
``amountOutMinimum = (1 - slippage) * quote``. If the realised pool output ``R``
lands in ``[amountOutMinimum, promised)``, the swap *succeeds* but the settlement is
short by ``promised - R`` → GPv2 token-conservation reverts with a bare string.

This probe proves the hypothesis WITHOUT any production change, RPC-tier upgrade, or
state-override gymnastics. For each recent bare-revert ``router-v2`` solution it:

  1. decodes the swap interaction's calldata (exactInputSingle / exactInput),
  2. re-quotes that exact swap at the competition ``simulationBlock`` (the block the
     validator and the real settlement evaluate) via QuoterV2,
  3. compares the realised output ``R`` against ``amountOutMinimum`` and ``promised``.

Per row we classify into three buckets:

  * ``R < amountOutMinimum``            → SWAP-LEVEL revert (drift > slippage; swap fails)
  * ``amountOutMinimum <= R < promised``→ OVER-PROMISE confirmed (swap ok, settle short)
  * ``R >= promised``                   → over-promise NOT the cause (flag for follow-up)

The distribution of ``(promised - R)`` in **bps** over the over-promise rows sizes any
eventual haircut: if it sits under the current 50 bps slippage, a haircut fixes it;
if it routinely exceeds 50 bps, the lever is block-aligned quoting, not the haircut.

Re-quoting needs the sim-block state to still be inside the non-archive RPC window
(~100 min on the configured PublicNode endpoint). Rows whose block has aged out RAISE
at ``eth_call`` (a non-revert RPC error) and are counted ``aged_out`` — never crash.

Usage
-----
    python -m scripts.probe_bare_reverts [--minutes 90] [--limit 20] \\
        [--strategy router-v2]

Run inside the cow-solver container so it reads the prod DB + RPC config:
    docker exec cow-solver python -m scripts.probe_bare_reverts --minutes 90 --limit 20
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from eth_abi import decode
from sqlalchemy import select

from src.config import settings
from src.encoder.v3_calldata import (
    EXACT_INPUT_SINGLE_SELECTOR,
    EXACT_OUTPUT_SINGLE_SELECTOR,
)
from src.encoder.v3_path import EXACT_INPUT_SELECTOR, EXACT_OUTPUT_SELECTOR
from src.log import get_logger
from src.persistence.db import get_session_factory
from src.persistence.models import ShadowAuction, ShadowSolution
from src.routing.amm_v3 import QUOTER_V2_ADDRESS
from src.routing.rpc import RpcClient
from src.routing.v3_batched import (
    _decode_multi_hop_return,
    _decode_single_hop_return,
    _encode_quote_exact_input,
    _encode_quote_exact_input_single,
)

log = get_logger(__name__)

# The exact bare revert string geth/erigon return for a low-level revert with no
# reason data (EIP-1474 code 3). A GPv2 token-conservation failure surfaces here.
BARE_REVERT = "execution reverted"

# ERC-20 approve selector — the OTHER interaction the encoder emits before a swap.
# We skip it when locating the swap interaction.
_APPROVE_SELECTOR = "095ea7b3"

# eth_abi struct shapes (must match src/encoder/v3_calldata.py + v3_path.py).
_SINGLE_STRUCT = "(address,address,uint24,address,uint256,uint256,uint256,uint160)"
_MULTI_STRUCT = "(bytes,address,uint256,uint256,uint256)"

# Verdict bucket labels.
_SWAP_LEVEL = "swap_level_revert"
_OVER_PROMISE = "over_promise"
_UNEXPLAINED = "unexplained"
_BUY_NOT_ANALYSED = "buy_not_analysed"
_AGED_OUT = "aged_out"
_NO_SIM_BLOCK = "no_sim_block"
_NO_SWAP = "no_swap_found"
_NO_PRICE = "no_clearing_price"
_DECODE_FAIL = "decode_fail"


@dataclass(frozen=True)
class SwapInfo:
    """Decoded fields of a single V3 SwapRouter interaction."""

    kind: str  # "exactInputSingle" | "exactInput" | "buy"
    token_in: str
    token_out: str
    fee: int
    amount_in: int
    amount_out_minimum: int
    path: bytes | None  # packed path for multi-hop exactInput, else None


def _selector_hex(selector: bytes) -> str:
    return selector.hex()


def _decode_swap(call_data: bytes) -> SwapInfo | None:
    """Decode a V3 swap interaction's calldata, or None if not a recognised swap.

    Buy-kind (exactOutput*) swaps are recognised but returned with ``kind="buy"``
    and not analysed further — router-v2 phantoms are dominantly sell-kind, and
    the over-promise mechanics differ on the sell side for buy orders.
    """
    if len(call_data) < 4:
        return None
    selector = call_data[:4].hex()
    body = call_data[4:]

    if selector == _selector_hex(EXACT_INPUT_SINGLE_SELECTOR):
        (params,) = decode([_SINGLE_STRUCT], body)
        token_in, token_out, fee, _recip, _deadline, amount_in, amount_out_min, _lim = params
        return SwapInfo(
            kind="exactInputSingle",
            token_in=token_in,
            token_out=token_out,
            fee=int(fee),
            amount_in=int(amount_in),
            amount_out_minimum=int(amount_out_min),
            path=None,
        )

    if selector == _selector_hex(EXACT_INPUT_SELECTOR):
        (params,) = decode([_MULTI_STRUCT], body)
        path, _recip, _deadline, amount_in, amount_out_min = params
        return SwapInfo(
            kind="exactInput",
            token_in="0x" + path[:20].hex(),
            token_out="0x" + path[-20:].hex(),
            fee=0,
            amount_in=int(amount_in),
            amount_out_minimum=int(amount_out_min),
            path=bytes(path),
        )

    if selector in (
        _selector_hex(EXACT_OUTPUT_SINGLE_SELECTOR),
        _selector_hex(EXACT_OUTPUT_SELECTOR),
    ):
        return SwapInfo(
            kind="buy",
            token_in="",
            token_out="",
            fee=0,
            amount_in=0,
            amount_out_minimum=0,
            path=None,
        )

    return None


def _find_swap_interactions(solution: dict[str, Any]) -> list[bytes]:
    """Return calldata bytes of every V3 SwapRouter swap interaction in a solution.

    A swap interaction targets ``settings.v3_swap_router`` and is not the ERC-20
    ``approve`` the encoder emits alongside it.
    """
    router = settings.v3_swap_router.lower()
    out: list[bytes] = []
    for ix in solution.get("interactions", []):
        target = str(ix.get("target", "")).lower()
        if target != router:
            continue
        raw = str(ix.get("callData", ""))
        if not raw.startswith("0x") or len(raw) < 10:
            continue
        if raw[2:10].lower() == _APPROVE_SELECTOR:
            continue
        out.append(bytes.fromhex(raw[2:]))
    return out


def _price_map(solution: dict[str, Any]) -> dict[str, int]:
    """Lower-cased clearing-price map (token addr → price as int)."""
    prices = solution.get("prices", {}) or {}
    out: dict[str, int] = {}
    for token, value in prices.items():
        try:
            out[str(token).lower()] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def _first_uid(solution: dict[str, Any]) -> str:
    trades = solution.get("trades", []) or []
    if not trades:
        return "(no-trade)"
    uid = str(trades[0].get("orderUid", "?"))
    short = uid[:10] + "…" if len(uid) > 11 else uid
    return short + (f"+{len(trades) - 1}" if len(trades) > 1 else "")


def _gap_bps(promised: int, realised: int) -> float:
    if promised <= 0:
        return float("nan")
    return 10_000 * (promised - realised) / promised


async def _requote(rpc: RpcClient, swap: SwapInfo, block: str) -> tuple[bool, int]:
    """Re-quote ``swap`` at ``block``. Returns (quoter_ok, realised_output).

    ``quoter_ok=False`` means the QuoterV2 call itself reverted at that block —
    the pool cannot deliver the requested input → the swap would revert too.
    Raises RuntimeError for infra-class errors (aged-out block, rate limit): the
    caller maps a raise to the ``aged_out`` bucket.
    """
    if swap.kind == "exactInputSingle":
        calldata = _encode_quote_exact_input_single(
            swap.token_in, swap.token_out, swap.amount_in, swap.fee
        )
        ok, ret = await rpc.eth_call_capture(QUOTER_V2_ADDRESS, calldata, block=block)
        if not ok:
            return False, 0
        return True, _decode_single_hop_return(bytes.fromhex(ret[2:]))

    # multi-hop exactInput
    assert swap.path is not None
    calldata = _encode_quote_exact_input(swap.path, swap.amount_in)
    ok, ret = await rpc.eth_call_capture(QUOTER_V2_ADDRESS, calldata, block=block)
    if not ok:
        return False, 0
    return True, _decode_multi_hop_return(bytes.fromhex(ret[2:]))


@dataclass
class RowResult:
    uid: str
    block: int | None
    verdict: str
    promised: int = 0
    amount_out_minimum: int = 0
    realised: int = 0
    gap_bps: float = float("nan")


async def _analyse_row(
    rpc: RpcClient,
    solution: dict[str, Any],
    sim_block: int | None,
) -> RowResult:
    uid = _first_uid(solution)

    if sim_block is None or sim_block <= 0:
        return RowResult(uid=uid, block=sim_block, verdict=_NO_SIM_BLOCK)

    swaps = _find_swap_interactions(solution)
    if not swaps:
        return RowResult(uid=uid, block=sim_block, verdict=_NO_SWAP)

    # Analyse the first swap interaction. For single-order router-v2 solutions
    # (the dominant phantom shape) there is exactly one; for joint solves we take
    # the first swap and look its sell-token clearing price up directly.
    try:
        swap = _decode_swap(swaps[0])
    except Exception as exc:  # noqa: BLE001 — diagnostic: never crash on one row
        log.debug("swap_decode_failed", uid=uid, error=str(exc))
        return RowResult(uid=uid, block=sim_block, verdict=_DECODE_FAIL)

    if swap is None:
        return RowResult(uid=uid, block=sim_block, verdict=_NO_SWAP)
    if swap.kind == "buy":
        return RowResult(uid=uid, block=sim_block, verdict=_BUY_NOT_ANALYSED)

    promised = _price_map(solution).get(swap.token_in.lower())
    if promised is None:
        return RowResult(uid=uid, block=sim_block, verdict=_NO_PRICE)

    try:
        quoter_ok, realised = await _requote(rpc, swap, hex(sim_block))
    except RuntimeError as exc:
        log.debug("requote_infra_error", uid=uid, block=sim_block, error=str(exc))
        return RowResult(uid=uid, block=sim_block, verdict=_AGED_OUT)

    if not quoter_ok or realised < swap.amount_out_minimum:
        verdict = _SWAP_LEVEL
    elif realised < promised:
        verdict = _OVER_PROMISE
    else:
        verdict = _UNEXPLAINED

    return RowResult(
        uid=uid,
        block=sim_block,
        verdict=verdict,
        promised=promised,
        amount_out_minimum=swap.amount_out_minimum,
        realised=realised,
        gap_bps=_gap_bps(promised, realised),
    )


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1)))))
    return ordered[k]


async def run_probe(
    minutes: int,
    limit: int,
    strategy: str,
) -> None:
    since = datetime.now(UTC) - timedelta(minutes=minutes)
    print(
        f"\nBare-revert over-promise probe  "
        f"(last {minutes} min, since {since:%Y-%m-%d %H:%M} UTC) "
        f"[strategy={strategy}]"
    )

    Session = get_session_factory()
    async with Session() as session:
        q = (
            select(ShadowSolution.solution, ShadowAuction.raw_auction)
            .join(ShadowAuction, ShadowSolution.auction_id == ShadowAuction.auction_id)
            .where(ShadowSolution.strategy == strategy)
            .where(ShadowSolution.feasible.is_(False))
            .where(ShadowSolution.revert_reason == BARE_REVERT)
            .where(ShadowSolution.created_at >= since)
            .order_by(ShadowSolution.created_at.desc())
            .limit(limit)
        )
        rows = (await session.execute(q)).all()

    print(f"  matched {len(rows)} bare-revert {strategy} solution(s)\n")
    if not rows:
        print("  Nothing to probe in window. Widen --minutes or check the strategy.\n")
        return

    rpc = RpcClient(settings.rpc_arbitrum)

    header = (
        f"  {'uid':<14} {'block':>10} {'promised':>20} {'amtOutMin':>20} "
        f"{'realised(R)':>20} {'gap_bps':>9}  verdict"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    tally: dict[str, int] = {}
    gap_samples: list[float] = []

    for solution, raw_auction in rows:
        if not isinstance(solution, dict):
            tally[_DECODE_FAIL] = tally.get(_DECODE_FAIL, 0) + 1
            continue
        sim_block_raw = (raw_auction or {}).get("simulationBlock")
        sim_block = int(sim_block_raw) if isinstance(sim_block_raw, int) else None

        res = await _analyse_row(rpc, solution, sim_block)
        tally[res.verdict] = tally.get(res.verdict, 0) + 1
        if res.verdict == _OVER_PROMISE:
            gap_samples.append(res.gap_bps)

        block_s = str(res.block) if res.block else "-"
        gap_s = f"{res.gap_bps:8.1f}" if res.gap_bps == res.gap_bps else "       -"  # noqa: PLR0124
        print(
            f"  {res.uid:<14} {block_s:>10} {res.promised:>20} "
            f"{res.amount_out_minimum:>20} {res.realised:>20} {gap_s:>9}  {res.verdict}"
        )

    print("\n" + "=" * 72)
    print("Summary")
    print("=" * 72)
    total = sum(tally.values())
    for verdict in sorted(tally, key=lambda k: -tally[k]):
        n = tally[verdict]
        pct = 100 * n / total if total else 0.0
        print(f"  {n:>4}  ({pct:5.1f}%)  {verdict}")

    if gap_samples:
        print("\n  over-promise gap distribution (bps): "
              f"min={min(gap_samples):.1f}  "
              f"median={_percentile(gap_samples, 50):.1f}  "
              f"p90={_percentile(gap_samples, 90):.1f}  "
              f"max={max(gap_samples):.1f}")
        cur = settings.encoder_slippage_bps
        over = sum(1 for g in gap_samples if g > cur)
        print(
            f"  current encoder slippage = {cur} bps; "
            f"{over}/{len(gap_samples)} over-promise rows exceed it "
            "(→ a haircut alone would NOT cover those)."
        )

    print(
        "\n  Decision gate:\n"
        f"    • {_OVER_PROMISE} dominates  → hypothesis confirmed; the encoder fix\n"
        "      (promise == swap's guaranteed bound) is the lever; gap distn sizes it.\n"
        f"    • {_SWAP_LEVEL} dominates    → pool drift, not over-promise; lever is\n"
        "      block-aligned quoting / higher slippage_bps, NOT the clearing haircut.\n"
        "    • Either way: do NOT pitch feas% to CoW — this only tells us the lever.\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe bare-revert phantoms for clearing-price over-promise."
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=90,
        help="Look-back window in minutes (default: 90; bounded by non-archive RPC).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max solutions to probe (default: 20).",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        default="router-v2",
        help="Strategy to probe (default: router-v2).",
    )
    args = parser.parse_args()
    asyncio.run(
        run_probe(minutes=args.minutes, limit=args.limit, strategy=args.strategy)
    )


if __name__ == "__main__":
    main()
