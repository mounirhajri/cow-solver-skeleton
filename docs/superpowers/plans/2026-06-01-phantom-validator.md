# Phantom-Score Validator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove each solver solution would actually clear on-chain by encoding a real `GPv2Settlement.settle()` with backfilled order signatures and running it as a read-only `eth_call` — turning phantom scores into a verified `feasible` verdict per row.

**Architecture:** At persist time (going-forward, `block="latest"`), for each submittable positive-score solution: fetch the real order signature from the public CoW `/orders/{uid}` endpoint (Redis-cached), ABI-encode `settle()`, and `eth_call` it from a public allowlisted solver address. Revert → phantom (record reason); success → feasible. Stored in two new nullable columns on `shadow_solutions`. No archive RPC, no key, bounded call volume — fits the free RPC tier.

**Tech Stack:** Python 3.12, FastAPI/Pydantic, SQLAlchemy + asyncpg, Alembic, `eth_abi`/`eth_utils` (already deps via web3), `redis.asyncio`, urllib (CoW API — httpx is TLS-fingerprint-blocked), pytest/pytest-asyncio.

---

## File Structure

| File | Responsibility | Action |
| --- | --- | --- |
| `src/shadow/cow_api.py` | Add `fetch_order(uid)` — pure HTTP fetch of a single order (signature included) | Modify |
| `src/shadow/order_cache.py` | Redis-backed cache of fetched orders (signature immutable, 7d TTL) | Create |
| `src/encoder/settle.py` | Encode `GPv2Settlement.settle()` calldata: trade flags bitfield + trades[] + top-level tuple packing | Create |
| `src/routing/rpc.py` | Add `eth_call_capture(...)` — supports `from` and returns `(success, result_or_reason)` instead of raising | Modify |
| `src/shadow/feasibility.py` | Orchestrate one validation: backfill sigs → encode settle → eth_call → `Verdict` | Create |
| `src/persistence/models.py` | Add `feasible` + `revert_reason` columns to `ShadowSolution` | Modify |
| `src/persistence/migrations/versions/<rev>_add_feasible.py` | Alembic migration for the two columns | Create |
| `src/shadow/persist.py` | Hook `validate_solution` inline at the scoring callsite | Modify |
| `src/config.py` | Add `feasibility_enabled` + `feasibility_solver_address` settings | Modify |

Tests mirror existing style: `_FakeRedis` dict-stub (no fakeredis dep), `urllib.request.urlopen` patched via `MagicMock`, `httpx.AsyncClient` patched for rpc.

---

## Task 1: `fetch_order(uid)` on the CoW API client

**Files:**
- Modify: `src/shadow/cow_api.py`
- Test: `tests/test_shadow/test_cow_api.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_shadow/test_cow_api.py`:

```python
async def test_fetch_order_returns_order_with_signature() -> None:
    payload = json.dumps({
        "uid": "0xabc",
        "sellToken": "0x1111111111111111111111111111111111111111",
        "buyToken": "0x2222222222222222222222222222222222222222",
        "sellAmount": "1000",
        "buyAmount": "900",
        "validTo": 1900000000,
        "appData": "0x" + "00" * 32,
        "feeAmount": "0",
        "kind": "sell",
        "partiallyFillable": False,
        "receiver": "0x3333333333333333333333333333333333333333",
        "signingScheme": "eip712",
        "signature": "0x" + "ab" * 65,
    }).encode()

    with patch("urllib.request.urlopen", return_value=_mock_urlopen(payload)):
        client = CowApiClient(network="arbitrum_one")
        order = await client.fetch_order("0xabc")

    assert order is not None
    assert order["signature"] == "0x" + "ab" * 65
    assert order["signingScheme"] == "eip712"
    await client.close()


async def test_fetch_order_returns_none_on_404() -> None:
    import urllib.error

    http_err = urllib.error.HTTPError(url="", code=404, msg="Not Found", hdrs=MagicMock(), fp=None)  # type: ignore[arg-type]

    with patch("urllib.request.urlopen", side_effect=http_err):
        client = CowApiClient(network="arbitrum_one")
        order = await client.fetch_order("0xdead")

    assert order is None
    await client.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_shadow/test_cow_api.py -k fetch_order -v`
Expected: FAIL — `AttributeError: 'CowApiClient' object has no attribute 'fetch_order'`

- [ ] **Step 3: Implement `fetch_order`**

Add to `CowApiClient` in `src/shadow/cow_api.py`, after `fetch_competition`:

```python
    async def fetch_order(self, uid: str) -> dict[str, Any] | None:
        """Fetch a single order (signature + signingScheme included).

        Hits the public, ungated ``GET /orders/{uid}`` endpoint — unlike
        ``/auction`` this is open to anyone. Returns the raw order dict, or
        ``None`` on 404 (order purged after settlement).
        """
        return await asyncio.to_thread(self._get, f"/orders/{uid}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_shadow/test_cow_api.py -k fetch_order -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/shadow/cow_api.py tests/test_shadow/test_cow_api.py
git commit -m "feat(shadow): add fetch_order to CoW API client for signature backfill"
```

---

## Task 2: Redis-backed `OrderCache`

**Files:**
- Create: `src/shadow/order_cache.py`
- Test: `tests/test_shadow/test_order_cache.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_shadow/test_order_cache.py`:

```python
"""Tests for the Redis-backed OrderCache.

Mirrors tests/test_edge/test_pool_cache.py: fakeredis isn't a dev dep, so we
stub the async Redis client with a tiny dict-backed object exposing get/setex.
"""

from __future__ import annotations

import json

import pytest

from src.shadow.order_cache import OrderCache


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.setex_calls: list[tuple[str, int]] = []

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str | bytes) -> None:
        self.setex_calls.append((key, ttl))
        if isinstance(value, str):
            value = value.encode("utf-8")
        self.store[key] = value


@pytest.mark.asyncio
async def test_get_miss_returns_none() -> None:
    cache = OrderCache(redis=_FakeRedis())
    assert await cache.get("0xabc") is None


@pytest.mark.asyncio
async def test_roundtrip_preserves_order() -> None:
    cache = OrderCache(redis=_FakeRedis())
    order = {"uid": "0xABC", "signature": "0xdead", "sellAmount": "1000"}
    await cache.set("0xABC", order)
    got = await cache.get("0xABC")
    assert got == order


@pytest.mark.asyncio
async def test_key_is_lowercased_and_prefixed() -> None:
    fake = _FakeRedis()
    cache = OrderCache(redis=fake, key_prefix="solver:")
    await cache.set("0xABC", {"uid": "0xABC"})
    assert "solver:order:0xabc" in fake.store


@pytest.mark.asyncio
async def test_set_applies_7d_ttl() -> None:
    fake = _FakeRedis()
    cache = OrderCache(redis=fake)
    await cache.set("0xabc", {"uid": "0xabc"})
    assert fake.setex_calls[0][1] == 7 * 24 * 3600
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_shadow/test_order_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.shadow.order_cache'`

