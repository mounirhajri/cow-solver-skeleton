"""Multicall3 aggregator: batch many eth_calls into one RPC round-trip.

Saves N-1 round-trips vs sequential eth_call. Allows per-call failure
isolation via aggregate3.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_abi import decode, encode

from src.routing.rpc import RpcClient

MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"
# aggregate3((address target, bool allowFailure, bytes callData)[])
AGGREGATE3_SELECTOR = "82ad56cb"


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

    @staticmethod
    def _to_checksum(address: str) -> str:
        # eth_abi expects raw lowercase addresses; checksumming is optional but harmless.
        return address.lower()
