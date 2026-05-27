"""Parallel quote fan-out across multiple LiquiditySource instances.

The aggregator owns the cross-source decision: query every source for the
same SwapRequest, collect their quotes, pick the one that maximises user
utility, and return both the winning Quote and the source that produced
it so the caller can call ``source.encode_interaction(quote, …)`` without
having to track which is which.

Defensive against the Protocol's "never raise" contract: a misbehaving
source that does raise must not kill the whole batch. ``return_exceptions``
sweeps any exception into a logged warning and drops that source from
this round.
"""

from __future__ import annotations

import asyncio

from src.liquidity.base import LiquiditySource, Quote, SwapRequest
from src.log import get_logger

log = get_logger(__name__)


class LiquidityAggregator:
    """Aggregates Quotes across configured LiquiditySource instances.

    Sources are queried in parallel via ``asyncio.gather``. Each source
    respects its own ``timeout_ms``; the aggregator does not impose a
    second-level timeout — it just collects whatever returned within the
    window each source was given.
    """

    def __init__(self, sources: list[LiquiditySource]) -> None:
        if not sources:
            raise ValueError("LiquidityAggregator needs at least one source")
        self._sources = list(sources)

    @property
    def sources(self) -> list[LiquiditySource]:
        return list(self._sources)

    async def best_quote(
        self, req: SwapRequest, timeout_ms: int
    ) -> tuple[Quote, LiquiditySource] | None:
        """Fan out, return the best ``(Quote, source)`` pair or ``None``.

        "Best" depends on kind:
          - ``sell``: maximise ``buy_amount`` (more output for fixed input)
          - ``buy``:  minimise ``sell_amount`` (less input for fixed output)

        Returns ``None`` if every source declined (no liquidity, timeout,
        error). Callers should treat this as "no AMM route exists for this
        order" and fall back to NoSolution or another strategy.
        """
        results = await asyncio.gather(
            *(self._safe_quote(s, req, timeout_ms) for s in self._sources),
            return_exceptions=False,
        )
        viable = [
            (q, s) for q, s in zip(results, self._sources, strict=True) if q is not None
        ]
        if not viable:
            return None
        if req.kind == "sell":
            return max(viable, key=lambda r: r[0].buy_amount)
        return min(viable, key=lambda r: r[0].sell_amount)

    async def _safe_quote(
        self, source: LiquiditySource, req: SwapRequest, timeout_ms: int
    ) -> Quote | None:
        """Wrap source.quote so a buggy raise doesn't kill the fan-out.

        The Protocol forbids raising — sources must return ``None`` on
        failure — but we cannot fully trust third-party implementations.
        A swallowed-and-logged exception is much cheaper than a corrupted
        batch result.
        """
        try:
            return await source.quote(req, timeout_ms)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "liquidity_source_raised",
                source=source.name,
                error=str(e),
                error_type=type(e).__name__,
            )
            return None
