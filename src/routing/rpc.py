import asyncio
import weakref

import eth_abi
import httpx
from web3 import Web3
from web3.providers.rpc import HTTPProvider

from src.config import settings

# Retry schedule for HTTP 429 / JSON-RPC -32005 (rate limit).
# Waits: 200 ms, 600 ms, 1 800 ms  (×3 factor, 3 attempts max).
_RETRY_DELAYS = (0.2, 0.6, 1.8)
_RATE_LIMIT_CODES = {429, -32005}

# ── Global RPC concurrency gate ─────────────────────────────────────────────
# The shared node (PublicNode free tier) THROTTLES under concurrent eth_calls:
# a single V3 quote pass is ~3s clean, but 30 concurrent passes over one client
# fail 28/30 (429/-32005 after retries, or empty httpx timeouts). The
# concurrency is real in prod — naive's price_refiner fans out 3-10 parallel
# quotes, and the background feasibility validator (a SEPARATE RpcClient) runs
# for every auction overlapping the next /solve. So we bound TOTAL in-flight
# eth_calls process-wide with a semaphore shared across ALL RpcClient instances
# (no higher RPC tier — hard constraint).
#
# Keyed by event loop (via WeakKeyDictionary so finished test loops are GC'd):
# in prod there is exactly one uvicorn loop → one global gate; under
# pytest-asyncio's per-test loops each gets its own, avoiding "future attached
# to a different loop" errors. The semaphore binds lazily on first await
# (Python >=3.12), so reading settings here — after env load — is correct.
_rpc_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _rpc_gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _rpc_semaphores.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(settings.rpc_max_concurrent)
        _rpc_semaphores[loop] = sem
    return sem


def gate_stats() -> dict[str, int]:
    """Diagnostic snapshot of the per-loop RPC gate.

    Reads Semaphore internals (``_value``/``_waiters``) — private, but this
    is targeted observability for the 2026-06-12 degradation hunt: the
    solver decays into all-timeouts ~1h after every restart while CPU,
    memory and FDs stay idle — consistent with gate slots being lost over
    time. If ``slots_free`` reads 0 with a growing waiter queue while no
    solve is making progress, the leak is proven; if the gate looks healthy
    during degradation, the stall lives elsewhere (httpx pool next).
    Returns -1s when the gate was never created on this loop.
    """
    loop = asyncio.get_running_loop()
    sem = _rpc_semaphores.get(loop)
    if sem is None:
        return {"slots_free": -1, "waiters": -1}
    waiters = getattr(sem, "_waiters", None)
    return {
        "slots_free": getattr(sem, "_value", -1),
        "waiters": len(waiters) if waiters is not None else 0,
    }

# JSON-RPC error code 3 is the EVM "execution reverted" code (EIP-1474).
# Geth/Erigon also surface reverts under -32000 with a "revert" message, so we
# match on the message substring too. Anything else is an infra-class failure.
_REVERT_CODE = 3

# Some reverts are about OUR caller, not the solution. settle()'s onlySolver gate
# reverts with "GPv2: not a solver" when the eth_call `from` isn't an allowlisted
# solver — a config failure on our side. Treat these as infra (raise → UNKNOWN),
# never as a phantom solution, or every solution gets falsely condemned whenever
# the configured solver address isn't allowlisted.
_CALLER_AUTH_MARKERS = ("not a solver",)

# Selector of the canonical Solidity `Error(string)` revert (keccak("Error(string)")[:4]).
# Many providers (notably PublicNode) return a generic "execution reverted" message
# and put the real revert payload in error.data, ABI-encoded under this selector.
_ERROR_STRING_SELECTOR = "08c379a0"


def _decode_revert_data(data: object) -> str | None:
    """Best-effort decode of a JSON-RPC error ``data`` field into a human reason.

    Providers like PublicNode mask the message as a bare "execution reverted" and
    carry the real reason in ``error.data``. For the canonical Solidity
    ``Error(string)`` revert this is ABI-encoded; decode it back to the string
    (e.g. "STF", "Too little received", "GPv2: not a solver"). For a custom-error
    selector we cannot recover a string, so surface the raw hex so the selector is
    at least visible. Returns ``None`` when there is no usable data.

    Pure/offline and never raises — a decode failure degrades to ``None`` (revert
    detection itself stays driven by the JSON-RPC code/message, never by this).
    """
    # Some providers nest the payload under data.data / data.message.
    if isinstance(data, dict):
        inner = data.get("data") if data.get("data") is not None else data.get("message")
        return _decode_revert_data(inner) if inner is not None else None
    if not isinstance(data, str):
        return None
    hexs = (data[2:] if data.startswith("0x") else data).lower()
    if not hexs:
        return None
    # Canonical Error(string): selector + abi.encode(string).
    if hexs.startswith(_ERROR_STRING_SELECTOR) and len(hexs) >= len(_ERROR_STRING_SELECTOR) + 64:
        try:
            decoded = eth_abi.decode(["string"], bytes.fromhex(hexs[len(_ERROR_STRING_SELECTOR):]))
            text = str(decoded[0]).strip()
            return text or None
        except Exception:
            return None
    # Custom error or raw bytes — surface the selector/hex itself.
    return "0x" + hexs


def _is_revert(code: object, msg: str) -> bool:
    """True iff this JSON-RPC error denotes a genuine on-chain execution revert."""
    if code == _REVERT_CODE:
        return True
    low = msg.lower()
    return "revert" in low or "reverted" in low