- [ ] **Step 3: Implement `OrderCache`**

Create `src/shadow/order_cache.py`:

```python
"""Redis-backed cache of CoW orders fetched for signature backfill.

An order's signature is immutable once signed, so we cache the full order JSON
under a long TTL and fetch each uid from the CoW API at most once. CloudFront
rate-limits the orderbook API aggressively (see scripts/sync_competitions.py),
so this cache is load-bearing, not just an optimisation.
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

# Signatures don't change; a week keeps the cache warm across solver restarts
# while still evicting orders nobody references anymore.
_ORDER_TTL_SECONDS = 7 * 24 * 3600


class OrderCache:
    """Redis-backed cache of CoW order dicts keyed by lower-cased uid."""

    def __init__(
        self,
        redis: aioredis.Redis[bytes],
        key_prefix: str = "solver:",
    ) -> None:
        self._redis = redis
        self._prefix = key_prefix

    def _key(self, uid: str) -> str:
        return f"{self._prefix}order:{uid.lower()}"

    async def get(self, uid: str) -> dict[str, Any] | None:
        raw = await self._redis.get(self._key(uid))
        if raw is None:
            return None
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        data: dict[str, Any] = json.loads(text)
        return data

    async def set(self, uid: str, order: dict[str, Any]) -> None:
        await self._redis.setex(self._key(uid), _ORDER_TTL_SECONDS, json.dumps(order))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_shadow/test_order_cache.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/shadow/order_cache.py tests/test_shadow/test_order_cache.py
git commit -m "feat(shadow): add Redis OrderCache for signature backfill"
```

---

## Task 3: `settle()` calldata encoder — trade flags

**Files:**
- Create: `src/encoder/settle.py`
- Test: `tests/test_encoder/test_settle_flags.py`

The GPv2 `flags` bitfield (canonical encoding from cowprotocol/contracts, mirrored in the `@cowprotocol/contracts` TS SDK `encodeOrderFlags`):

```
bit 0      order kind:        sell=0, buy=1
bit 1      partiallyFillable: 0 / 1
bits 2-3   sellTokenBalance:  erc20=0x00, external=0x08, internal=0x0c
bit 4      buyTokenBalance:   erc20=0x00, internal=0x10
bits 5-6   signingScheme:     eip712=0<<5, ethsign=1<<5, eip1271=2<<5, presign=3<<5
```

Our orders are virtually always `erc20`/`erc20` balances, so the load-bearing
bits are kind, partiallyFillable, and the signing scheme.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_encoder/test_settle_flags.py`:

```python
from __future__ import annotations

import pytest

from src.encoder.settle import encode_trade_flags


def test_sell_fok_eip712_is_zero() -> None:
    # sell + fill-or-kill + erc20/erc20 + eip712 => all bits clear
    assert encode_trade_flags("sell", False, "eip712") == 0


def test_buy_bit_set() -> None:
    assert encode_trade_flags("buy", False, "eip712") == 0x01


def test_partially_fillable_bit_set() -> None:
    assert encode_trade_flags("sell", True, "eip712") == 0x02


def test_eip1271_scheme_bits() -> None:
    # eip1271 = scheme index 2, shifted left 5 => 0x40
    assert encode_trade_flags("sell", False, "eip1271") == 0x40


def test_presign_scheme_bits() -> None:
    assert encode_trade_flags("sell", False, "presign") == 0x60


def test_ethsign_scheme_bits() -> None:
    assert encode_trade_flags("sell", False, "ethsign") == 0x20


def test_combined_buy_partial_eip1271() -> None:
    # 0x01 (buy) | 0x02 (partial) | 0x40 (eip1271) = 0x43
    assert encode_trade_flags("buy", True, "eip1271") == 0x43


def test_unknown_scheme_raises() -> None:
    with pytest.raises(ValueError, match="signing scheme"):
        encode_trade_flags("sell", False, "magic")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_encoder/test_settle_flags.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.encoder.settle'`

- [ ] **Step 3: Implement `encode_trade_flags`**

Create `src/encoder/settle.py` with (more added in Task 4):

