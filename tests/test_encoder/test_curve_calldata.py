"""Tests for Curve stable-pool swap encoding (approve + exchange)."""
import pytest
from eth_abi import decode
from eth_utils import keccak

from src.encoder.curve_calldata import (
    EXCHANGE_SELECTOR,
    encode_curve_swap_interactions,
    encode_exchange,
)
from src.encoder.erc20 import APPROVE_SELECTOR

_POOL = "0x7f90122BF0700F9E7e1F688fe926940E8839F353"  # 2pool (Arbitrum)
_USDC_E = "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8"
_USDT = "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"


def test_exchange_selector_matches_keccak() -> None:
    """Pin the selector against keccak of the 4-arg Vyper signature.
    Both classic StableSwap and stableswap-NG declare a `_receiver`
    DEFAULT argument, so the 4-arg entry point exists on both and routes
    output to msg.sender (= the settlement). A 5-arg signature would be a
    different selector."""
    assert keccak(b"exchange(int128,int128,uint256,uint256)")[:4] == EXCHANGE_SELECTOR
    assert EXCHANGE_SELECTOR.hex() == "3df02124"


def test_encode_exchange_golden_roundtrip() -> None:
    cd = encode_exchange(1, 0, 100_000_000, 99_500_000)
    assert cd[:4] == EXCHANGE_SELECTOR
    i, j, dx, min_dy = decode(["int128", "int128", "uint256", "uint256"], cd[4:])
    assert (i, j, dx, min_dy) == (1, 0, 100_000_000, 99_500_000)


def test_encode_exchange_bounds_guards() -> None:
    with pytest.raises(ValueError, match="non-negative int128"):
        encode_exchange(-1, 0, 1, 0)
    with pytest.raises(ValueError, match="non-negative int128"):
        encode_exchange(0, 2**127, 1, 0)  # int128 overflow
    with pytest.raises(ValueError, match="must differ"):
        encode_exchange(1, 1, 1, 0)
    with pytest.raises(ValueError, match="dx must be positive"):
        encode_exchange(0, 1, 0, 0)
    with pytest.raises(ValueError, match="dx must be positive"):
        encode_exchange(0, 1, 2**256, 0)


def _interactions() -> list:  # type: ignore[type-arg]
    return encode_curve_swap_interactions(
        pool_address=_POOL,
        i=0,
        j=1,
        amount_in=100_000_000,
        min_amount_out=99_500_000,
        sell_token=_USDC_E,
        buy_token=_USDT,
        executed_buy=99_900_000,
    )


def test_interactions_shape_and_order() -> None:
    """[approve, exchange] — allowance must exist before the pool's
    transferFrom consumes it."""
    approve, exchange = _interactions()
    assert approve.target == _USDC_E  # approve is called ON the sell token
    assert approve.value == 0
    assert exchange.target == _POOL
    assert exchange.value == 0


def test_approve_golden_decode() -> None:
    """Approve grants the POOL exactly amount_in — no more."""
    approve, _ = _interactions()
    assert approve.call_data[:4] == APPROVE_SELECTOR
    spender, amount = decode(["address", "uint256"], approve.call_data[4:])
    assert spender == _POOL.lower()
    assert amount == 100_000_000
    # approve moves no tokens itself → empty declared flows.
    assert approve.inputs == ()
    assert approve.outputs == ()


def test_exchange_golden_decode() -> None:
    """All four exchange args decode back; min_dy is the slippage guard,
    present ONLY inside the calldata."""
    _, exchange = _interactions()
    assert exchange.call_data[:4] == EXCHANGE_SELECTOR
    i, j, dx, min_dy = decode(
        ["int128", "int128", "uint256", "uint256"], exchange.call_data[4:]
    )
    assert (i, j, dx, min_dy) == (0, 1, 100_000_000, 99_500_000)


def test_declared_flows_promise_executed_buy_not_minimum() -> None:
    """The DECLARED output must be the PROMISED executed_buy (driver
    solvency accounting), never the slippage-reduced min_amount_out —
    declaring the minimum makes the solution look insolvent to the driver
    (v4_calldata.py's hard-learned 2026-06-08 lesson)."""
    _, exchange = _interactions()
    assert exchange.inputs == ((_USDC_E, 100_000_000),)
    assert exchange.outputs == ((_USDT, 99_900_000),)  # executed_buy
    assert exchange.outputs[0][1] != 99_500_000  # NOT min_amount_out


def test_min_above_promise_rejected() -> None:
    """min_amount_out > executed_buy would revert on-chain even when the
    promise is met — reject at encode time."""
    with pytest.raises(ValueError, match="must not exceed executed_buy"):
        encode_curve_swap_interactions(
            pool_address=_POOL,
            i=0,
            j=1,
            amount_in=100,
            min_amount_out=101,
            sell_token=_USDC_E,
            buy_token=_USDT,
            executed_buy=100,
        )


def test_non_positive_executed_buy_rejected() -> None:
    with pytest.raises(ValueError, match="executed_buy must be positive"):
        encode_curve_swap_interactions(
            pool_address=_POOL,
            i=0,
            j=1,
            amount_in=100,
            min_amount_out=0,
            sell_token=_USDC_E,
            buy_token=_USDT,
            executed_buy=0,
        )


def test_gpv2_wire_shape() -> None:
    """The interactions serialise to the CustomInteraction wire shape the
    driver requires (kind/inputs/outputs present — 2026-06-08 conformance)."""
    approve, exchange = _interactions()
    a, e = approve.to_gpv2_dict(), exchange.to_gpv2_dict()
    assert a["kind"] == "custom" and e["kind"] == "custom"
    assert a["inputs"] == [] and a["outputs"] == []
    assert e["inputs"] == [{"token": _USDC_E, "amount": "100000000"}]
    assert e["outputs"] == [{"token": _USDT, "amount": "99900000"}]
    assert isinstance(e["callData"], str) and e["callData"].startswith("0x3df02124")
