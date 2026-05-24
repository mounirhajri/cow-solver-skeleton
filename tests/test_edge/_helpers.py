"""Shared test helpers for tests/test_edge/.

Keep this file free of `edge.*` imports at module scope — conftest.py skips the
whole directory when the edge submodule is absent, but `pytest --collect-only`
in some configurations still parses helper modules.
"""
from src.models.order import Order


def mk_partial_order(
    uid: str,
    sell_token: str,
    buy_token: str,
    sell_amount: int,
    buy_amount: int,
    partially_fillable: bool = True,
) -> Order:
    """Create a sell-kind Order with an explicit partiallyFillable flag.

    Used by the partial-fill tests in test_surplus_lp.py and test_multi_party.py.
    """
    return Order(
        uid=uid,
        sellToken=sell_token,
        buyToken=buy_token,
        sellAmount=sell_amount,
        buyAmount=buy_amount,
        feePolicies=[],
        validTo=99,
        kind="sell",
        owner="0x" + "a" * 40,
        partiallyFillable=partially_fillable,
        **{"class": "limit"},
    )
