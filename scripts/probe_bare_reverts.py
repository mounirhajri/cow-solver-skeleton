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
from collections import defaultdict
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
from src.models.auction import Auction
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

# Offline-mode flags, ranked worst → best. The reconstruction picks the
# highest-ranked flag observed across a solution's trades.
_LIMIT_FLOOR_VIOLATION = "limit_floor_violation"  # sell order: floor(buy) < buyAmount
_LIMIT_VIOLATION = "limit_violation"  # buy order: ceil(sell) > sellAmount
_CONSERVATION_SHORT = "conservation_short"  # swap delivers < sum owed (approx)
_OFFLINE_NO_PRICE = "no_price"  # missing a clearing price → couldn't fully check
_ORDER_MISSING = "order_missing"  # trade uid absent from raw_auction → can't check
_EXPLAINED_NONE = "explained_none"  # stored math consistent → revert is on-chain
_PARSE_ERROR = "parse_error"  # raw_auction failed to validate

# Higher rank = more conclusive root-cause signal.
_FLAG_RANK = {
    _LIMIT_FLOOR_VIOLATION: 5,
    _LIMIT_VIOLATION: 4,
    _CONSERVATION_SHORT: 3,
    _ORDER_MISSING: 2,
    _OFFLINE_NO_PRICE: 1,
    _EXPLAINED_NONE: 0,
    _PARSE_ERROR: -1,
}


def _ceil_div(a: int, b: int) -> int:
    """Ceiling division, mirroring joint_clearing._ceil_div (GPv2 buy-side rounding)."""
    if b <= 0:
        return 0
    return (a + b - 1) // b


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


# ---------------------------------------------------------------------------
# Offline mode: reconstruct GPv2 limit + conservation math from stored rows
# ---------------------------------------------------------------------------


@dataclass
class OfflineRow:
    uid: str
    strategy: str
    n_trades: int
    worst_flag: str
    deficit_wei: int = 0
    deficit_bps: float = float("nan")


def _analyse_offline(
    solution: dict[str, Any],
    raw_auction: dict[str, Any],
    strategy: str,
) -> OfflineRow:
    """Reconstruct each trade's GPv2-side amounts from the stored solution + auction.

    No RPC: uses only the persisted clearing prices, trade executed amounts, and the
    order limits in ``raw_auction``. Reproduces GPv2's rounding (floor on sell-order
    buy amounts, ceil on buy-order sell amounts) to catch the ceil-vs-floor limit
    edge that ``joint_clearing._all_limits_satisfied`` can pass but settle() reverts on.
    """
    trades = solution.get("trades", []) or []
    n_trades = len(trades)
    uid = _first_uid(solution)

    try:
        auction = Auction.model_validate(raw_auction)
    except Exception as exc:  # noqa: BLE001 — diagnostic: never crash on one row
        log.debug("offline_parse_error", uid=uid, error=str(exc))
        return OfflineRow(uid, strategy, n_trades, _PARSE_ERROR)

    order_map = {o.uid: o for o in auction.orders}
    prices = _price_map(solution)

    worst_flag = _EXPLAINED_NONE
    deficit_wei = 0
    deficit_bps = float("nan")

    def _consider(flag: str, dwei: int, dbps: float) -> None:
        nonlocal worst_flag, deficit_wei, deficit_bps
        better_rank = _FLAG_RANK[flag] > _FLAG_RANK[worst_flag]
        same_rank_bigger = _FLAG_RANK[flag] == _FLAG_RANK[worst_flag] and dwei > deficit_wei
        if better_rank or same_rank_bigger:
            worst_flag, deficit_wei, deficit_bps = flag, dwei, dbps

    # Sum floored sell-order buy amounts per buy-token (for the conservation slack).
    buy_owed: dict[str, int] = defaultdict(int)

    for tr in trades:
        order = order_map.get(str(tr.get("orderUid", "")))
        if order is None:
            _consider(_ORDER_MISSING, 0, float("nan"))
            continue
        try:
            executed = int(tr.get("executedAmount"))
        except (TypeError, ValueError):
            _consider(_ORDER_MISSING, 0, float("nan"))
            continue

        p_sell = prices.get(order.sell_token.lower())
        p_buy = prices.get(order.buy_token.lower())
        if p_sell is None or p_buy is None or p_buy == 0:
            _consider(_OFFLINE_NO_PRICE, 0, float("nan"))
            continue

        if order.kind == "sell":
            executed_buy = (executed * p_sell) // p_buy  # GPv2 floor
            buy_owed[order.buy_token.lower()] += executed_buy
            if executed_buy < order.buy_amount:
                d = order.buy_amount - executed_buy
                bps = 10_000 * d / order.buy_amount if order.buy_amount else float("nan")
                _consider(_LIMIT_FLOOR_VIOLATION, d, bps)
        else:  # buy order
            if p_sell == 0:
                _consider(_OFFLINE_NO_PRICE, 0, float("nan"))
                continue
            executed_sell = _ceil_div(executed * p_buy, p_sell)  # GPv2 ceil
            if executed_sell > order.sell_amount:
                d = executed_sell - order.sell_amount
                bps = 10_000 * d / order.sell_amount if order.sell_amount else float("nan")
                _consider(_LIMIT_VIOLATION, d, bps)

    # Secondary, approximate: does each buy-token's swap delivery cover what users
    # are owed?  Swap delivery for a sell→buy group == prices[sell_token] (combined
    # buy).  With GPv2 floor on the per-order owed amounts this is normally >= 0;
    # a negative slack would indicate a genuine multi-order distribution shortfall.
    delivered: dict[str, int] = defaultdict(int)
    for cd in _find_swap_interactions(solution):
        try:
            sw = _decode_swap(cd)
        except Exception:  # noqa: BLE001
            continue
        if sw is None or sw.kind == "buy":
            continue
        p_in = prices.get(sw.token_in.lower())
        if p_in is not None:
            delivered[sw.token_out.lower()] += p_in
    for token, owed in buy_owed.items():
        if token not in delivered:
            # No decodable swap delivers this token → we can't reconstruct the
            # delivery side offline. Absence of a swap is NOT a shortfall; skip
            # rather than emit a spurious conservation_short.
            continue
        slack = delivered[token] - owed
        if slack < 0:
            bps = 10_000 * (-slack) / owed if owed else float("nan")
            _consider(_CONSERVATION_SHORT, -slack, bps)

    return OfflineRow(uid, strategy, n_trades, worst_flag, deficit_wei, deficit_bps)


