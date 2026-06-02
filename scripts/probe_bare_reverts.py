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

from eth_abi import decode, encode
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
from src.persistence.models import GhostOrder, ShadowAuction, ShadowSolution
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

# ---------------------------------------------------------------------------
# Delivery-mode (router-vs-quoter + counterparty) flags & ERC-20 plumbing
# ---------------------------------------------------------------------------
# Canonical CoW GPv2VaultRelayer — the SAME address on every chain CoW supports.
# Users grant their sell-token allowance to THIS contract (not the settlement);
# settle() pulls each trade's sell tokens through it. Checking allowance against
# the settlement instead would falsely report every order as un-approved.
GPV2_VAULT_RELAYER = "0xC92E8bdf79f0507f65a392b0ab4667716BFE0110"

# ERC-20 view selectors: balanceOf(address) / allowance(address,address).
_BALANCE_OF_SELECTOR = bytes.fromhex("70a08231")
_ALLOWANCE_SELECTOR = bytes.fromhex("dd62ed3e")

# Delivery flags, ranked by on-chain revert *order* — settle() pulls every
# trade's sell tokens (transferIn, through the relayer) BEFORE running the swap
# interactions, so a counterparty-delivery failure aborts the settlement first.
_USER_NO_ALLOWANCE = "user_no_allowance"  # transferIn reverts: owner hasn't approved
_USER_NO_BALANCE = "user_no_balance"  # transferIn reverts: owner lacks the sell tokens
_SWAP_PATH_REVERTS = "swap_path_reverts"  # QuoterV2 reverts → pool/fee path broken
_AMOUNT_OUT_MIN_UNMET = "amount_out_min_unmet"  # swap delivers < amountOutMinimum
_SWAP_CLEAN_USER_OK = "swap_clean_user_ok"  # both legs pass → bare cause is deeper

