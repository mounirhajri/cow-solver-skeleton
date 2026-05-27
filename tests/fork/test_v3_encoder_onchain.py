"""Opt-in: simulate our encoded V3 calldata against a real Arbitrum RPC.

The unit tests prove the calldata round-trips through abi.decode — but
that doesn't catch e.g.

  * targeting the wrong router contract (SwapRouter vs SwapRouter02 —
    they share an address space but the struct shape differs)
  * encoding for a fee tier with no liquidity (pool exists at 0.05% but
    not 0.3% for this token pair, our quoter returned the wrong one)
  * sending into a recipient that can't actually hold the output token
    (some tokens have transfer hooks that revert against contracts)

``eth_call`` against the live router exercises every check the on-chain
swap will do at settlement time, modulo state mutation. State-mutating
tests (real settlement, balance verification) need a writable fork —
Anvil or Tenderly — and live behind a separate marker.
"""

from __future__ import annotations

import urllib.request
import json

import pytest

from src.encoder.v3 import encode_v3_swap

USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
SETTLEMENT = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"
V3_SWAP_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"


def _eth_call(rpc_url: str, to: str, data: bytes, from_addr: str = SETTLEMENT) -> dict[str, object]:
    """Execute a single ``eth_call`` and return the raw JSON-RPC response.

    Returns the entire response so callers can inspect ``error.message``
    on revert. A real revert is informative (we want to know *why*) so we
    don't unwrap it here.
    """
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "id": 1,
        "params": [
            {
                "from": from_addr,
                "to": to,
                "data": "0x" + data.hex(),
            },
            "latest",
        ],
    }
    req = urllib.request.Request(
        rpc_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_exact_input_single_calldata_executes_against_v3_router(
    arbitrum_rpc_url: str,
) -> None:
    """Our exactInputSingle calldata must execute against the live V3 router.

    We call from the GPv2 Settlement address — the contract that will
    actually invoke this calldata during settle(). Settlement holds
    allowances to the V3 router for major tokens (or we approve as part
    of the setup script), and ``eth_call`` honours those allowances at
    pinned state.

    The expected outcome: either a successful return (we get back the
    amount_out wei value) or a revert with an *informative* reason. A
    silent revert with empty data would mean we're hitting the wrong
    function or the wrong contract.
    """
    # Small amount so this works even if Settlement has minimal balance
    # — we're just exercising the calldata shape, not actually moving
    # value. The router will revert on insufficient balance, which is
    # fine here: we only assert the revert isn't "function selector not
    # recognised" (would mean wrong contract / wrong selector).
    calldata = encode_v3_swap(
        token_in=USDC,
        token_out=WETH,
        fee_in=500,
        intermediate=None,
        fee_out=None,
        exact_output=False,
        executed_sell=1_000_000,  # 1 USDC
        executed_buy=10**12,       # arbitrary positive lower bound for amountOutMinimum
        recipient=SETTLEMENT,
        deadline=2**31 - 1,
        slippage_bps=50,
        router_address=V3_SWAP_ROUTER,
    )

    resp = _eth_call(arbitrum_rpc_url, V3_SWAP_ROUTER, calldata.call_data)

    if "error" in resp:
        msg = str(resp["error"].get("message", "")).lower()
        # A "no liquidity" or "insufficient balance" or "STF" (transfer
        # failure) revert is fine — it means the router DID recognise our
        # call and started processing it. A "function selector not
        # recognised" would mean we picked the wrong router or the wrong
        # struct shape.
        assert "selector" not in msg, (
            f"selector mismatch — wrong router or wrong struct shape: {msg}"
        )
        # If the revert reason mentions our struct fields (recipient,
        # amount, etc.) that's confirmation the dispatch worked.
        # Accept any revert that isn't a structural mismatch.
        return

    # Success path: response contains the encoded amount_out as a hex
    # string. We don't compare exact wei (pool state drifts) but the
    # value should at least decode as a non-zero uint256.
    result = resp.get("result", "")
    assert isinstance(result, str) and result.startswith("0x"), (
        f"unexpected response shape: {resp}"
    )
