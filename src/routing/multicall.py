"""Multicall3 aggregator: batch many eth_calls into one RPC round-trip.

Saves N-1 round-trips vs sequential eth_call. Allows per-call failure
isolation via aggregate3.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_abi import decode, encode

from src.log import get_logger
from src.routing.rpc import RpcClient

log = get_logger(__name__)

MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"
# aggregate3((address target, bool allowFailure, bytes callData)[])
AGGREGATE3_SELECTOR = "82ad56cb"

# Gas-cap overflow markers. A QuoterV2 call SIMULATES the swap, so a quote over
# a deep tick range can cost millions of gas; a batch can blow the provider's
# eth_call gas cap and revert the WHOLE call. We key on gas-specific phrasings
# rather than the bare JSON-RPC ``-32000`` code (that code is the generic
# "server error", also used for plain reverts / "header not found"), so we
# never misclassify a non-gas failure as overflow. Rate-limit codes
# (-32005 / 429) never reach here — rpc.eth_call retries them internally.
_GAS_OVERFLOW_MARKERS = ("out of gas", "gas required exceeds", "intrinsic gas", "gas limit")


def _is_gas_overflow(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(marker in s for marker in _GAS_OVERFLOW_MARKERS)


@dataclass(frozen=True)
class Call:
    target: str
    call_data: str  # 0x-prefixed hex
    allow_failure: bool = True


@dataclass(frozen=True)
class CallResult:
    success: bool
    return_data: bytes


class Multicall3:
    def __init__(self, rpc: RpcClient, address: str = MULTICALL3_ADDRESS) -> None:
        self.rpc = rpc
        self.address = address

    async def aggregate(self, calls: list[Call]) -> list[CallResult]:
        if not calls:
            return []
        tuples = [
            (
                self._to_checksum(c.target),
                c.allow_failure,
                bytes.fromhex(c.call_data[2:] if c.call_data.startswith("0x") else c.call_data),
            )
            for c in calls
        ]
        encoded_args = encode(["(address,bool,bytes)[]"], [tuples])
        data = "0x" + AGGREGATE3_SELECTOR + encoded_args.hex()
        result_hex = await self.rpc.eth_call(self.address, data)
        # Decode (bool, bytes)[]
        raw = bytes.fromhex(result_hex[2:] if result_hex.startswith("0x") else result_hex)
        (decoded,) = decode(["(bool,bytes)[]"], raw)
        return [CallResult(success=bool(d[0]), return_data=bytes(d[1])) for d in decoded]

    async def aggregate_resilient(self, calls: list[Call]) -> list[CallResult]:
        """``aggregate`` that survives a node gas-cap overflow.

        A single QuoterV2 call over a deep tick range can cost millions of gas,
        so a batch can blow the provider's eth_call cap and revert the ENTIRE
        call with ``-32000 out of gas``. Rather than let that abort the caller
        (e.g. the whole router quote pass), recursively bisect the batch so only
        the individual over-cap call is dropped — returned as
        ``CallResult(success=False, b"")`` (→ amount_out 0 downstream) — while
        every other call still resolves. Results stay positionally aligned with
        ``calls`` (contiguous slices + ordered concat), so callers that
        ``zip(..., strict=True)`` keep their correlation. Non-gas errors are
        re-raised unchanged — fanning out a real outage would just multiply the
        failing round-trips.
        """
        if not calls:
            return []
        try:
            return await self.aggregate(calls)
        except Exception as exc:  # noqa: BLE001
            if not _is_gas_overflow(exc):
                raise
            if len(calls) == 1:
                log.warning("multicall_call_dropped_out_of_gas", target=calls[0].target)
                return [CallResult(success=False, return_data=b"")]
            mid = len(calls) // 2
            left = await self.aggregate_resilient(calls[:mid])
            right = await self.aggregate_resilient(calls[mid:])
            return left + right

    @staticmethod
    def _to_checksum(address: str) -> str:
        # eth_abi expects raw lowercase addresses; checksumming is optional but harmless.
        return address.lower()
