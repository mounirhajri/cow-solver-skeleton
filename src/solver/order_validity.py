"""Solve-time order-validity pre-filter for router candidates.

The feasibility work proved that the dominant phantom causes are not bad
clearing math but DEAD ORDERS we keep re-picking: already filled (or
on-chain cancelled), presign orders whose pre-signature never landed, and
owners who can't fund the sell side (no balance / no vault-relayer
allowance). The delivery probe showed the same 1-2 zombie orders being
re-solved every auction because they sit at the top of the surplus sort.

This module checks a small candidate set (the router's top-K) with ONE
Multicall3 batch of cheap storage reads — RPC budget stays flat:

  - ``GPv2Settlement.filledAmount(uid)``  → already filled / cancelled
    (GPv2 cancellation sets filledAmount to uint256.max, so a plain
    ``> 0`` check catches both for fill-or-kill orders).
  - ``GPv2Settlement.preSignature(uid)``  → only for ``presign`` orders;
    zero means the on-chain pre-signature never landed.
  - ``IERC20(sellToken).balanceOf(owner)`` and
    ``allowance(owner → GPv2VaultRelayer)`` → settle() pulls sell funds
    through the relayer BEFORE any swap; an owner short on either reverts
    the whole settlement. Only checked for plain ``erc20`` sell balance —
    vault-funded orders (external/internal) skip these rather than risk a
    false drop.

Block discipline matters: the internal shadow poller solves auctions
POST-settlement, so at ``latest`` the orders the real winner just filled
would read as "filled" and we would filter away exactly the orders whose
feasibility we can measure. Callers must pass the auction's
``simulation_block`` when present (the poller flow); the live driver flow
has none → ``latest`` is then correct.

FAIL-OPEN everywhere: an RPC error, a reverted view call, or a decode
hiccup keeps the order. Losing a solvable order to an infra blip is worse
than occasionally re-quoting a dead one — the post-hoc feasibility
validator still labels whatever slips through.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_abi import encode

from src.log import get_logger
from src.models.order import Order
from src.routing.multicall import Call, Multicall3

log = get_logger(__name__)

# Canonical GPv2VaultRelayer — the same address on every chain CoW supports.
# settle() pulls each trade's sell tokens through this contract, so the
# owner's ERC-20 allowance must be granted to IT (not to the settlement).
GPV2_VAULT_RELAYER = "0xC92E8bdf79f0507f65a392b0ab4667716BFE0110"

# keccak4 selectors, pinned against eth_utils.keccak in the unit tests.
FILLED_AMOUNT_SELECTOR = "2479fb6e"   # filledAmount(bytes)
PRE_SIGNATURE_SELECTOR = "d08d33d1"   # preSignature(bytes)
BALANCE_OF_SELECTOR = "70a08231"      # balanceOf(address)
ALLOWANCE_SELECTOR = "dd62ed3e"       # allowance(address,address)

_UINT_MAX_BYTES = 32


def _uid_bytes(uid: str) -> bytes:
    return bytes.fromhex(uid[2:] if uid.startswith("0x") else uid)


def _encode_uid_call(selector: str, uid: str) -> str:
    return "0x" + selector + encode(["bytes"], [_uid_bytes(uid)]).hex()


def _encode_balance_of(owner: str) -> str:
    return "0x" + BALANCE_OF_SELECTOR + encode(["address"], [owner.lower()]).hex()


def _encode_allowance(owner: str, spender: str) -> str:
    return "0x" + ALLOWANCE_SELECTOR + encode(
        ["address", "address"], [owner.lower(), spender.lower()]
    ).hex()


def _decode_uint(data: bytes) -> int | None:
    """uint256 from a raw 32-byte return; None when the call returned junk."""
    if len(data) < _UINT_MAX_BYTES:
        return None
    return int.from_bytes(data[:_UINT_MAX_BYTES], "big")


@dataclass(frozen=True)
class _Check:
    """One (order, kind-of-check) tag, positionally aligned with the calls."""

    uid: str
    kind: str  # "filled" | "presig" | "balance" | "allowance"


def _build_checks(
    orders: list[Order], settlement_addr: str, vault_relayer: str
) -> tuple[list[Call], list[_Check]]:
    calls: list[Call] = []
    checks: list[_Check] = []
    for o in orders:
        calls.append(Call(
            target=settlement_addr,
            call_data=_encode_uid_call(FILLED_AMOUNT_SELECTOR, o.uid),
            allow_failure=True,
        ))
        checks.append(_Check(o.uid, "filled"))
        if (o.signing_scheme or "").lower() == "presign":
            calls.append(Call(
                target=settlement_addr,
                call_data=_encode_uid_call(PRE_SIGNATURE_SELECTOR, o.uid),
                allow_failure=True,
            ))
            checks.append(_Check(o.uid, "presig"))
        # Funding checks only when wallet state is authoritative (plain erc20
        # sell balance). preInteractions exempt ONLY the allowance check: a
        # permit hook can mint the ALLOWANCE at settlement, but nothing mints
        # BALANCE out of thin air — and the measured zombie class (composable
        # orders with permanent balance 0, re-solved hundreds of times/day,
        # 2026-06-11) slipped through precisely via a blanket preInteractions
        # exemption. Balance is always checked.
        if o.sell_token_balance.lower() == "erc20":
            calls.append(Call(
                target=o.sell_token,
                call_data=_encode_balance_of(o.owner),
                allow_failure=True,
            ))
            checks.append(_Check(o.uid, "balance"))
            if not o.pre_interactions:
                calls.append(Call(
                    target=o.sell_token,
                    call_data=_encode_allowance(o.owner, vault_relayer),
                    allow_failure=True,
                ))
                checks.append(_Check(o.uid, "allowance"))
    return calls, checks


def _required_funding(order: Order, filled: int | None) -> int:
    """The minimum sell-token funding below which WE provably cannot fill.

    Sell fill-or-kill: the full ``sell_amount`` minus anything already
    filled (filledAmount is in sell units). Sell partials: the router quotes
    down to a 0.5× fraction, so funding for the SMALLEST fraction we would
    execute suffices — requiring the full remainder would falsely drop
    orders our own solver can partially fill. Buy fill-or-kill: the limit
    ``sell_amount`` (worst-case pull). Buy partials: skipped entirely
    (remaining sell is proportional and not worth a false drop).
    """
    if order.kind == "sell":
        f = min(filled or 0, order.sell_amount)
        remaining = order.sell_amount - f
        if order.partially_fillable:
            return min(remaining, order.sell_amount // 2)
        return remaining
    if not order.partially_fillable:
        return order.sell_amount
    return 0  # buy partial — skip funding checks


async def filter_valid_orders(
    multicall: Multicall3,
    orders: list[Order],
    *,
    settlement_addr: str,
    vault_relayer: str = GPV2_VAULT_RELAYER,
    block: str = "latest",
) -> tuple[list[Order], dict[str, str]]:
    """Drop candidates that provably cannot settle. Never raises.

    Returns ``(kept_orders, dropped: uid → reason)``. Order of ``kept`` is
    preserved. On ANY infrastructure failure the full input is returned
    (fail-open) — this filter may only ever remove orders it has positive
    on-chain evidence against.
    """
    if not orders:
        return [], {}

    # The ENTIRE body is fail-open: call building (a malformed uid must not
    # crash the solve), the RPC round-trip, and decoding. This filter may only
    # ever act on positive on-chain evidence — anything else keeps the order.
    try:
        calls, checks = _build_checks(orders, settlement_addr, vault_relayer)
        results = await multicall.aggregate_resilient(calls, block=block)

        # Collect per-uid readings; a reverted/short call leaves the reading
        # None → that individual check is skipped (per-check fail-open).
        readings: dict[str, dict[str, int | None]] = {o.uid: {} for o in orders}
        for check, result in zip(checks, results, strict=True):
            value = _decode_uint(result.return_data) if result.success else None
            readings[check.uid][check.kind] = value

        by_uid = {o.uid: o for o in orders}
        dropped: dict[str, str] = {}
        for uid, r in readings.items():
            order = by_uid[uid]
            filled = r.get("filled")
            target = order.sell_amount if order.kind == "sell" else order.buy_amount
            # GPv2 cancellation sets filledAmount to uint256.max, so this also
            # catches on-chain-cancelled orders.
            if (
                filled is not None
                and filled > 0
                and (not order.partially_fillable or filled >= target)
            ):
                dropped[uid] = "filled"
                continue
            presig = r.get("presig")
            if presig is not None and presig == 0:
                dropped[uid] = "presignature_missing"
                continue
            required = _required_funding(order, filled)
            if required > 0:
                balance = r.get("balance")
                if balance is not None and balance < required:
                    dropped[uid] = "insufficient_balance"
                    continue
                allowance = r.get("allowance")
                if allowance is not None and allowance < required:
                    dropped[uid] = "insufficient_allowance"
                    continue

        kept = [o for o in orders if o.uid not in dropped]
        return kept, dropped
    except Exception as exc:  # noqa: BLE001
        log.warning("order_validity_filter_failed_open", error=str(exc), block=block)
        return list(orders), {}
