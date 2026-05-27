"""GPv2 Interaction primitive.

A CoW settlement carries three lists of interactions — pre, intra, post —
executed around the user-token transfer phase of ``GPv2Settlement.settle``.
This module defines the dataclass and the JSON serialisation that lands in
the Solution we return to the CoW driver.

The on-chain Solidity struct:

    struct Interaction {
        address target;
        uint256 value;
        bytes   callData;
    }

The wire format the CoW driver expects:

    { "target": "0x…", "value": "<decimal-string>", "callData": "0x…" }

We keep the Python representation strongly typed (``bytes``, ``int``) and
convert to the CoW-driver wire shape only at serialisation time.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Interaction:
    """A single GPv2 interaction call.

    ``target`` is a 0x-prefixed checksum address. ``value`` is wei (0 for
    pure ERC-20 paths). ``call_data`` carries the ABI-encoded selector +
    arguments as raw bytes — never include the 0x prefix; serialisation
    adds it.
    """

    target: str
    value: int
    call_data: bytes

    def __post_init__(self) -> None:
        # A common error: passing a hex string instead of bytes for call_data.
        # Reject it loudly rather than silently writing the wrong wire format.
        if not isinstance(self.call_data, bytes):
            raise TypeError(
                f"call_data must be bytes, got {type(self.call_data).__name__}"
            )
        if self.value < 0:
            raise ValueError(f"value must be non-negative, got {self.value}")
        if not (self.target.startswith("0x") and len(self.target) == 42):
            raise ValueError(f"target must be a 0x-prefixed 20-byte address, got {self.target!r}")

    def to_gpv2_dict(self) -> dict[str, str]:
        """Serialise to the dict shape the CoW driver expects."""
        return {
            "target": self.target,
            "value": str(self.value),
            "callData": "0x" + self.call_data.hex(),
        }
