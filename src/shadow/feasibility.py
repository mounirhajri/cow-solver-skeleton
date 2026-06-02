"""Prove a solver solution would clear on-chain.

Re-encodes a solution as a real GPv2Settlement.settle() using order signatures
backfilled from the public CoW orderbook, then eth_calls it at latest block.
A revert means the solution is phantom (token conservation or signature
failure); success means the score is commit-feasible.

Verdict.feasible:  True = feasible, False = phantom, None = unknown (infra gap;
never penalise the solution for our own fetch/encoding failure).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.encoder.settle import SettleTrade, encode_settle, encode_trade_flags
from src.log import get_logger

log = get_logger(__name__)

# GPv2 sentinel: a null/zero receiver in the SIGNED order means "pay the owner".
# The EIP-712 digest is signed over address(0), so we MUST re-encode address(0)
# (not the owner) or ecrecover yields the wrong signer and settle() reverts on a
# bad signature — which we'd misread as a phantom solution.
_ZERO_ADDRESS = "0x" + "00" * 20


@dataclass(frozen=True)
class Verdict:
    feasible: bool | None
    reason: str | None = None


@dataclass(frozen=True)
class FeasibilityGate:
    """Hard pre-submission feasibility check, bound to its RPC/API deps.

    Wraps :func:`validate_solution` with the long-lived dependencies the
    orchestrator already has on hand (order cache, CoW orderbook client, RPC
    client, contract addresses) so the hot path can ask a single question:
    "would this exact solution clear on-chain right now?".

    Always simulates against ``latest`` — for a live submission the only state
    that matters is the block we are about to settle into. The orchestrator
    submits a solution ONLY on a ``True`` verdict; ``False`` (phantom) and
    ``None`` (UNKNOWN/infra) both mean "do not submit", because a NoSolution is
    always safe whereas winning with a revert gets the solver slashed.
    """

    cache: Any
    api: Any
    rpc: Any
    settlement_addr: str
    solver_addr: str

    async def check(self, solution: dict[str, Any]) -> Verdict:
        """Validate one solution dict. Never raises (validate_solution swallows)."""
        return await validate_solution(
            solution,
            cache=self.cache,
            api=self.api,
            rpc=self.rpc,
            settlement_addr=self.settlement_addr,
            solver_addr=self.solver_addr,
            block="latest",
        )


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _app_data_bytes(order: dict[str, Any]) -> bytes:
    raw = order.get("appData") or order.get("appDataHash") or ("0x" + "00" * 32)
    h = raw[2:] if isinstance(raw, str) and raw.startswith("0x") else str(raw)
    b = bytes.fromhex(h)
    # appData is a bytes32: left-pad short values with zeros, and take the
    # right-most 32 bytes of any oversized value (CoW should never send oversized).
    return b.rjust(32, b"\x00")[:32]


def _sig_bytes(order: dict[str, Any]) -> bytes:
    raw = order.get("signature") or "0x"
    h = raw[2:] if raw.startswith("0x") else raw
    return bytes.fromhex(h)


async def _resolve_order(uid: str, cache: Any, api: Any) -> dict[str, Any] | None:
    cached: dict[str, Any] | None = await cache.get(uid)
    if cached is not None:
        return cached
    fetched: dict[str, Any] | None = await api.fetch_order(uid)
    if fetched is not None:
        await cache.set(uid, fetched)
    return fetched


async def validate_solution(
    solution: dict[str, Any],
    *,
    cache: Any,
    api: Any,
    rpc: Any,
    settlement_addr: str,
    solver_addr: str,
    block: str = "latest",
) -> Verdict:
    """Validate one solution's on-chain feasibility. Never raises.

    ``block`` selects the state the settle() is simulated against. Default
    ``"latest"`` measures post-settlement drift (orders already filled, pools
    moved) — fine for infra checks but NOT auction-time feasibility. Pass the
    competition simulation block (``hex(n)``) to evaluate the solution against
    the same state CoW used at auction time. If that block has aged out of the
    node's history, the RPC returns a ``missing trie node`` error (no "revert"
    in the message) → eth_call_capture raises → mapped to UNKNOWN, never a
    false phantom.
    """
    try:
        trades_in = [
            t for t in (solution.get("trades") or [])
            if t.get("kind") == "fulfillment"
        ]
        if not trades_in:
            return Verdict(None, "no fulfillment trades")

        # Build the token index map from clearing prices (deterministic order).
        prices = solution.get("prices") or {}
        tokens = sorted({k.lower() for k in prices})
        if len(tokens) != len(prices):
            return Verdict(None, "duplicate token keys (case-insensitive)")
        index = {tok: i for i, tok in enumerate(tokens)}
        clearing_prices = [_to_int(prices[next(
            k for k in prices if k.lower() == tok
        )]) for tok in tokens]

        settle_trades: list[SettleTrade] = []
        for t in trades_in:
            uid = (t.get("orderUid") or t.get("order_uid") or "").lower()
            order = await _resolve_order(uid, cache, api)
            if order is None:
                return Verdict(None, f"order {uid} unfetchable")
            sell_tok = (order.get("sellToken") or "").lower()
            buy_tok = (order.get("buyToken") or "").lower()
            if sell_tok not in index or buy_tok not in index:
                return Verdict(None, f"order {uid} token not in clearing prices")
            flags = encode_trade_flags(
                kind=str(order.get("kind") or "sell"),
                partially_fillable=bool(order.get("partiallyFillable")),
                signing_scheme=str(order.get("signingScheme") or "eip712"),
            )
            settle_trades.append(SettleTrade(
                sell_token_index=index[sell_tok],
                buy_token_index=index[buy_tok],
                receiver=order.get("receiver") or _ZERO_ADDRESS,
                sell_amount=_to_int(order.get("sellAmount")),
                buy_amount=_to_int(order.get("buyAmount")),
                valid_to=_to_int(order.get("validTo")),
                app_data=_app_data_bytes(order),
                fee_amount=_to_int(order.get("feeAmount")),
                flags=flags,
                executed_amount=_to_int(t.get("executedAmount")),
                signature=_sig_bytes(order),
            ))

        intra = []
        for ix in solution.get("interactions") or []:
            target = ix.get("target")
            value = _to_int(ix.get("value"))
            cd = ix.get("callData") or "0x"
            cd_bytes = bytes.fromhex(cd[2:] if cd.startswith("0x") else cd)
            intra.append((target, value, cd_bytes))

        calldata = encode_settle(
            tokens=tokens,
            clearing_prices=clearing_prices,
            trades=settle_trades,
            intra_interactions=intra,
        )
        ok, payload = await rpc.eth_call_capture(
            to=settlement_addr,
            data="0x" + calldata.hex(),
            from_addr=solver_addr,
            block=block,
        )
        return Verdict(True, None) if ok else Verdict(False, payload[:500])
    except Exception as exc:  # noqa: BLE001
        log.info("feasibility_validation_error", error=str(exc))
        return Verdict(None, f"validation error: {exc}")
