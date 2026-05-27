"""Pluggable liquidity-source layer for the solver.

A LiquiditySource takes a SwapRequest (user wants to swap A for B) and returns
a Quote describing how *this* source would fulfill it, plus a method to encode
the on-chain interaction that realises the swap inside a CoW settlement.

Phase 0b ships only V3Source. The Protocol is the foundation for adding
external aggregator and RFQ sources in later phases without touching the
solver core.
"""

from src.liquidity.base import LiquiditySource, Quote, SwapKind, SwapRequest

__all__ = ["LiquiditySource", "Quote", "SwapKind", "SwapRequest"]
