"""Per-token outcome extraction for classifier training data.

For each auction, derives one row per token observed in the orders, marking
whether it appeared in the winner's clearing prices and/or in our solution's.
"""

from __future__ import annotations

from typing import Any


def extract_token_outcomes(
    auction: dict[str, Any],
    winner_solution: dict[str, Any] | None,
    our_solution: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return a list of token outcome dicts ready for ORM insertion.

    Each dict has: token_address (lowercased), appeared_in_winner,
    appeared_in_ours, caused_revert (always False here; reconciler fills it
    in Phase 4).
    """
    tokens: set[str] = set()
    # Primary source: per-order tokens (when orders were fetched)
    for order in auction.get("orders", []):
        sell = order.get("sellToken") or order.get("sell_token")
        buy = order.get("buyToken") or order.get("buy_token")
        if sell:
            tokens.add(sell.lower())
        if buy:
            tokens.add(buy.lower())
    # Fallback source: auction.tokens dict (present when poller built a skeleton
    # payload for a large auction without fetching individual orders).
    for token_addr in auction.get("tokens", {}):
        if isinstance(token_addr, str):
            tokens.add(token_addr.lower())

    # CoW API uses "clearingPrices" (camelCase); our solver returns "prices".
    # Accept both, plus the orders array as additional signal.
    def _winner_tokens(sol: dict[str, Any] | None) -> set[str]:
        if not sol:
            return set()
        toks: set[str] = set()
        for key in ("prices", "clearingPrices"):
            for k in sol.get(key, {}) or {}:
                if isinstance(k, str):
                    toks.add(k.lower())
        # Fall back to orders' tokens if no clearing prices recorded
        for o in sol.get("orders", []) or []:
            if isinstance(o, dict):
                for tk_key in ("sellToken", "buyToken", "sell_token", "buy_token"):
                    val = o.get(tk_key)
                    if isinstance(val, str):
                        toks.add(val.lower())
        return toks

    winner_tokens = _winner_tokens(winner_solution)
    ours_tokens = _winner_tokens(our_solution)

    return [
        {
            "token_address": t,
            "appeared_in_winner": t in winner_tokens,
            "appeared_in_ours": t in ours_tokens,
            "caused_revert": False,
        }
        for t in sorted(tokens)
    ]