def _is_caller_auth_revert(msg: str) -> bool:
    """True iff a revert is about our caller's authorization, not the solution."""
    low = msg.lower()
    return any(m in low for m in _CALLER_AUTH_MARKERS)


class RpcClient:
    """Minimal async RPC client for L2 reads.

    eth_call uses httpx directly to avoid blocking the asyncio event loop.
    Retries up to 3 times with exponential back-off on rate-limit responses
    (HTTP 429 or JSON-RPC error code -32005).
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._w3 = Web3(HTTPProvider(url, request_kwargs={"timeout": 5}))
        self._client = httpx.AsyncClient(timeout=5.0)

    def gas_price(self) -> int:
        return int(self._w3.eth.gas_price)

    def block_number(self) -> int:
        return int(self._w3.eth.block_number)

    async def eth_call(self, to: str, data: str, block: str = "latest") -> str:
        """Single eth_call. Returns 0x-prefixed hex string.

        Retries automatically on rate-limit errors (HTTP 429 / code -32005)
        with exponential back-off so transient limits don't abort a quote.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": to, "data": data}, block],
        }
        last_exc: Exception = RuntimeError("eth_call: no attempts made")
        for _attempt, delay in enumerate((*_RETRY_DELAYS, None), start=1):
            # Gate only the in-flight request; the backoff sleep below runs
            # OUTSIDE the gate so a retrying caller doesn't idle a slot.
            async with _rpc_gate():
                resp = await self._client.post(self.url, json=payload, timeout=5.0)

            # HTTP-level rate limit (some providers return 429 directly)
            if resp.status_code == 429:
                last_exc = RuntimeError("RPC error 429: Too Many Requests")
                if delay is not None:
                    await asyncio.sleep(delay)
                continue

            body = resp.json()
            if "error" in body:
                err = body["error"]
                code = err.get("code", "?") if isinstance(err, dict) else "?"
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                last_exc = RuntimeError(f"RPC error {code}: {msg}")
                # Retry on rate-limit JSON-RPC codes
                if code in _RATE_LIMIT_CODES and delay is not None:
                    await asyncio.sleep(delay)
                    continue
                raise last_exc  # non-retryable error — fail immediately

            if "result" not in body:
                raise RuntimeError(f"RPC response missing 'result': {body}")
            return str(body["result"])

        raise last_exc

    async def eth_call_capture(
        self,
        to: str,
        data: str,
        from_addr: str | None = None,
        block: str = "latest",
    ) -> tuple[bool, str]:
        """Like ``eth_call`` but returns a contract revert instead of raising.

        Returns ``(True, result_hex)`` on success and ``(False, reason)`` ONLY
        for a genuine on-chain execution revert (the solution is phantom).
        ``from_addr`` is injected into the call object — needed for
        ``onlySolver`` functions like settle() that gate on msg.sender.

        Infrastructure-class failures (non-revert JSON-RPC errors, a missing
        ``result`` field, or rate-limit exhaustion after all retries) RAISE
        ``RuntimeError``. This keeps the tri-state honest: the caller must map
        a raise to an UNKNOWN verdict, never to "phantom". Conflating our own
        fetch/RPC failure with an on-chain revert would falsely condemn a
        solution as infeasible.

        Rate-limit errors (429 / -32005) are retried with the same back-off as
        ``eth_call``.
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
        last_exc: Exception = RuntimeError("eth_call: no attempts made")
        for _attempt, delay in enumerate((*_RETRY_DELAYS, None), start=1):
            # Gate only the in-flight request (see eth_call); backoff is outside.
            async with _rpc_gate():
                resp = await self._client.post(self.url, json=payload, timeout=5.0)

            if resp.status_code == 429:
                last_exc = RuntimeError("RPC error 429: Too Many Requests")
                if delay is not None:
                    await asyncio.sleep(delay)
                continue

            body = resp.json()
            if "error" in body:
                err = body["error"]
                code = err.get("code", "?") if isinstance(err, dict) else "?"
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                if code in _RATE_LIMIT_CODES and delay is not None:
                    last_exc = RuntimeError(f"RPC error {code}: {msg}")
                    await asyncio.sleep(delay)
                    continue
                # Distinguish a genuine contract revert (→ phantom, (False, reason))
                # from any other RPC error (→ infra, raise → UNKNOWN verdict).
                if _is_revert(code, msg):
                    # The real reason often hides in err.data (providers mask the
                    # message as a bare "execution reverted"). Surface it so the
                    # stored revert_reason is diagnostic, not the generic string.
                    detail = _decode_revert_data(err.get("data")) if isinstance(err, dict) else None
                    reason = f"{msg}: {detail}" if detail and detail not in msg else msg
                    # ...unless the revert is the onlySolver gate rejecting OUR
                    # caller: that's our misconfiguration, not a phantom solution.
                    # Inspect the decoded reason — the marker may live in err.data.
                    if _is_caller_auth_revert(reason):
                        raise RuntimeError(
                            f"RPC error {code}: {reason} "
                            "(eth_call `from` is not an allowlisted solver — "
                            "config issue, not a phantom solution)"
                        )
                    return False, reason
                raise RuntimeError(f"RPC error {code}: {msg}")

            if "result" not in body:
                raise RuntimeError(f"RPC response missing 'result': {body}")
            return True, str(body["result"])

        raise last_exc