async def run_offline(days: int, limit: int, strategy: str) -> None:
    since = datetime.now(UTC) - timedelta(days=days)
    print(
        f"\nBare-revert OFFLINE conservation/limit probe  "
        f"(last {days} day(s), since {since:%Y-%m-%d %H:%M} UTC) [strategy={strategy}]"
    )

    Session = get_session_factory()
    async with Session() as session:
        q = (
            select(
                ShadowSolution.solution,
                ShadowSolution.strategy,
                ShadowAuction.raw_auction,
            )
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
        print("  Nothing to probe in window. Widen --days or check the strategy.\n")
        return

    header = (
        f"  {'uid':<14} {'strat':<14} {'n_tr':>4} {'deficit_wei':>22} "
        f"{'deficit_bps':>12}  worst_flag"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    tally: dict[str, int] = {}
    floor_deficits_wei: list[float] = []
    floor_deficits_bps: list[float] = []

    for solution, strat, raw_auction in rows:
        if not isinstance(solution, dict) or not isinstance(raw_auction, dict):
            tally[_PARSE_ERROR] = tally.get(_PARSE_ERROR, 0) + 1
            continue
        res = _analyse_offline(solution, raw_auction, str(strat))
        tally[res.worst_flag] = tally.get(res.worst_flag, 0) + 1
        if res.worst_flag == _LIMIT_FLOOR_VIOLATION:
            floor_deficits_wei.append(float(res.deficit_wei))
            if res.deficit_bps == res.deficit_bps:  # not NaN  # noqa: PLR0124
                floor_deficits_bps.append(res.deficit_bps)

        bps_s = f"{res.deficit_bps:11.4f}" if res.deficit_bps == res.deficit_bps else "          -"  # noqa: PLR0124, E501
        print(
            f"  {res.uid:<14} {res.strategy:<14} {res.n_trades:>4} "
            f"{res.deficit_wei:>22} {bps_s:>12}  {res.worst_flag}"
        )

    print("\n" + "=" * 72)
    print("Summary")
    print("=" * 72)
    total = sum(tally.values())
    for flag in sorted(tally, key=lambda k: (-_FLAG_RANK.get(k, 0), -tally[k])):
        n = tally[flag]
        pct = 100 * n / total if total else 0.0
        print(f"  {n:>4}  ({pct:5.1f}%)  {flag}")

    if floor_deficits_wei:
        print(
            "\n  limit_floor_violation deficit (wei): "
            f"min={int(min(floor_deficits_wei))}  "
            f"median={int(_percentile(floor_deficits_wei, 50))}  "
            f"p90={int(_percentile(floor_deficits_wei, 90))}  "
            f"max={int(max(floor_deficits_wei))}"
        )
        if floor_deficits_bps:
            print(
                "  limit_floor_violation deficit (bps): "
                f"min={min(floor_deficits_bps):.4f}  "
                f"median={_percentile(floor_deficits_bps, 50):.4f}  "
                f"p90={_percentile(floor_deficits_bps, 90):.4f}  "
                f"max={max(floor_deficits_bps):.4f}"
            )

    print(
        "\n  Decision gate:\n"
        f"    • {_LIMIT_FLOOR_VIOLATION} dominates → root cause confirmed: ceil→floor\n"
        "      limit check in joint_clearing._all_limits_satisfied (deficit ≈ 1-wei class).\n"
        f"    • {_CONSERVATION_SHORT} dominates  → multi-order distribution under-delivers.\n"
        f"    • {_EXPLAINED_NONE} dominates    → stored math is fine; revert is on-chain\n"
        "      (signature / ERC20 / router-vs-quoter), not the clearing math.\n"
        "    • Either way: do NOT pitch feas% to CoW — this only identifies the lever.\n"
    )


async def run_requote(
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
        description="Probe bare-revert phantoms: offline limit/conservation "
        "reconstruction (default) or RPC re-quote at the sim-block."
    )
    parser.add_argument(
        "--mode",
        choices=("offline", "requote"),
        default="offline",
        help=(
            "offline (default): reconstruct GPv2 limit/conservation math from stored "
            "rows, no RPC, full-window. requote: re-quote each swap at the sim-block "
            "(bounded by the ~100-min non-archive RPC window)."
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=2,
        help="Offline-mode look-back window in days (default: 2).",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=90,
        help="Requote-mode window in minutes (default: 90; bounded by non-archive RPC).",
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
    if args.mode == "offline":
        asyncio.run(
            run_offline(days=args.days, limit=args.limit, strategy=args.strategy)
        )
    else:
        asyncio.run(
            run_requote(minutes=args.minutes, limit=args.limit, strategy=args.strategy)
        )


if __name__ == "__main__":
    main()