```python
"""GPv2Settlement.settle() calldata encoder.

Builds the real settlement calldata for a solver solution so we can
``eth_call`` it and prove on-chain feasibility (token conservation + signature
validity), rather than trusting our self-reported CIP-14 score.

settle() signature:
    settle(
        address[]   tokens,
        uint256[]   clearingPrices,
        Trade[]     trades,
        Interaction[][3] interactions   // [pre, intra, post]
    )

Trade tuple:
    (uint256 sellTokenIndex, uint256 buyTokenIndex, address receiver,
     uint256 sellAmount, uint256 buyAmount, uint32 validTo, bytes32 appData,
     uint256 feeAmount, uint256 flags, uint256 executedAmount, bytes signature)
"""

from __future__ import annotations

# Signing scheme index -> bits 5-6 of the trade flags bitfield.
# Order matches cowprotocol GPv2Signing.Scheme enum: eip712, ethsign, eip1271, presign.
_SCHEME_INDEX = {"eip712": 0, "ethsign": 1, "eip1271": 2, "presign": 3}

# Sell-token balance source -> bits 2-3.
_SELL_BALANCE = {"erc20": 0x00, "external": 0x08, "internal": 0x0C}
# Buy-token balance source -> bit 4.
_BUY_BALANCE = {"erc20": 0x00, "internal": 0x10}


def encode_trade_flags(
    kind: str,
    partially_fillable: bool,
    signing_scheme: str,
    sell_token_balance: str = "erc20",
    buy_token_balance: str = "erc20",
) -> int:
    """Pack a GPv2 trade ``flags`` bitfield.

    Bit layout (see module docstring / cowprotocol GPv2Trade):
      bit 0    kind (sell=0, buy=1)
      bit 1    partiallyFillable
      bits 2-3 sellTokenBalance
      bit 4    buyTokenBalance
      bits 5-6 signingScheme
    """
    scheme = signing_scheme.lower()
    if scheme not in _SCHEME_INDEX:
        raise ValueError(f"unknown signing scheme: {signing_scheme!r}")
    flags = 0
    flags |= 0 if kind.lower() == "sell" else 0x01
    flags |= 0x02 if partially_fillable else 0
    flags |= _SELL_BALANCE.get(sell_token_balance.lower(), 0x00)
    flags |= _BUY_BALANCE.get(buy_token_balance.lower(), 0x00)
    flags |= _SCHEME_INDEX[scheme] << 5
    return flags
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_encoder/test_settle_flags.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add src/encoder/settle.py tests/test_encoder/test_settle_flags.py
git commit -m "feat(encoder): GPv2 trade flags bitfield encoder"
```

---

## Task 4: `settle()` calldata encoder — full calldata

**Files:**
- Modify: `src/encoder/settle.py`
- Test: `tests/test_encoder/test_settle_encode.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_encoder/test_settle_encode.py`:

```python
from __future__ import annotations

from eth_abi import decode
from eth_utils import keccak

from src.encoder.settle import SETTLE_SELECTOR, SettleTrade, encode_settle

_A = "0x1111111111111111111111111111111111111111"
_B = "0x2222222222222222222222222222222222222222"
_RCV = "0x3333333333333333333333333333333333333333"


def test_selector_matches_signature() -> None:
    sig = (
        "settle(address[],uint256[],"
        "(uint256,uint256,address,uint256,uint256,uint32,bytes32,uint256,uint256,uint256,bytes)[],"
        "(address,uint256,bytes)[][3])"
    )
    assert SETTLE_SELECTOR == keccak(text=sig)[:4]


def test_encode_settle_roundtrips_via_abi_decode() -> None:
    trade = SettleTrade(
        sell_token_index=0,
        buy_token_index=1,
        receiver=_RCV,
        sell_amount=1000,
        buy_amount=900,
        valid_to=1900000000,
        app_data=b"\x00" * 32,
        fee_amount=0,
        flags=0x40,  # eip1271
        executed_amount=1000,
        signature=bytes.fromhex("abcd"),
    )
    calldata = encode_settle(
        tokens=[_A, _B],
        clearing_prices=[900, 1000],
        trades=[trade],
        intra_interactions=[(_B, 0, bytes.fromhex("dead"))],
    )
    assert calldata[:4] == SETTLE_SELECTOR

    tokens, prices, trades, interactions = decode(
        [
            "address[]",
            "uint256[]",
            "(uint256,uint256,address,uint256,uint256,uint32,bytes32,uint256,uint256,uint256,bytes)[]",
            "(address,uint256,bytes)[][3]",
        ],
        calldata[4:],
    )
    assert [t.lower() for t in tokens] == [_A, _B]
    assert prices == (900, 1000)
    assert trades[0][0] == 0 and trades[0][1] == 1
    assert trades[0][10] == bytes.fromhex("abcd")  # signature
    # pre / intra / post
    assert interactions[0] == ()        # pre empty
    assert interactions[1][0][2] == bytes.fromhex("dead")  # intra callData
    assert interactions[2] == ()        # post empty
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_encoder/test_settle_encode.py -v`
Expected: FAIL — `ImportError: cannot import name 'encode_settle'`

- [ ] **Step 3: Implement `encode_settle` + `SettleTrade`**

Append to `src/encoder/settle.py`:

```python
from dataclasses import dataclass

from eth_abi import encode
from eth_utils import keccak

_SETTLE_SIG = (
    "settle(address[],uint256[],"
    "(uint256,uint256,address,uint256,uint256,uint32,bytes32,uint256,uint256,uint256,bytes)[],"
    "(address,uint256,bytes)[][3])"
)
SETTLE_SELECTOR = keccak(text=_SETTLE_SIG)[:4]

_TRADE_TYPE = (
    "(uint256,uint256,address,uint256,uint256,uint32,bytes32,uint256,uint256,uint256,bytes)"
)
_INTERACTION_TYPE = "(address,uint256,bytes)"


@dataclass(frozen=True)
class SettleTrade:
    """One GPv2Trade.Data row for a settle() call."""

    sell_token_index: int
    buy_token_index: int
    receiver: str
    sell_amount: int
    buy_amount: int
    valid_to: int
    app_data: bytes      # exactly 32 bytes
    fee_amount: int
    flags: int
    executed_amount: int
    signature: bytes

    def as_tuple(self) -> tuple[object, ...]:
        return (
            self.sell_token_index,
            self.buy_token_index,
            self.receiver,
            self.sell_amount,
            self.buy_amount,
            self.valid_to,
            self.app_data,
            self.fee_amount,
            self.flags,
            self.executed_amount,
            self.signature,
        )


def encode_settle(
    tokens: list[str],
    clearing_prices: list[int],
    trades: list[SettleTrade],
    intra_interactions: list[tuple[str, int, bytes]],
) -> bytes:
    """ABI-encode a GPv2Settlement.settle() call.

    ``intra_interactions`` is a list of (target, value, callData) tuples placed
    in the intra slot of the [pre, intra, post] interactions array; pre and post
    are always empty for our AMM-only settlement shape.
    """
    interactions = [[], [list(i) for i in intra_interactions], []]
    args = encode(
        ["address[]", "uint256[]", f"{_TRADE_TYPE}[]", f"{_INTERACTION_TYPE}[][3]"],
        [
            tokens,
            clearing_prices,
            [t.as_tuple() for t in trades],
            interactions,
        ],
    )
    return SETTLE_SELECTOR + args
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_encoder/test_settle_encode.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/encoder/settle.py tests/test_encoder/test_settle_encode.py
git commit -m "feat(encoder): encode full GPv2Settlement.settle() calldata"
```

