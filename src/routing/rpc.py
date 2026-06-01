import asyncio

import httpx
from web3 import Web3
from web3.providers.rpc import HTTPProvider

# Retry schedule for HTTP 429 / JSON-RPC -32005 (rate limit).
# Waits: 200 ms, 600 ms, 1 800 ms  (×3 factor, 3 attempts max).
_RETRY_DELAYS = (0.2, 0.6, 1.8)
_RATE_LIMIT_CODES = {429, -32005}

# JSON-RPC error code 3 is the EVM "execution reverted" code (EIP-1474).
# Geth/Erigon also surface reverts under -32000 with a "revert" message, so we
# match on the message substring too. Anything else is an infra-class failure.
_REVERT_CODE = 3


def _is_revert(code: object, msg: str) -> bool:
    """True iff this JSON-RPC error denotes a genuine on-chain execution revert."""
    if code == _REVERT_CODE:
        return True
    low = msg.lower()
    return "revert" in low or "reverted" in low


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
                # Distinguish a genuine contract revert (→ phantom, (False, msg))
                # from any other RPC error (→ infra, raise → UNKNOWN verdict).
                if _is_revert(code, msg):
                    return False, msg
                raise RuntimeError(f"RPC error {code}: {msg}")

            if "result" not in body:
                raise RuntimeError(f"RPC response missing 'result': {body}")
            return True, str(body["result"])

        raise last_exc