# Higher rank = earlier/more-decisive on-chain revert point.
_DELIVERY_RANK = {
    _USER_NO_ALLOWANCE: 6,
    _USER_NO_BALANCE: 5,
    _SWAP_PATH_REVERTS: 4,
    _AMOUNT_OUT_MIN_UNMET: 3,
    _SWAP_CLEAN_USER_OK: 0,
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


# ---------------------------------------------------------------------------
# Delivery mode: router-vs-quoter + counterparty on-chain delivery check
# ---------------------------------------------------------------------------


def _balance_calldata(owner: str) -> str:
    return "0x" + (_BALANCE_OF_SELECTOR + encode(["address"], [owner])).hex()


def _allowance_calldata(owner: str, spender: str) -> str:
    return "0x" + (_ALLOWANCE_SELECTOR + encode(["address", "address"], [owner, spender])).hex()


async def _erc20_uint(rpc: RpcClient, token: str, calldata: str, block: str) -> int | None:
    """eth_call an ERC-20 uint256 view at ``block``.

    Returns the value, or ``None`` if the token call itself reverts (non-standard
    token / not a contract at this block). RAISES RuntimeError on infra-class
    errors (aged-out block, rate-limit) so the caller buckets the row ``aged_out``
    rather than misreading an RPC failure as a delivery shortfall.
    """
    ok, ret = await rpc.eth_call_capture(token, calldata, block=block)
    if not ok:
        return None
    h = ret[2:] if ret.startswith("0x") else ret
    if not h:
        return None
    return int(h, 16)


@dataclass
class DeliveryRow:
    uid: str
    block: int | None
    verdict: str
    detail: str = ""


async def _analyse_delivery(
    rpc: RpcClient,
    solution: dict[str, Any],
    raw_auction: dict[str, Any],
    sim_block: int | None,
) -> DeliveryRow:
    """Attribute a bare revert: our swap under-delivers vs the user can't deliver.

    Leg 1 (router-vs-quoter): QuoterV2 runs the *same* pool-swap code SwapRouter
    does, so the quote IS the real router delivery. A quote revert ⇒ the encoded
    pool/fee path is broken (the swap is the bare source); a quote below
    amountOutMinimum ⇒ the swap would revert "Too little received".

    Leg 2 (counterparty): settle() pulls each trade's sell tokens through the vault
    relayer BEFORE the swap. If the order's owner lacks balance/allowance at the
    sim-block, transferIn reverts first (bare for USDT-class tokens) — and that is
    a stale/underfunded order, NOT our bug (CoW's own driver would fail it too).
    """
    uid = _first_uid(solution)
    if not sim_block or sim_block <= 0:
        return DeliveryRow(uid, sim_block, _NO_SIM_BLOCK)
    block = hex(sim_block)
    prices = _price_map(solution)

    order_map: dict[str, Any] = {}
    try:
        auction = Auction.model_validate(raw_auction)
        order_map = {o.uid: o for o in auction.orders}
    except Exception as exc:  # noqa: BLE001 — diagnostic: never crash on one row
        log.debug("delivery_parse_error", uid=uid, error=str(exc))  # Leg 2 degrades

    worst = _SWAP_CLEAN_USER_OK
    detail = ""

    def consider(flag: str, det: str) -> None:
        nonlocal worst, detail
        if _DELIVERY_RANK[flag] > _DELIVERY_RANK[worst]:
            worst, detail = flag, det

    # Leg 1 — swap delivery (router-vs-quoter).
    for cd in _find_swap_interactions(solution):
        try:
            sw = _decode_swap(cd)
        except Exception:  # noqa: BLE001
            continue
        if sw is None or sw.kind == "buy":
            continue
        try:
            ok, realised = await _requote(rpc, sw, block)
        except RuntimeError as exc:
            log.debug("delivery_requote_infra", uid=uid, error=str(exc))
            return DeliveryRow(uid, sim_block, _AGED_OUT)
        if not ok:
            consider(
                _SWAP_PATH_REVERTS,
                f"{sw.token_in[:8]}->{sw.token_out[:8]} fee={sw.fee} pool reverts",
            )
        elif realised < sw.amount_out_minimum:
            consider(_AMOUNT_OUT_MIN_UNMET, f"R={realised} < min={sw.amount_out_minimum}")

    # Leg 2 — counterparty (user) delivery.
    for tr in solution.get("trades", []) or []:
        order = order_map.get(str(tr.get("orderUid", "")))
        if order is None:
            continue
        try:
            executed = int(tr.get("executedAmount"))
        except (TypeError, ValueError):
            continue
        # Amount transferIn pulls from the owner = the order's executed SELL side.
        if order.kind == "sell":
            need = executed
        else:  # buy order: derive the sell-side amount GPv2 pulls (ceil rounding).
            p_sell = prices.get(order.sell_token.lower())
            p_buy = prices.get(order.buy_token.lower())
            need = (
                _ceil_div(executed * p_buy, p_sell)
                if p_sell and p_buy
                else int(order.sell_amount)
            )
        token = order.sell_token
        owner = order.owner
        try:
            bal = await _erc20_uint(rpc, token, _balance_calldata(owner), block)
            alw = await _erc20_uint(
                rpc, token, _allowance_calldata(owner, GPV2_VAULT_RELAYER), block
            )
        except RuntimeError as exc:
            log.debug("delivery_erc20_infra", uid=uid, error=str(exc))
            return DeliveryRow(uid, sim_block, _AGED_OUT)
        if bal is not None and bal < need:
            consider(_USER_NO_BALANCE, f"bal={bal} < need={need} ({token[:10]})")
        if alw is not None and alw < need:
            consider(_USER_NO_ALLOWANCE, f"allow={alw} < need={need} ({token[:10]})")

    return DeliveryRow(uid, sim_block, worst, detail)


async def run_delivery(minutes: int, limit: int, strategy: str) -> None:
    since = datetime.now(UTC) - timedelta(minutes=minutes)
    print(
        f"\nBare-revert DELIVERY probe (router-vs-quoter + counterparty)  "
        f"(last {minutes} min, since {since:%Y-%m-%d %H:%M} UTC) [strategy={strategy}]"
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

    header = f"  {'uid':<14} {'block':>10}  {'verdict':<22}  detail"
    print(header)
    print("  " + "-" * (len(header) - 2))

    tally: dict[str, int] = {}
    for solution, raw_auction in rows:
        if not isinstance(solution, dict) or not isinstance(raw_auction, dict):
            tally[_PARSE_ERROR] = tally.get(_PARSE_ERROR, 0) + 1
            continue
        sim_block_raw = raw_auction.get("simulationBlock")
        sim_block = int(sim_block_raw) if isinstance(sim_block_raw, int) else None
        res = await _analyse_delivery(rpc, solution, raw_auction, sim_block)
        tally[res.verdict] = tally.get(res.verdict, 0) + 1
        block_s = str(res.block) if res.block else "-"
        print(f"  {res.uid:<14} {block_s:>10}  {res.verdict:<22}  {res.detail}")

    print("\n" + "=" * 72)
    print("Summary")
    print("=" * 72)
    total = sum(tally.values())
    for verdict in sorted(tally, key=lambda k: (-_DELIVERY_RANK.get(k, -99), -tally[k])):
        n = tally[verdict]
        pct = 100 * n / total if total else 0.0
        print(f"  {n:>4}  ({pct:5.1f}%)  {verdict}")

    print(
        "\n  Decision gate:\n"
        f"    • {_USER_NO_ALLOWANCE}/{_USER_NO_BALANCE} dominate → NOT our bug:\n"
        "      stale/underfunded orders; settle()'s transferIn reverts first. CoW's\n"
        "      own driver would fail these too — exclude from our phantom accounting.\n"
        f"    • {_SWAP_PATH_REVERTS} dominates → encoder path/fee selection is broken;\n"
        "      the swap itself is the bare source (pool absent at the encoded block).\n"
        f"    • {_AMOUNT_OUT_MIN_UNMET} dominates → amountOutMinimum set too high vs\n"
        "      realised pool output (drift > slippage) → block-aligned quoting lever.\n"
        f"    • {_SWAP_CLEAN_USER_OK} dominates → both legs pass; the bare revert is\n"
        "      deeper (signature / deadline / ERC20 return-value quirk) → next lever\n"
        "      is decoding the trade signature + the settlement's own accounting.\n"
        "    • Either way: do NOT pitch feas% to CoW — this only identifies the lever.\n"
    )


# ---------------------------------------------------------------------------
# Quantify mode: size the un-fundable-order tail (distinct orders + surplus
# concentration + ghost-set cross-ref + on-chain fundability spot-check)
# ---------------------------------------------------------------------------


def _short_uid(uid: str) -> str:
    return uid[:10] + "…" if len(uid) > 11 else uid


def _transfer_in_need(order: Any, executed: int, prices: dict[str, int]) -> int:
    """Sell-side amount settle()'s transferIn pulls from the order owner."""
    if order.kind == "sell":
        return executed
    p_sell = prices.get(order.sell_token.lower())
    p_buy = prices.get(order.buy_token.lower())
    return _ceil_div(executed * p_buy, p_sell) if p_sell and p_buy else int(order.sell_amount)


@dataclass
class OrderAgg:
    uid: str
    short: str
    n_solutions: int = 0
    total_score_wei: int = 0  # summed claimed CIP-14 surplus across recurrences
    sell_token: str = ""
    owner: str = ""
    latest_sim_block: int | None = None
    need: int = 0
    is_ghost: bool = False
    ghost_seen: int = 0
    fundable: str = "unchecked"  # unfundable | fundable | aged_out | no_block


async def run_quantify(days: int, limit: int, strategy: str) -> None:
    since = datetime.now(UTC) - timedelta(days=days)
    print(
        f"\nBare-revert QUANTIFY probe (distinct un-fundable orders + surplus)  "
        f"(last {days} day(s), since {since:%Y-%m-%d %H:%M} UTC) [strategy={strategy}]"
    )

    Session = get_session_factory()
    async with Session() as session:
        q = (
            select(
                ShadowSolution.solution,
                ShadowSolution.our_score_wei,
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
        print("  Nothing to probe in window. Widen --days / --limit or check strategy.\n")
        return

    aggs: dict[str, OrderAgg] = {}
    n_solutions_total = 0
    for solution, score_wei, raw_auction in rows:
        if not isinstance(solution, dict) or not isinstance(raw_auction, dict):
            continue
        trades = solution.get("trades") or []
        if not trades:
            continue
        uid = str(trades[0].get("orderUid", ""))
        if not uid:
            continue
        n_solutions_total += 1
        agg = aggs.get(uid)
        if agg is None:
            agg = OrderAgg(uid=uid, short=_short_uid(uid))
            aggs[uid] = agg
        agg.n_solutions += 1
        agg.total_score_wei += int(score_wei) if score_wei is not None else 0
        if agg.sell_token == "":  # enrich once, from the most-recent row (desc order)
            try:
                auction = Auction.model_validate(raw_auction)
                order = {o.uid: o for o in auction.orders}.get(uid)
            except Exception as exc:  # noqa: BLE001
                log.debug("quantify_parse_error", uid=uid, error=str(exc))
                order = None
            if order is not None:
                try:
                    executed = int(trades[0].get("executedAmount"))
                except (TypeError, ValueError):
                    executed = 0
                agg.sell_token = order.sell_token
                agg.owner = order.owner
                agg.need = _transfer_in_need(order, executed, _price_map(solution))
                sb = raw_auction.get("simulationBlock")
                agg.latest_sim_block = int(sb) if isinstance(sb, int) else None

    # Cross-reference the existing ghost-order set (refresh_ghost_set.py) — these
    # are the orders bipartite ALREADY filters but router-v2 does not consult.
    uids = list(aggs)
    if uids:
        async with Session() as session:
            gq = select(GhostOrder.uid, GhostOrder.n_auctions_seen).where(
                GhostOrder.uid.in_(uids)
            )
            for guid, seen in (await session.execute(gq)).all():
                a = aggs.get(str(guid))
                if a is not None:
                    a.is_ghost, a.ghost_seen = True, int(seen)

    # On-chain fundability spot-check: ONE balanceOf+allowance per DISTINCT order
    # (cheap — the tail is concentrated). Only decidable inside the RPC window.
    rpc = RpcClient(settings.rpc_arbitrum)
    for a in aggs.values():
        if a.latest_sim_block is None or a.sell_token == "":
            a.fundable = "no_block"
            continue
        block = hex(a.latest_sim_block)
        try:
            bal = await _erc20_uint(rpc, a.sell_token, _balance_calldata(a.owner), block)
            alw = await _erc20_uint(
                rpc, a.sell_token, _allowance_calldata(a.owner, GPV2_VAULT_RELAYER), block
            )
        except RuntimeError:
            a.fundable = "aged_out"
            continue
        short = (bal is not None and bal < a.need) or (alw is not None and alw < a.need)
        a.fundable = "unfundable" if short else "fundable"

    ordered = sorted(aggs.values(), key=lambda a: (-a.n_solutions, -a.total_score_wei))
    total_score = sum(a.total_score_wei for a in aggs.values())

    print(
        f"  {'order':<14} {'n_sol':>5} {'sol%':>6} {'claimed_surplus_eth':>20} "
        f"{'ghost(seen)':>12}  fundability"
    )
    print("  " + "-" * 78)
    for a in ordered:
        sol_pct = 100 * a.n_solutions / n_solutions_total if n_solutions_total else 0.0
        eth = a.total_score_wei / 1e18
        ghost_s = f"yes({a.ghost_seen})" if a.is_ghost else "no"
        print(
            f"  {a.short:<14} {a.n_solutions:>5} {sol_pct:>5.1f}% {eth:>20.6f} "
            f"{ghost_s:>12}  {a.fundable}"
        )

    print("\n" + "=" * 72)
    print("Summary")
    print("=" * 72)
    n_distinct = len(aggs)
    print(f"  {n_solutions_total} solution(s) ← {n_distinct} DISTINCT order(s)")

    # Concentration: share of solutions + claimed surplus held by the top-k orders.
    for k in (1, 3, 5):
        top = ordered[:k]
        if not top:
            continue
        top_sol = sum(a.n_solutions for a in top)
        top_sur = sum(a.total_score_wei for a in top)
        sol_share = 100 * top_sol / n_solutions_total if n_solutions_total else 0.0
        sur_share = 100 * top_sur / total_score if total_score else 0.0
        print(
            f"  top-{k:<2} order(s): {sol_share:5.1f}% of solutions, "
            f"{sur_share:5.1f}% of claimed surplus"
        )

    # Ghost-set + fundability splits over DISTINCT orders.
    n_ghost = sum(1 for a in aggs.values() if a.is_ghost)
    print(
        f"\n  already in ghost set: {n_ghost}/{n_distinct} distinct order(s) "
        "(bipartite filters these; router-v2 does NOT consult the set)"
    )
    fund_tally: dict[str, int] = {}
    for a in aggs.values():
        fund_tally[a.fundable] = fund_tally.get(a.fundable, 0) + 1
    parts = "  ".join(f"{k}={v}" for k, v in sorted(fund_tally.items()))
    print(f"  fundability (distinct, RPC-window only): {parts}")
    print(
        f"  total claimed surplus across the bare-revert tail: {total_score / 1e18:.6f} ETH\n"
        "  → if the un-fundable distinct orders concentrate the surplus, a solve-time\n"
        "    deliverability pre-filter (reuse the ghost set in router-v2) removes the\n"
        "    tail truthfully. Do NOT pitch feas% to CoW — this only sizes the lever.\n"
    )


# PR #81 (17b7c6c) — "emit Settlement→SwapRouter approve before each V3 swap" —
# merged 2026-06-02 00:39 UTC; deploy.yml takes ~10 min, so simulations run with
# the approve from ~01:00 UTC. feasrate mode splits at this boundary so the
# approve fix's effect on feas% is isolated from pre-fix rows.
_FIX81_DEPLOYED_AT = datetime(2026, 6, 2, 1, 0, tzinfo=UTC)


async def run_feasrate(days: int, limit: int, strategy: str) -> None:
    """Offline feasibility-rate breakdown (no RPC), split at the #81 deploy.

    Answers the prerequisite to any win-rate claim: how often does a solved
    solution actually pass on-chain? Counts feasible True/False/None over --days
    and isolates the post-#81 slice so the Settlement→Router approve fix's effect
    is visible. feas% = True/(True+False) (decidable rate); check% =
    (True+False)/total (how many got a decidable verdict at all).
    """
    since = datetime.now(UTC) - timedelta(days=days)
    print(
        f"\nFeasibility-rate breakdown (offline)  "
        f"(last {days} day(s), since {since:%Y-%m-%d %H:%M} UTC) [strategy={strategy}]"
    )

    Session = get_session_factory()
    async with Session() as session:
        q = (
            select(ShadowSolution.feasible, ShadowSolution.created_at)
            .where(ShadowSolution.strategy == strategy)
            .where(ShadowSolution.status == "solved")
            .where(ShadowSolution.solution.is_not(None))
            .where(ShadowSolution.created_at >= since)
            .order_by(ShadowSolution.created_at.desc())
            .limit(limit)
        )
        rows = (await session.execute(q)).all()

    print(f"  matched {len(rows)} solved {strategy} solution(s)\n")
    if not rows:
        print("  Nothing to probe in window. Widen --days / --limit or check strategy.\n")
        return

    def _tally(subset: list[Any]) -> tuple[int, int, int]:
        t = sum(1 for f in subset if f is True)
        fa = sum(1 for f in subset if f is False)
        no = sum(1 for f in subset if f is None)
        return t, fa, no

    def _report(label: str, feas_flags: list[Any]) -> None:
        t, fa, no = _tally(feas_flags)
        total = t + fa + no
        decidable = t + fa
        feas_pct = 100 * t / decidable if decidable else 0.0
        check_pct = 100 * decidable / total if total else 0.0
        print(f"  {label}")
        print(
            f"    feasible(True)={t}  phantom(False)={fa}  unknown(None)={no}  "
            f"(n={total})"
        )
        print(
            f"    feas% (True/decidable) = {feas_pct:.1f}%   "
            f"check% (decidable/total) = {check_pct:.1f}%"
        )

    all_flags = [f for f, _ in rows]
    pre = [f for f, c in rows if c < _FIX81_DEPLOYED_AT]
    post = [f for f, c in rows if c >= _FIX81_DEPLOYED_AT]

    print("=" * 72)
    _report("FULL WINDOW:", all_flags)
    print()
    _report(f"PRE-#81  (before {_FIX81_DEPLOYED_AT:%Y-%m-%d %H:%M} UTC):", pre)
    print()
    _report(f"POST-#81 (since {_FIX81_DEPLOYED_AT:%Y-%m-%d %H:%M} UTC):", post)
    print(
        "\n  feas% is the prerequisite to any win-rate: a phantom solution can never\n"
        "  be a win. If POST-#81 feas% is still ≈0, the approve fix alone did not\n"
        "  unblock execution — order-validity (filled / presig / fundability) and the\n"
        "  not-a-solver artifact dominate. Do NOT pitch feas% to CoW.\n"
    )


_GENERIC_REASON = "execution reverted (generic / pre-fix / empty-data)"


def _normalise_reason(reason: str | None) -> str:
    """Group key for a stored revert_reason.

    Strips the generic ``execution reverted`` prefix so the real detail — now
    decoded from ``err.data`` (f283c27) — becomes the group key (e.g. "STF",
    "Too little received", "GPv2: invalid signature"). Rows captured before that
    fix (or genuine empty-data reverts) collapse to a single generic bucket.
    """
    if not reason:
        return "(none)"
    text = reason.strip()
    prefix = "execution reverted"
    if text.lower().startswith(prefix):
        rest = text[len(prefix):].lstrip(": ").strip()
        return rest if rest else _GENERIC_REASON
    return text


@dataclass
class ReasonAgg:
    reason: str
    n: int = 0
    surplus_wei: int = 0


async def run_reasons(days: int, limit: int, strategy: str) -> None:
    """Offline, surplus-weighted tally of stored revert reasons (no RPC).

    Reads the payoff of the err.data decode fix: groups every infeasible solution
    by its real revert reason and ranks by claimed surplus, so the fundable-revert
    tail's true cause (deadline / Too little received / signature) is visible
    instead of the generic "execution reverted". NOT filtered to bare reverts —
    that filter is exactly what this fix dissolves.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    print(
        f"\nRevert-reason tally (offline, surplus-weighted)  "
        f"(last {days} day(s), since {since:%Y-%m-%d %H:%M} UTC) [strategy={strategy}]"
    )

    Session = get_session_factory()
    async with Session() as session:
        q = (
            select(ShadowSolution.revert_reason, ShadowSolution.our_score_wei)
            .where(ShadowSolution.strategy == strategy)
            .where(ShadowSolution.feasible.is_(False))
            .where(ShadowSolution.created_at >= since)
            .order_by(ShadowSolution.created_at.desc())
            .limit(limit)
        )
        rows = (await session.execute(q)).all()

    print(f"  matched {len(rows)} infeasible {strategy} solution(s)\n")
    if not rows:
        print("  Nothing to probe in window. Widen --days / --limit or check strategy.\n")
        return

    aggs: dict[str, ReasonAgg] = {}
    for reason, score_wei in rows:
        key = _normalise_reason(reason)
        agg = aggs.get(key)
        if agg is None:
            agg = ReasonAgg(reason=key)
            aggs[key] = agg
        agg.n += 1
        agg.surplus_wei += int(score_wei) if score_wei is not None else 0

    total_n = sum(a.n for a in aggs.values())
    total_surplus = sum(a.surplus_wei for a in aggs.values())
    ordered = sorted(aggs.values(), key=lambda a: (-a.surplus_wei, -a.n))

    print(f"  {'reason':<46} {'n':>4} {'n%':>6} {'surplus_eth':>14} {'sur%':>6}")
    print("  " + "-" * 80)
    for a in ordered:
        n_pct = 100 * a.n / total_n if total_n else 0.0
        s_pct = 100 * a.surplus_wei / total_surplus if total_surplus else 0.0
        label = a.reason if len(a.reason) <= 45 else a.reason[:44] + "…"
        print(
            f"  {label:<46} {a.n:>4} {n_pct:>5.1f}% "
            f"{a.surplus_wei / 1e18:>14.6f} {s_pct:>5.1f}%"
        )

    generic = aggs.get(_GENERIC_REASON)
    n_generic = generic.n if generic else 0
    n_decoded = total_n - n_generic
    print("\n" + "=" * 72)
    print("Summary")
    print("=" * 72)
    print(f"  {total_n} infeasible solution(s), {len(aggs)} distinct reason(s)")
    print(
        f"  decoded reasons: {n_decoded}/{total_n}  ·  still-generic: {n_generic}/{total_n}\n"
        f"  total claimed surplus: {total_surplus / 1e18:.6f} ETH"
    )
    if n_decoded == 0:
        print(
            "\n  All rows are still generic → the err.data decode fix (f283c27) has not\n"
            "  yet captured fresh reverts in this window. Re-run after solutions are\n"
            "  validated post-deploy (going-forward only; no backfill)."
        )
    else:
        print(
            "\n  Decoded reasons present → the fundable-revert tail's true cause is now\n"
            "  visible. Read the top surplus rows: 'Too little received' → router-vs-\n"
            "  quoter delivery gap; 'Transaction too old' → deadline; signature/STF →\n"
            "  encoding. Do NOT pitch feas% to CoW — this only identifies the lever.\n"
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
        choices=("offline", "requote", "delivery", "quantify", "reasons", "feasrate"),
        default="offline",
        help=(
            "offline (default): reconstruct GPv2 limit/conservation math from stored "
            "rows, no RPC, full-window. requote: re-quote each swap at the sim-block. "
            "delivery: router-vs-quoter swap delivery + counterparty balance/allowance "
            "check at the sim-block (use --minutes). quantify: aggregate the bare-revert "
            "tail by DISTINCT order over --days — recurrence, claimed-surplus "
            "concentration, ghost-set cross-ref, and a per-distinct-order fundability "
            "spot-check. reasons: offline, surplus-weighted tally of the decoded "
            "revert_reason strings over --days (reads the err.data-decode payoff). "
            "feasrate: offline feasible True/False/None breakdown over --days, split "
            "at the #81 approve-fix deploy (the prerequisite to any win-rate). "
            "requote/delivery are bounded by the ~100-min non-archive RPC window."
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
    elif args.mode == "quantify":
        asyncio.run(
            run_quantify(days=args.days, limit=args.limit, strategy=args.strategy)
        )
    elif args.mode == "reasons":
        asyncio.run(
            run_reasons(days=args.days, limit=args.limit, strategy=args.strategy)
        )
    elif args.mode == "feasrate":
        asyncio.run(
            run_feasrate(days=args.days, limit=args.limit, strategy=args.strategy)
        )
    elif args.mode == "delivery":
        asyncio.run(
            run_delivery(minutes=args.minutes, limit=args.limit, strategy=args.strategy)
        )
    else:
        asyncio.run(
            run_requote(minutes=args.minutes, limit=args.limit, strategy=args.strategy)
        )


if __name__ == "__main__":
    main()