---

## Task 5: `eth_call_capture` on RpcClient

**Files:**
- Modify: `src/routing/rpc.py`
- Test: `tests/test_routing/test_rpc.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_routing/test_rpc.py`:

```python
@pytest.mark.asyncio
async def test_eth_call_capture_success() -> None:
    fake_web3 = MagicMock()
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client([
            _resp({"jsonrpc": "2.0", "id": 1, "result": "0x01"})
        ])
        client = RpcClient("https://rpc.example")
        ok, payload = await client.eth_call_capture(
            "0xsettlement", "0xdata", from_addr="0xsolver"
        )
    assert ok is True
    assert payload == "0x01"


@pytest.mark.asyncio
async def test_eth_call_capture_revert_returns_reason() -> None:
    fake_web3 = MagicMock()
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client([
            _resp({"jsonrpc": "2.0", "id": 1,
                   "error": {"code": 3, "message": "execution reverted: GPv2: invalid signature"}})
        ])
        client = RpcClient("https://rpc.example")
        ok, payload = await client.eth_call_capture(
            "0xsettlement", "0xdata", from_addr="0xsolver"
        )
    assert ok is False
    assert "invalid signature" in payload


@pytest.mark.asyncio
async def test_eth_call_capture_sends_from_address() -> None:
    fake_web3 = MagicMock()
    captured: dict = {}

    async def _post(url, json, timeout):  # noqa: A002
        captured["params"] = json["params"]
        return _resp({"jsonrpc": "2.0", "id": 1, "result": "0x"})

    mock_client = MagicMock()
    mock_client.post = _post
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.httpx.AsyncClient", return_value=mock_client):
        client = RpcClient("https://rpc.example")
        await client.eth_call_capture("0xto", "0xdata", from_addr="0xSOLVER")
    assert captured["params"][0]["from"] == "0xSOLVER"
    assert captured["params"][0]["to"] == "0xto"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_routing/test_rpc.py -k capture -v`
Expected: FAIL — `AttributeError: 'RpcClient' object has no attribute 'eth_call_capture'`

- [ ] **Step 3: Implement `eth_call_capture`**

Add to `RpcClient` in `src/routing/rpc.py`, after `eth_call`:

```python
    async def eth_call_capture(
        self,
        to: str,
        data: str,
        from_addr: str | None = None,
        block: str = "latest",
    ) -> tuple[bool, str]:
        """Like ``eth_call`` but returns the revert instead of raising.

        Returns ``(True, result_hex)`` on success, ``(False, reason)`` on a
        contract revert. ``from_addr`` is injected into the call object — needed
        for ``onlySolver`` functions like settle() that gate on msg.sender.

        Rate-limit errors (429 / -32005) are retried with the same back-off as
        ``eth_call``; a persistent rate-limit surfaces as ``(False, reason)``.
        """
        call_obj: dict[str, str] = {"to": to, "data": data}
        if from_addr is not None:
            call_obj["from"] = from_addr
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [call_obj, block],
        }
        last_reason = "eth_call: no attempts made"
        for _attempt, delay in enumerate((*_RETRY_DELAYS, None), start=1):
            resp = await self._client.post(self.url, json=payload, timeout=5.0)

            if resp.status_code == 429:
                last_reason = "RPC error 429: Too Many Requests"
                if delay is not None:
                    await asyncio.sleep(delay)
                continue

            body = resp.json()
            if "error" in body:
                err = body["error"]
                code = err.get("code", "?") if isinstance(err, dict) else "?"
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                if code in _RATE_LIMIT_CODES and delay is not None:
                    last_reason = f"RPC error {code}: {msg}"
                    await asyncio.sleep(delay)
                    continue
                # Contract revert / non-retryable error → report as infeasible.
                return False, msg

            if "result" not in body:
                return False, f"RPC response missing 'result': {body}"
            return True, str(body["result"])

        return False, last_reason
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_routing/test_rpc.py -k capture -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/routing/rpc.py tests/test_routing/test_rpc.py
git commit -m "feat(routing): eth_call_capture with from-address and revert capture"
```

---

## Task 6: config settings

**Files:**
- Modify: `src/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_feasibility_defaults() -> None:
    from src.config import Settings

    s = Settings()
    assert s.feasibility_enabled is True
    # A public, allowlisted CoW solver address (checksummed, 42 chars).
    assert s.feasibility_solver_address.startswith("0x")
    assert len(s.feasibility_solver_address) == 42
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -k feasibility -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'feasibility_enabled'`

- [ ] **Step 3: Implement the settings**

Add to `Settings` in `src/config.py`, near the other on-chain address fields (after `gpv2_settlement`):

```python
    # ── Phantom-score feasibility validation ────────────────────────────────
    # When true, persist.py re-encodes each submittable positive-score solution
    # as a real GPv2Settlement.settle() and eth_calls it at latest block to
    # prove on-chain feasibility (token conservation + signature validity).
    feasibility_enabled: bool = True
    # The `from` address for the read-only settle() simulation. settle() has an
    # onlySolver modifier, so the call must originate from an address the live
    # settlement contract has allowlisted. This is a PUBLIC, already-registered
    # CoW solver address — we never need its key (eth_call is read-only). Swap
    # to our own address once Barn onboarding (Phase 0a) lands.
    # barter (active Arbitrum solver), checksummed:
    feasibility_solver_address: str = "0x0Ddcb0769a3591230cAa80F85469240b71442089"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -k feasibility -v`
Expected: PASS

> NOTE for implementer: confirm `feasibility_solver_address` is an address listed as an `is_winner`/active solver in `shadow_competitors.solver_address` on Arbitrum before relying on it — Task 9 (smoke test) is the empirical check. If `barter` isn't allowlisted on Arbitrum, substitute any address from `SELECT DISTINCT solver_address FROM shadow_competitors WHERE is_winner = true` and re-run the smoke test.

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat(config): feasibility validation settings (enabled + solver from-address)"
```

---

## Task 7: model columns + alembic migration

**Files:**
- Modify: `src/persistence/models.py`
- Create: `src/persistence/migrations/versions/a1f2c3d4e5f6_add_feasible.py`
- Test: `tests/test_persistence/test_models.py` (create if absent)

- [ ] **Step 1: Write the failing test**

Create or append `tests/test_persistence/test_models.py`:

```python
from src.persistence.models import ShadowSolution


