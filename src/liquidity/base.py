"""LiquiditySource Protocol — the contract every routing backend implements.

Each source knows two things:
  1. How to *quote* a SwapRequest (what would it cost, what's the output?)
  2. How to *encode* the on-chain take that realises the quote inside a
     CoW Protocol settlement.

The contract is intentionally narrow so V3, V2, 1inch-API, Bebop-RFQ etc. can
sit behind the same interface and be aggregated in parallel. See the module
docstring in src/liquidity/__init__.py for the broader rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from src.encoder.interactions import Interaction


SwapKind = Literal["sell", "buy"]


@dataclass(frozen=True)
class SwapRequest:
    """A single token-swap intent we want a source to quote.

    Mirrors a CoW Order's relevant fields but is decoupled — multiple
    LiquiditySources are queried with the same SwapRequest and we don't want
    Order-specific concerns (signing, expiry, fee policy) leaking into the
    routing layer.

    For ``kind == "sell"``: sell_amount is fixed, the source returns the
    achievable buy_amount. For ``kind == "buy"``: buy_amount is fixed, the
    source returns the required sell_amount.
    """

    sell_token: str
    buy_token: str
    sell_amount: int
    buy_amount: int
    kind: SwapKind
    chain_id: int

    def __post_init__(self) -> None:
        # Catch caller mistakes early. A 0-amount on the *fixed* side of the
        # swap is almost always a bug; sources will either return None or
        # produce a divide-by-zero downstream. Better to fail loudly here.
        if self.kind == "sell" and self.sell_amount <= 0:
            raise ValueError(f"sell kind requires positive sell_amount, got {self.sell_amount}")
        if self.kind == "buy" and self.buy_amount <= 0:
            raise ValueError(f"buy kind requires positive buy_amount, got {self.buy_amount}")
        if self.sell_token.lower() == self.buy_token.lower():
            raise ValueError("sell_token and buy_token must differ")


@dataclass(frozen=True)
class Quote:
    """A source's offer to fulfil a SwapRequest.

    ``valid_until`` is the unix timestamp the quote stops being honoured.
    For AMM sources (V3, V2) this is set to a far-future sentinel — pool
    state can shift block-to-block but there is no explicit expiry. For
    RFQ / intent sources it's the maker-signed deadline and is real:
    encoding a take after this timestamp will revert on-chain.

    ``route_metadata`` is a source-specific dict carrying everything the
    same source's ``encode_interaction`` needs to build the call (e.g. fee
    tier for V3, packed path for multi-hop, signed payload for RFQ). Treat
    it as opaque outside the source that produced it.

    ``gas_estimate`` is the source's best guess at intra-interaction gas
    cost, used by the solver's surplus/cost trade-off. Whole transaction
    gas (signature checks, vault transfers) is the orchestrator's concern.
    """

    source: str
    sell_amount: int
    buy_amount: int
    valid_until: int
    route_metadata: dict[str, Any] = field(default_factory=dict)
    gas_estimate: int = 0


class LiquiditySource(Protocol):
    """Pluggable backend that quotes and encodes one liquidity venue.

    Implementations live in sibling modules (``v3.py``, future ``v2.py``,
    ``oneinch.py``, etc). The solver assembles them via LiquidityAggregator,
    not by importing them directly — adding a source is a one-line wiring
    change.
    """

    name: str

    async def quote(self, req: SwapRequest, timeout_ms: int) -> Quote | None:
        """Return a Quote or None on failure / timeout / no liquidity.

        Implementations MUST return within ``timeout_ms`` or return None.
        Never raise on timeout — the aggregator fan-out relies on None
        meaning "skip this source" without exception handling.
        """
        ...

    def encode_interaction(
        self, quote: Quote, recipient: str
    ) -> "Interaction":
        """Build the on-chain call that realises ``quote`` for ``recipient``.

        ``recipient`` is the address that receives the buy_token output —
        always the GPv2 Settlement contract during CoW settlement. The
        source is responsible for any selector, calldata, and value field.
        """
        ...

    def required_allowances(self, quote: Quote) -> list[tuple[str, str]]:
        """Return ``(token, spender)`` pairs that must be approved before
        the encoded interaction can succeed.

        The orchestrator's pre-flight check ensures every returned pair has
        a sufficient on-chain allowance from GPv2Settlement. Sources that
        don't move user tokens (peer-to-peer matches) should return ``[]``.
        """
        ...

    async def health_check(self) -> bool:
        """Lightweight liveness probe.

        Used by the aggregator's circuit breaker — a source that fails
        health checks repeatedly is temporarily excluded from quote fan-out
        to avoid burning the per-batch latency budget on something broken.
        """
        ...
