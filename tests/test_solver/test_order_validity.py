"""Tests for the solve-time order-validity pre-filter.

The filter may only drop an order on POSITIVE on-chain evidence (filled /
cancelled, missing presignature, un-fundable owner) — every infrastructure
failure path must fail OPEN and keep the order.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from eth_abi import decode
from eth_utils import keccak

from src.models.order import Order
from src.routing.multicall import Call, CallResult
from src.solver.order_validity import (
    ALLOWANCE_SELECTOR,
    BALANCE_OF_SELECTOR,
    FILLED_AMOUNT_SELECTOR,
    GPV2_VAULT_RELAYER,
    PRE_SIGNATURE_SELECTOR,
    filter_valid_orders,
)

SETTLEMENT = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"

_UINT_MAX = 2**256 - 1


def _uid(i: int) -> str:
    return "0x" + f"{i:02x}" * 56


def _order(
    i: int,
    *,
    kind: str = "sell",
    partially_fillable: bool = False,
    signing_scheme: str = "eip712",
    sell_token_balance: str = "erc20",
    sell_amount: int = 1_000_000,
) -> Order:
    return Order.model_validate({
        "uid": _uid(i),
        "sellToken": USDC,
        "buyToken": WETH,
        "sellAmount": str(sell_amount),
        "buyAmount": "300000000000000",
        "validTo": 2_000_000_000,
        "kind": kind,
        "owner": "0x" + f"{i:02x}" * 20,
        "partiallyFillable": partially_fillable,
        "class": "limit",
        "signingScheme": signing_scheme,
        "sellTokenBalance": sell_token_balance,
    })


def _fake_multicall(
    *,
    filled: dict[str, int] | None = None,
    presig: dict[str, int] | None = None,
    balance: dict[str, int] | None = None,    # keyed by owner (lowercase)
    allowance: dict[str, int] | None = None,  # keyed by owner (lowercase)
    record: dict[str, Any] | None = None,
) -> MagicMock:
    """A Multicall3 stand-in that decodes each call's calldata and answers
    from the configured maps (defaults: nothing filled, presig present,
    balance/allowance effectively infinite)."""

    async def agg(calls: list[Call], block: str = "latest") -> list[CallResult]:
        if record is not None:
            record["block"] = block
            record["calls"] = list(calls)
        out: list[CallResult] = []
        for c in calls:
            sel = c.call_data[2:10]
            tail = bytes.fromhex(c.call_data[10:])
            if sel == FILLED_AMOUNT_SELECTOR:
                (uid_b,) = decode(["bytes"], tail)
                v = (filled or {}).get("0x" + uid_b.hex(), 0)
            elif sel == PRE_SIGNATURE_SELECTOR:
                (uid_b,) = decode(["bytes"], tail)
                v = (presig or {}).get("0x" + uid_b.hex(), 1)
            elif sel == BALANCE_OF_SELECTOR:
                (owner,) = decode(["address"], tail)
                v = (balance or {}).get(owner.lower(), 10**30)
            elif sel == ALLOWANCE_SELECTOR:
                (owner, _spender) = decode(["address", "address"], tail)
                v = (allowance or {}).get(owner.lower(), 10**30)
            else:  # pragma: no cover - unexpected selector
                raise AssertionError(f"unexpected selector {sel}")
            out.append(CallResult(success=True, return_data=v.to_bytes(32, "big")))
        return out

    mc = MagicMock()
    mc.aggregate_resilient = agg
    return mc


def test_selectors_pinned_against_keccak() -> None:
    """A wrong selector would silently hit a non-existent function (revert →
    per-check fail-open → filter silently inert). Pin all four."""
    assert keccak(b"filledAmount(bytes)")[:4].hex() == FILLED_AMOUNT_SELECTOR
    assert keccak(b"preSignature(bytes)")[:4].hex() == PRE_SIGNATURE_SELECTOR
    assert keccak(b"balanceOf(address)")[:4].hex() == BALANCE_OF_SELECTOR
    assert keccak(b"allowance(address,address)")[:4].hex() == ALLOWANCE_SELECTOR


@pytest.mark.asyncio
async def test_healthy_order_kept() -> None:
    o = _order(1)
    kept, dropped = await filter_valid_orders(
        _fake_multicall(), [o], settlement_addr=SETTLEMENT
    )
    assert kept == [o]
    assert dropped == {}


@pytest.mark.asyncio
async def test_filled_fok_order_dropped() -> None:
    """Any nonzero filledAmount kills a fill-or-kill order — including the
    uint256.max GPv2 writes on cancellation."""
    o_filled = _order(1)
    o_cancelled = _order(2)
    o_live = _order(3)
    mc = _fake_multicall(filled={o_filled.uid: 1, o_cancelled.uid: _UINT_MAX})
    kept, dropped = await filter_valid_orders(
        mc, [o_filled, o_cancelled, o_live], settlement_addr=SETTLEMENT
    )
    assert kept == [o_live]
    assert dropped == {o_filled.uid: "filled", o_cancelled.uid: "filled"}


@pytest.mark.asyncio
async def test_partially_filled_partial_order_kept_fully_filled_dropped() -> None:
    half = _order(1, partially_fillable=True, sell_amount=1_000_000)
    full = _order(2, partially_fillable=True, sell_amount=1_000_000)
    mc = _fake_multicall(filled={half.uid: 500_000, full.uid: 1_000_000})
    kept, dropped = await filter_valid_orders(
        mc, [half, full], settlement_addr=SETTLEMENT
    )
    assert kept == [half]
    assert dropped == {full.uid: "filled"}


@pytest.mark.asyncio
async def test_presign_order_without_onchain_presignature_dropped() -> None:
    missing = _order(1, signing_scheme="presign")
    signed = _order(2, signing_scheme="presign")
    eoa = _order(3)  # eip712 — must not even be queried for presig
    record: dict[str, Any] = {}
    mc = _fake_multicall(presig={missing.uid: 0, signed.uid: 1}, record=record)
    kept, dropped = await filter_valid_orders(
        mc, [missing, signed, eoa], settlement_addr=SETTLEMENT
    )
    assert kept == [signed, eoa]
    assert dropped == {missing.uid: "presignature_missing"}
    presig_calls = [
        c for c in record["calls"] if c.call_data[2:10] == PRE_SIGNATURE_SELECTOR
    ]
    assert len(presig_calls) == 2  # only the two presign orders


@pytest.mark.asyncio
async def test_unfundable_owner_dropped() -> None:
    no_balance = _order(1, sell_amount=1_000_000)
    no_allowance = _order(2, sell_amount=1_000_000)
    funded = _order(3, sell_amount=1_000_000)
    mc = _fake_multicall(
        balance={no_balance.owner.lower(): 0},
        allowance={no_allowance.owner.lower(): 999_999},
    )
    kept, dropped = await filter_valid_orders(
        mc, [no_balance, no_allowance, funded], settlement_addr=SETTLEMENT
    )
    assert kept == [funded]
    assert dropped == {
        no_balance.uid: "insufficient_balance",
        no_allowance.uid: "insufficient_allowance",
    }


@pytest.mark.asyncio
async def test_partial_sell_funding_checked_against_remaining() -> None:
    """A half-filled partial sell only needs funds for the REMAINING half."""
    o = _order(1, partially_fillable=True, sell_amount=1_000_000)
    mc = _fake_multicall(
        filled={o.uid: 600_000},
        balance={o.owner.lower(): 400_000},   # exactly the remaining 400k
        allowance={o.owner.lower(): 400_000},
    )
    kept, dropped = await filter_valid_orders(mc, [o], settlement_addr=SETTLEMENT)
    assert kept == [o]
    assert dropped == {}


@pytest.mark.asyncio
async def test_permit_hook_order_skips_funding_checks() -> None:
    """An order with preInteractions (e.g. an EIP-2612 permit hook) may only
    receive its allowance AT settlement — solve-time wallet reads must not
    condemn it (reviewer-flagged false-drop class)."""
    o = Order.model_validate({
        "uid": _uid(1), "sellToken": USDC, "buyToken": WETH,
        "sellAmount": "1000000", "buyAmount": "300000000000000",
        "validTo": 2_000_000_000, "kind": "sell",
        "owner": "0x" + "11" * 20, "partiallyFillable": False,
        "class": "limit", "signingScheme": "eip712",
        "preInteractions": [{"target": "0x" + "22" * 20, "value": "0", "callData": "0x"}],
    })
    record: dict[str, Any] = {}
    mc = _fake_multicall(
        balance={o.owner.lower(): 0},  # would drop WITHOUT the exemption
        allowance={o.owner.lower(): 0},
        record=record,
    )
    kept, dropped = await filter_valid_orders(mc, [o], settlement_addr=SETTLEMENT)
    assert kept == [o]
    assert dropped == {}
    sels = {c.call_data[2:10] for c in record["calls"]}
    assert BALANCE_OF_SELECTOR not in sels  # funding checks not even queried


@pytest.mark.asyncio
async def test_partial_sell_kept_when_funded_for_smallest_fraction() -> None:
    """The router quotes partials down to 0.5×sell_amount — an owner funded
    for that smallest fraction is fillable BY US and must be kept, even if
    the full remainder exceeds the balance (reviewer-flagged false-drop)."""
    o = _order(1, partially_fillable=True, sell_amount=1_000_000)
    mc = _fake_multicall(
        balance={o.owner.lower(): 500_000},   # exactly the 0.5x fraction
        allowance={o.owner.lower(): 500_000},
    )
    kept, dropped = await filter_valid_orders(mc, [o], settlement_addr=SETTLEMENT)
    assert kept == [o]
    assert dropped == {}

    # Below even the smallest fraction → genuinely un-fillable by us → drop.
    mc2 = _fake_multicall(
        balance={o.owner.lower(): 499_999},
        allowance={o.owner.lower(): 499_999},
    )
    kept2, dropped2 = await filter_valid_orders(mc2, [o], settlement_addr=SETTLEMENT)
    assert kept2 == []
    assert dropped2 == {o.uid: "insufficient_balance"}


@pytest.mark.asyncio
async def test_vault_funded_order_skips_balance_checks() -> None:
    """sellTokenBalance != erc20 → wallet balance is meaningless; no
    balanceOf/allowance calls are built and the order is kept."""
    o = _order(1, sell_token_balance="external")
    record: dict[str, Any] = {}
    mc = _fake_multicall(balance={o.owner.lower(): 0}, record=record)
    kept, dropped = await filter_valid_orders(mc, [o], settlement_addr=SETTLEMENT)
    assert kept == [o]
    assert dropped == {}
    sels = {c.call_data[2:10] for c in record["calls"]}
    assert BALANCE_OF_SELECTOR not in sels
    assert ALLOWANCE_SELECTOR not in sels


@pytest.mark.asyncio
async def test_rpc_failure_fails_open() -> None:
    mc = MagicMock()

    async def boom(calls: list[Call], block: str = "latest") -> list[CallResult]:
        raise RuntimeError("RPC error 429: Too Many Requests")

    mc.aggregate_resilient = boom
    orders = [_order(1), _order(2)]
    kept, dropped = await filter_valid_orders(mc, orders, settlement_addr=SETTLEMENT)
    assert kept == orders
    assert dropped == {}


@pytest.mark.asyncio
async def test_reverted_view_call_fails_open_per_check() -> None:
    """A token whose balanceOf reverts (success=False) must not drop the
    order — only that single reading is skipped."""

    async def agg(calls: list[Call], block: str = "latest") -> list[CallResult]:
        out = []
        for c in calls:
            if c.call_data[2:10] == BALANCE_OF_SELECTOR:
                out.append(CallResult(success=False, return_data=b""))
            else:
                out.append(CallResult(success=True, return_data=(0).to_bytes(32, "big")))
        return out

    mc = MagicMock()
    mc.aggregate_resilient = agg
    o = _order(1)
    kept, dropped = await filter_valid_orders(mc, [o], settlement_addr=SETTLEMENT)
    # filled=0 (kept), balance unreadable (skipped), allowance=0 → dropped on
    # allowance, NOT on the unreadable balance.
    assert kept == []
    assert dropped == {o.uid: "insufficient_allowance"}


@pytest.mark.asyncio
async def test_malformed_uid_fails_open() -> None:
    """Non-hex uids (legacy fixtures) must never crash the solve."""
    bad = Order.model_validate({
        "uid": "0x_not_hex", "sellToken": USDC, "buyToken": WETH,
        "sellAmount": "1000", "buyAmount": "900", "validTo": 2_000_000_000,
        "kind": "sell", "owner": "0x" + "11" * 20,
        "partiallyFillable": False, "class": "limit",
    })
    kept, dropped = await filter_valid_orders(
        _fake_multicall(), [bad], settlement_addr=SETTLEMENT
    )
    assert kept == [bad]
    assert dropped == {}


@pytest.mark.asyncio
async def test_simulation_block_forwarded() -> None:
    record: dict[str, Any] = {}
    mc = _fake_multicall(record=record)
    await filter_valid_orders(
        mc, [_order(1)], settlement_addr=SETTLEMENT, block="0xabc123"
    )
    assert record["block"] == "0xabc123"


# ── Router wiring ────────────────────────────────────────────────────────────


def _auction(orders: list[Order], sim_block: int | None = None) -> Any:
    from src.models.auction import Auction

    return Auction(
        id="42", orders=orders, tokens={},
        deadline="2026-12-31T00:00:00Z", simulation_block=sim_block,
    )


def _mock_quoter(monkeypatch, quoted_uids: list[str]) -> None:
    from src.routing.v3_batched import V3BatchedQuote

    async def mock_batched(_mc, paths, *args, **kwargs):
        for p in paths:
            if p.order_uid not in quoted_uids:
                quoted_uids.append(p.order_uid)
        return [V3BatchedQuote(path=p, amount_out=400_000_000_000_000) for p in paths]

    monkeypatch.setattr("src.solver.router.batched_v3_quote", mock_batched)


@pytest.mark.asyncio
async def test_router_validity_filter_redirects_to_next_order(monkeypatch) -> None:
    """THE point of the pre-filter: a zombie order at the top of the surplus
    sort must lose its quote slot to the next live order — instead of being
    re-quoted (and re-phantomed) auction after auction."""
    from src.solver.router import RouterSolver

    zombie, live = _order(1), _order(2)

    async def fake_filter(_mc, orders, *, settlement_addr, vault_relayer=None, block):
        kept = [o for o in orders if o.uid != zombie.uid]
        return kept, {zombie.uid: "filled"}

    monkeypatch.setattr("src.solver.router.filter_valid_orders", fake_filter)
    quoted: list[str] = []
    _mock_quoter(monkeypatch, quoted)

    solver = RouterSolver(
        multicall=MagicMock(), intermediates=[],
        max_orders=1, order_validity_filter=True,
    )
    result = await solver.solve(_auction([zombie, live]))

    assert zombie.uid not in quoted, "zombie order still reached the quoter"
    assert quoted == [live.uid]
    from src.solver.base import NoSolution

    assert not isinstance(result, NoSolution)


@pytest.mark.asyncio
async def test_router_validity_filter_disabled_is_inert(monkeypatch) -> None:
    from src.solver.router import RouterSolver

    calls = {"n": 0}

    async def fake_filter(*a, **k):  # pragma: no cover - must not run
        calls["n"] += 1
        raise AssertionError("filter must not be called when disabled")

    monkeypatch.setattr("src.solver.router.filter_valid_orders", fake_filter)
    _mock_quoter(monkeypatch, [])

    solver = RouterSolver(
        multicall=MagicMock(), intermediates=[],
        order_validity_filter=False,
    )
    await solver.solve(_auction([_order(1)]))
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_router_validity_filter_uses_simulation_block(monkeypatch) -> None:
    """Poller auctions carry the competition simulation block — the filter
    MUST check there, not at `latest`, or post-settlement state would mark
    the winner's freshly-filled orders as dead and skew shadow data."""
    from src.solver.router import RouterSolver

    seen: dict[str, Any] = {}

    async def fake_filter(_mc, orders, *, settlement_addr, vault_relayer=None, block):
        seen["block"] = block
        return list(orders), {}

    monkeypatch.setattr("src.solver.router.filter_valid_orders", fake_filter)
    _mock_quoter(monkeypatch, [])

    solver = RouterSolver(
        multicall=MagicMock(), intermediates=[], order_validity_filter=True,
    )
    await solver.solve(_auction([_order(1)], sim_block=471_000_000))
    assert seen["block"] == hex(471_000_000)

    await solver.solve(_auction([_order(1)], sim_block=None))
    assert seen["block"] == "latest"


@pytest.mark.asyncio
async def test_allowance_call_targets_vault_relayer() -> None:
    """The allowance spender must be the GPv2VaultRelayer — settle() pulls
    sell funds through IT, not through the settlement contract."""
    record: dict[str, Any] = {}
    await filter_valid_orders(
        _fake_multicall(record=record), [_order(1)], settlement_addr=SETTLEMENT
    )
    allowance_calls = [
        c for c in record["calls"] if c.call_data[2:10] == ALLOWANCE_SELECTOR
    ]
    assert len(allowance_calls) == 1
    (_owner, spender) = decode(
        ["address", "address"], bytes.fromhex(allowance_calls[0].call_data[10:])
    )
    assert spender.lower() == GPV2_VAULT_RELAYER.lower()