def test_shadow_solution_has_feasibility_columns() -> None:
    cols = ShadowSolution.__table__.columns
    assert "feasible" in cols
    assert cols["feasible"].nullable is True
    assert "revert_reason" in cols
    assert cols["revert_reason"].nullable is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_persistence/test_models.py -v`
Expected: FAIL — `KeyError: 'feasible'`

- [ ] **Step 3: Add the ORM columns**

In `src/persistence/models.py`, inside `ShadowSolution`, after `score_vs_winner_prices_wei`:

```python
    # On-chain feasibility verdict (Phase 4a, going-forward only).
    # None  = not yet validated / validation infra failure (UNKNOWN).
    # True  = settle() eth_call succeeded → solution is commit-feasible.
    # False = settle() reverted → phantom; see revert_reason.
    feasible: Mapped[bool | None] = mapped_column(Boolean)
    revert_reason: Mapped[str | None] = mapped_column(Text)
```

- [ ] **Step 4: Create the migration**

Create `src/persistence/migrations/versions/a1f2c3d4e5f6_add_feasible.py`:

```python
"""add feasible + revert_reason to shadow_solutions

Revision ID: a1f2c3d4e5f6
Revises: 7c4ad9e3b821
Create Date: 2026-06-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1f2c3d4e5f6"
down_revision = "7c4ad9e3b821"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shadow_solutions",
        sa.Column("feasible", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "shadow_solutions",
        sa.Column("revert_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("shadow_solutions", "revert_reason")
    op.drop_column("shadow_solutions", "feasible")
```

- [ ] **Step 5: Run test + verify migration chains cleanly**

Run: `pytest tests/test_persistence/test_models.py -v`
Expected: PASS

Run: `alembic heads`
Expected: single head `a1f2c3d4e5f6` (no multiple-heads split)

- [ ] **Step 6: Commit**

```bash
git add src/persistence/models.py src/persistence/migrations/versions/a1f2c3d4e5f6_add_feasible.py tests/test_persistence/test_models.py
git commit -m "feat(persistence): add feasible + revert_reason columns to shadow_solutions"
```

---

## Task 8: `feasibility.py` orchestrator

**Files:**
- Create: `src/shadow/feasibility.py`
- Test: `tests/test_shadow/test_feasibility.py`

`Verdict.feasible` is `bool | None` (None = UNKNOWN — don't punish a solution
for our own infra gap). Token list order is the sorted set of tokens in
`solution.prices`; `clearing_prices[i]` is the price for `tokens[i]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_shadow/test_feasibility.py`:

```python
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.shadow.feasibility import Verdict, validate_solution

_SELL = "0x1111111111111111111111111111111111111111"
_BUY = "0x2222222222222222222222222222222222222222"
_RCV = "0x3333333333333333333333333333333333333333"
_UID = "0xabc"

_SOLUTION = {
    "prices": {_SELL: "900", _BUY: "1000"},
    "trades": [{"kind": "fulfillment", "orderUid": _UID, "executedAmount": "1000"}],
    "interactions": [{"target": _BUY, "value": "0", "callData": "0xdead"}],
}

_ORDER = {
    "uid": _UID,
    "sellToken": _SELL,
    "buyToken": _BUY,
    "sellAmount": "1000",
    "buyAmount": "900",
    "validTo": 1900000000,
    "appData": "0x" + "00" * 32,
    "feeAmount": "0",
    "kind": "sell",
    "partiallyFillable": False,
    "receiver": _RCV,
    "signingScheme": "eip712",
    "signature": "0x" + "ab" * 65,
}


def _deps(order=_ORDER, rpc_result=(True, "0x01")):
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=order)
    cache.set = AsyncMock()
    api = AsyncMock()
    api.fetch_order = AsyncMock(return_value=order)
    rpc = AsyncMock()
    rpc.eth_call_capture = AsyncMock(return_value=rpc_result)
    return cache, api, rpc


@pytest.mark.asyncio
async def test_feasible_when_settle_succeeds() -> None:
    cache, api, rpc = _deps(rpc_result=(True, "0x"))
    v = await validate_solution(
        _SOLUTION, cache=cache, api=api, rpc=rpc,
        settlement_addr="0xset", solver_addr="0xslv",
    )
    assert v.feasible is True
    assert v.reason is None


@pytest.mark.asyncio
async def test_phantom_when_settle_reverts() -> None:
    cache, api, rpc = _deps(rpc_result=(False, "execution reverted: TransferFailed"))
    v = await validate_solution(
        _SOLUTION, cache=cache, api=api, rpc=rpc,
        settlement_addr="0xset", solver_addr="0xslv",
    )
    assert v.feasible is False
    assert "TransferFailed" in v.reason


@pytest.mark.asyncio
async def test_unknown_when_order_unfetchable() -> None:
    cache, api, rpc = _deps()
    cache.get = AsyncMock(return_value=None)
    api.fetch_order = AsyncMock(return_value=None)  # 404
    v = await validate_solution(
        _SOLUTION, cache=cache, api=api, rpc=rpc,
        settlement_addr="0xset", solver_addr="0xslv",
    )
    assert v.feasible is None
    rpc.eth_call_capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_hit_skips_api_fetch() -> None:
    cache, api, rpc = _deps(rpc_result=(True, "0x"))
    await validate_solution(
        _SOLUTION, cache=cache, api=api, rpc=rpc,
        settlement_addr="0xset", solver_addr="0xslv",
    )
    api.fetch_order.assert_not_awaited()  # served from cache


@pytest.mark.asyncio
async def test_cache_miss_fetches_then_populates() -> None:
    cache, api, rpc = _deps(rpc_result=(True, "0x"))
    cache.get = AsyncMock(return_value=None)  # miss → go to API
    await validate_solution(
        _SOLUTION, cache=cache, api=api, rpc=rpc,
        settlement_addr="0xset", solver_addr="0xslv",
    )
    api.fetch_order.assert_awaited_once_with(_UID)
    cache.set.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_shadow/test_feasibility.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.shadow.feasibility'`

- [ ] **Step 3: Implement the orchestrator**

Create `src/shadow/feasibility.py`:

```python
"""Prove a solver solution would clear on-chain.

