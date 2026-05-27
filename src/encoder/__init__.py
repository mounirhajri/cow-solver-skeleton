"""On-chain calldata encoding for GPv2 settlement interactions.

The encoder turns a routing decision (Quote from a LiquiditySource) into the
raw bytes that GPv2Settlement.settle() invokes during the intra-interaction
stage. This module owns the wire-format concerns — ABI selectors, packed
paths, struct encoding — that the rest of the solver shouldn't touch.
"""

from src.encoder.interactions import Interaction

__all__ = ["Interaction"]
