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

The wire format the CoW driver expects (solver-engine OpenAPI
``CustomInteraction`` — see docs/cow-spec/solver-engine-openapi.yml):

    {
        "kind": "custom",
        "target": "0x…",
        "value": "<decimal-string>",
        "callData": "0x…",
        "inputs":  [{"token": "0x…", "amount": "<decimal-string>"}],
        "outputs": [{"token": "0x…", "amount": "<decimal-string>"}]
    }

``kind``/``inputs``/``outputs`` are REQUIRED by the spec — the driver
deserialises with serde and rejects any solution whose interaction omits
them. ``inputs``/``outputs`` declare the token flow the settlement provides
to / receives back from the interaction (a swap consumes ``token_in`` and
returns ``token_out``); the driver uses them for token-conservation
accounting and internalisation. An interaction that moves no tokens (e.g.
an ``approve``) carries empty ``inputs``/``outputs``.

We keep the Python representation strongly typed (``bytes``, ``int``) and
convert to the CoW-driver wire shape only at serialisation time.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Interaction:
    """A single GPv2 interaction call.

    ``target`` is a 0x-prefixed checksum address. ``value`` is wei (0 for
    pure ERC-20 paths). ``call_data`` carries the ABI-encoded selector +
    arguments as raw bytes — never include the 0x prefix; serialisation
    adds it. ``inputs``/``outputs`` are ``(token, amount)`` token flows the
    settlement provides to / receives from this interaction (empty for
    interactions that move no tokens, e.g. ``approve``).
    """

    target: str
    value: int
    call_data: bytes
    inputs: tuple[tuple[str, int], ...] = field(default=())
    outputs: tuple[tuple[str, int], ...] = field(default=())

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

    def to_gpv2_dict(self) -> dict[str, object]:
        """Serialise to the ``CustomInteraction`` wire shape the driver expects.

        Emits the spec-required ``kind``/``inputs``/``outputs`` alongside
        ``target``/``value``/``callData``. Return type is ``dict[str, object]``
        (not ``dict[str, str]``) so the result is assignment-compatible with
        ``Solution.interactions``, whose Pydantic field declares the same
        variance-friendly type.
        """
        return {
            "kind": "custom",
            "target": self.target,
            "value": str(self.value),
            "callData": "0x" + self.call_data.hex(),
            "inputs": [{"token": t, "amount": str(a)} for t, a in self.inputs],
            "outputs": [{"token": t, "amount": str(a)} for t, a in self.outputs],
        }