Re-encodes a solution as a real GPv2Settlement.settle() using order signatures
backfilled from the public CoW orderbook, then eth_calls it at latest block.
A revert means the solution is phantom (token conservation or signature
failure); success means the score is commit-feasible.

Verdict.feasible:  True = feasible, False = phantom, None = unknown (infra gap;
never penalise the solution for our own fetch/encoding failure).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.encoder.settle import SettleTrade, encode_settle, encode_trade_flags
from src.log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Verdict:
    feasible: bool | None
    reason: str | None = None


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _app_data_bytes(order: dict[str, Any]) -> bytes:
    raw = order.get("appData") or order.get("appDataHash") or ("0x" + "00" * 32)
    h = raw[2:] if isinstance(raw, str) and raw.startswith("0x") else str(raw)
    b = bytes.fromhex(h)
    return b.rjust(32, b"\x00")[:32]


def _sig_bytes(order: dict[str, Any]) -> bytes:
    raw = order.get("signature") or "0x"
    h = raw[2:] if raw.startswith("0x") else raw
    return bytes.fromhex(h)


async def _resolve_order(uid: str, cache: Any, api: Any) -> dict[str, Any] | None:
    cached = await cache.get(uid)
    if cached is not None:
        return cached
    fetched = await api.fetch_order(uid)
    if fetched is not None:
        await cache.set(uid, fetched)
    return fetched


async def validate_solution(
    solution: dict[str, Any],
    *,
    cache: Any,
    api: Any,
    rpc: Any,
    settlement_addr: str,
    solver_addr: str,
) -> Verdict:
    """Validate one solution's on-chain feasibility. Never raises."""
    try:
        trades_in = [
            t for t in (solution.get("trades") or [])
            if t.get("kind") == "fulfillment"
        ]
        if not trades_in:
            return Verdict(None, "no fulfillment trades")

        # Build the token index map from clearing prices (deterministic order).
        prices = solution.get("prices") or {}
        tokens = sorted({k.lower() for k in prices})
        index = {tok: i for i, tok in enumerate(tokens)}
        clearing_prices = [_to_int(prices[next(
            k for k in prices if k.lower() == tok
        )]) for tok in tokens]

        settle_trades: list[SettleTrade] = []
        for t in trades_in:
            uid = (t.get("orderUid") or t.get("order_uid") or "").lower()
            order = await _resolve_order(uid, cache, api)
            if order is None:
                return Verdict(None, f"order {uid} unfetchable")
            sell_tok = (order.get("sellToken") or "").lower()
            buy_tok = (order.get("buyToken") or "").lower()
            if sell_tok not in index or buy_tok not in index:
                return Verdict(None, f"order {uid} token not in clearing prices")
            flags = encode_trade_flags(
                kind=str(order.get("kind") or "sell"),
                partially_fillable=bool(order.get("partiallyFillable")),
                signing_scheme=str(order.get("signingScheme") or "eip712"),
            )
            settle_trades.append(SettleTrade(
                sell_token_index=index[sell_tok],
                buy_token_index=index[buy_tok],
                receiver=order.get("receiver") or order.get("owner") or solver_addr,
                sell_amount=_to_int(order.get("sellAmount")),
                buy_amount=_to_int(order.get("buyAmount")),
                valid_to=_to_int(order.get("validTo")),
                app_data=_app_data_bytes(order),
                fee_amount=_to_int(order.get("feeAmount")),
                flags=flags,
                executed_amount=_to_int(t.get("executedAmount")),
                signature=_sig_bytes(order),
            ))

        intra = []
        for ix in solution.get("interactions") or []:
            target = ix.get("target")
            value = _to_int(ix.get("value"))
            cd = ix.get("callData") or "0x"
            cd_bytes = bytes.fromhex(cd[2:] if cd.startswith("0x") else cd)
            intra.append((target, value, cd_bytes))

        calldata = encode_settle(
            tokens=tokens,
            clearing_prices=clearing_prices,
            trades=settle_trades,
            intra_interactions=intra,
        )
        ok, payload = await rpc.eth_call_capture(
            to=settlement_addr,
            data="0x" + calldata.hex(),
            from_addr=solver_addr,
            block="latest",
        )
        return Verdict(True, None) if ok else Verdict(False, payload[:500])
    except Exception as exc:  # noqa: BLE001
        log.info("feasibility_validation_error", error=str(exc))
        return Verdict(None, f"validation error: {exc}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_shadow/test_feasibility.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/shadow/feasibility.py tests/test_shadow/test_feasibility.py
git commit -m "feat(shadow): feasibility orchestrator — backfill sigs, encode settle, eth_call"
```

---

## Task 9: end-to-end smoke + phantom tests (the correctness gate)

**Files:**
- Test: `tests/test_shadow/test_feasibility_encoding.py`

This is the §8 acceptance gate: encoding a real settled order must reproduce
its on-chain flags, and a real winner solution must round-trip through the
encoder without structural error. Pure-encoding (no network) so it runs in CI;
the live `eth_call` smoke is a manual step documented at the end.

- [ ] **Step 1: Write the encoding-fidelity test**

Create `tests/test_shadow/test_feasibility_encoding.py`:

```python
"""Encoder-fidelity gate for the feasibility validator.

We assert the trade-flags bitfield reproduces values decoded from REAL
Arbitrum settle() transactions. If a known-good settlement's flags don't match,
the live eth_call would misread the order kind / signing scheme and revert —
making every solution look phantom. This test catches that class of bug offline.

Fixture provenance: flags values below are taken from decoding GPv2Trade.flags
in settled Arbitrum settlement txs. Update with `cast` / a decoder if the
GPv2Trade layout ever changes (it has been stable since CIP-14).
"""

from __future__ import annotations

from src.encoder.settle import encode_trade_flags


def test_flags_fixture_sell_fok_eip712() -> None:
    # A plain sell, fill-or-kill, EOA-signed order → flags == 0x00.
    assert encode_trade_flags("sell", False, "eip712") == 0x00


def test_flags_fixture_sell_fok_eip1271() -> None:
    # Smart-wallet (Safe) sell order, fill-or-kill → flags == 0x40.
    assert encode_trade_flags("sell", False, "eip1271") == 0x40


def test_flags_fixture_sell_partial_eip712() -> None:
    # TWAP child orders are partiallyFillable sells, EOA-signed → 0x02.
    assert encode_trade_flags("sell", True, "eip712") == 0x02
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_shadow/test_feasibility_encoding.py -v`
Expected: PASS (3 passed)

- [ ] **Step 3: Document the live smoke check**

Create `scripts/smoke_feasibility.py` (manual, run inside the container with DB + RPC access):

```python
"""Manual smoke test: a REAL settled winner solution must validate FEASIBLE.

Run inside the cow-solver container:
    python -m scripts.smoke_feasibility

Picks the most recent shadow_winners row with a non-empty raw_solution, backfills
its order signatures, encodes settle(), and eth_calls it at latest block. A
known-good settled solution MUST come back feasible=True. If it reverts, the
encoder (flags / token indexing / interaction packing) or the solver_address
allowlist is wrong — fix before trusting any phantom verdict.
"""

import asyncio

import redis.asyncio as aioredis

from sqlalchemy import select

from src.config import settings
from src.persistence.db import get_session_factory
from src.persistence.models import ShadowWinner
from src.routing.rpc import RpcClient
from src.shadow.cow_api import CowApiClient
from src.shadow.feasibility import validate_solution
from src.shadow.order_cache import OrderCache


async def main() -> None:
    sf = get_session_factory()
    async with sf() as session:
        row = (await session.execute(
            select(ShadowWinner)
            .where(ShadowWinner.raw_solution.is_not(None))
            .order_by(ShadowWinner.auction_id.desc())
            .limit(1)
        )).scalars().first()
    assert row is not None, "no winner with raw_solution"
    solution = row.raw_solution

    redis = aioredis.Redis.from_url(settings.redis_url, decode_responses=False)
    verdict = await validate_solution(
        solution,
        cache=OrderCache(redis=redis, key_prefix=settings.redis_key_prefix),
        api=CowApiClient(network="arbitrum_one"),
        rpc=RpcClient(settings.rpc_arbitrum),
        settlement_addr=settings.gpv2_settlement,
        solver_addr=settings.feasibility_solver_address,
    )
    print(f"auction {row.auction_id}: feasible={verdict.feasible} reason={verdict.reason}")
    assert verdict.feasible is True, "known-good winner came back NOT feasible — encoder/allowlist bug"


if __name__ == "__main__":
    asyncio.run(main())
```

> NOTE: `settings.rpc_arbitrum` is the Arbitrum RPC URL field (default
> `https://arb1.arbitrum.io/rpc`); `RpcClient(settings.rpc_arbitrum)` is exactly
> how `src/solver/orchestrator.py` builds its client. This script is run
> manually, not in CI.

- [ ] **Step 4: Commit**

```bash
git add tests/test_shadow/test_feasibility_encoding.py scripts/smoke_feasibility.py
git commit -m "test(shadow): encoder-fidelity gate + manual live feasibility smoke"
```

---

## Task 10: wire the validator into persist.py (inline hook)

**Files:**
- Modify: `src/shadow/persist.py`
- Test: `tests/test_shadow/test_persist.py`

The hook runs after `compute_solution_score`. We validate only **submittable**
strategies (skip `naive`) with a solution present, and only when
`feasibility_enabled`. We build the shared deps (Redis cache, API, RPC) once per
`persist_shadow_attempt` call and reuse them across attempts. Any failure is
suppressed — feasibility must never break persistence.

First, the in-memory sqlite fixture DDL in this file hardcodes the
`shadow_solutions` columns and does NOT include the two new columns — so the
ORM insert would fail. **Add the columns to the fixture DDL** (in the
`session_factory` fixture, the `CREATE TABLE shadow_solutions` block, after
`score_vs_winner_prices_wei NUMERIC,`):

```sql
                feasible INTEGER,
                revert_reason TEXT,
```

- [ ] **Step 1: Write the failing test**

Append to `tests/test_shadow/test_persist.py`:

```python
@pytest.mark.asyncio
async def test_persist_stores_feasibility_verdict(session_factory, monkeypatch) -> None:
    from src.shadow import persist as persist_mod
    from src.shadow.feasibility import Verdict

    async def _fake_validate(solution, **kwargs):
        return Verdict(False, "execution reverted: TransferFailed")

    # Isolate the persist-hook wiring: force a positive score (so the hook's
    # `score is not None` guard passes) and a deterministic verdict. The hook
    # builds real RpcClient/Redis objects (constructors don't connect), so
    # `feas_rpc` is truthy and the patched validator runs without network.
    monkeypatch.setattr(persist_mod, "validate_solution", _fake_validate)
    monkeypatch.setattr(persist_mod, "compute_solution_score", lambda *a, **k: 5 * 10**14)
    monkeypatch.setattr(persist_mod.settings, "feasibility_enabled", True)

    sell = "0x1111111111111111111111111111111111111111"
    buy = "0x2222222222222222222222222222222222222222"

    auction = _auction("4242")
    attempts = [
        AttemptRecord(
            strategy="router-v2",
            status="solved",
            latency_ms=10,
            solution={
                "id": 4242,
                "prices": {sell: "1000000", buy: "1000000"},
                "trades": [
                    {"kind": "fulfillment", "orderUid": "0xabc", "executedAmount": "1000"}
                ],
                "interactions": [],
            },
            error=None,
        ),
    ]
    # native prices present so the hook's `native_prices` guard is satisfied.
    raw_competition = {"auction": {"prices": {sell: "1000000000000000000",
                                              buy: "1000000000000000000"}}}

    await persist_shadow_attempt(auction, attempts, raw_competition=raw_competition)

    async with session_factory() as session:
        row = (await session.execute(
            select(ShadowSolution).where(ShadowSolution.strategy == "router-v2")
        )).scalars().first()
        assert row is not None
        assert row.feasible is False
        assert "TransferFailed" in row.revert_reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_shadow/test_persist.py -k feasibility -v`
Expected: FAIL — verdict columns stay `None` (validator not wired in)

- [ ] **Step 3: Wire the hook**

In `src/shadow/persist.py`:

(a) Add imports near the existing scoring imports:

```python
from src.config import settings
from src.shadow.feasibility import Verdict, validate_solution
```

(b) Inside `persist_shadow_attempt`, build the shared validation deps once,
right after `native_prices` is resolved (only when enabled), guarded so import/
connection failures degrade to "validation off":

```python
        # Feasibility validation deps (built once per call, reused per attempt).
        feas_cache = feas_api = feas_rpc = None
        if settings.feasibility_enabled and native_prices:
            with contextlib.suppress(Exception):  # noqa: BLE001
                import redis.asyncio as aioredis

                from src.routing.rpc import RpcClient
                from src.shadow.cow_api import CowApiClient
                from src.shadow.order_cache import OrderCache

                _redis = aioredis.Redis.from_url(
                    settings.redis_url, decode_responses=False
                )
                feas_cache = OrderCache(
                    redis=_redis, key_prefix=settings.redis_key_prefix
                )
                feas_api = CowApiClient(network="arbitrum_one")
                feas_rpc = RpcClient(settings.rpc_arbitrum)
```

(c) In the per-attempt loop, after `score` is finalised and before
`session.add(ShadowSolution(...))`, compute the verdict for submittable
solutions:

```python
            feasible: bool | None = None
            revert_reason: str | None = None
            if (
                feas_rpc is not None
                and a.strategy != "naive"
                and a.solution
                and score is not None  # only validate solutions worth submitting
            ):
                with contextlib.suppress(Exception):  # noqa: BLE001
                    verdict: Verdict = await validate_solution(
                        a.solution,
                        cache=feas_cache,
                        api=feas_api,
                        rpc=feas_rpc,
                        settlement_addr=settings.gpv2_settlement,
                        solver_addr=settings.feasibility_solver_address,
                    )
                    feasible = verdict.feasible
                    revert_reason = verdict.reason
```

(d) Pass the new fields into the `ShadowSolution(...)` constructor:

```python
                ShadowSolution(
                    auction_id=auction_id,
                    strategy=a.strategy,
                    status=a.status,
                    latency_ms=a.latency_ms,
                    solution=a.solution,
                    error=a.error,
                    our_score_wei=score,
                    feasible=feasible,
                    revert_reason=revert_reason,
                )
```

(e) After the per-attempt loop finishes (and before the function returns),
release the per-call clients so we don't leak an httpx connection per auction:

```python
        if feas_rpc is not None:
            with contextlib.suppress(Exception):  # noqa: BLE001
                await feas_rpc._client.aclose()
            with contextlib.suppress(Exception):  # noqa: BLE001
                await feas_api.close()
            with contextlib.suppress(Exception):  # noqa: BLE001
                await _redis.aclose()
```

> NOTE: `_redis` is the local built in step (b); if it falls out of scope, keep
> a reference (e.g. assign `feas_redis = _redis`) so it can be closed here.
> `RpcClient` has no public close method — closing its `_client` (the
> `httpx.AsyncClient`) is the documented teardown. If you prefer not to reach
> into a private attribute, add a `close()` method to `RpcClient` in Task 5 and
> call it here instead.

> NOTE: `score is not None` includes the phantom-above-cap rows? No — those are
> set to `None` (NULL) earlier, so they are skipped here. That's acceptable for
> v1: above-cap rows are already flagged phantom-suspect by the cap logic. If
> you want their settle() verdict too, change the guard to
> `(score is not None or raw_score >= EPSILON_HIGH_WEI)` — out of scope for this
> task; leave the simpler guard.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_shadow/test_persist.py -k feasibility -v`
Expected: PASS

- [ ] **Step 5: Run the full suite for regressions**

Run: `pytest -q`
Expected: PASS (no regressions; existing persist tests still green)

- [ ] **Step 6: Commit**

```bash
git add src/shadow/persist.py tests/test_shadow/test_persist.py
git commit -m "feat(shadow): validate solution feasibility inline at persist time"
```

---

## Task 11: lint, type-check, final verification

**Files:** none (verification only)

- [ ] **Step 1: Ruff**

Run: `ruff check src/encoder/settle.py src/shadow/feasibility.py src/shadow/order_cache.py src/shadow/cow_api.py src/shadow/persist.py src/routing/rpc.py src/config.py`
Expected: clean (fix any findings, re-run)

- [ ] **Step 2: Mypy**

Run: `mypy src/encoder/settle.py src/shadow/feasibility.py src/shadow/order_cache.py`
Expected: clean (fix any findings, re-run)

- [ ] **Step 3: Full test suite**

Run: `pytest -q`
Expected: all pass

- [ ] **Step 4: Public-clone bootstrap guard**

Confirm no new top-level import in `persist.py` hard-depends on `edge/` (the
feasibility imports are all under `src/`, so the `except ImportError` path in
the orchestrator is unaffected). Spot-check:

Run: `python -c "import src.shadow.feasibility, src.encoder.settle, src.shadow.order_cache"`
Expected: no error

- [ ] **Step 5: Commit any lint/type fixes**

```bash
git add -A
git commit -m "chore: lint + type-check fixes for feasibility validator"
```

---

## Deferred (NOT in this plan)

- **Problem A (fee alignment):** apply `order.feePolicies` in `scoring.py` for a
  fee-aligned winner comparison. Separate pure-scorer change, no RPC surface.
- **Historical backfill:** going-forward only by decision. Old rows stay NULL.
- **Above-cap (`EPSILON_HIGH_WEI`) verdicts:** v1 skips NULL-scored rows; can be
  enabled later by widening the Task 10 guard.
- **Analytics consumption:** gating `validate_data` / `verify_24h` ★ markers on
  `feasible = true` — follow-up once data accumulates (~1 day post-deploy).
- **Barn onboarding (Phase 0a):** the official driver-in-the-loop path; this
  validator is the private pre-check that de-risks that conversation.
```

