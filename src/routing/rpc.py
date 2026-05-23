import asyncio

import httpx
from web3 import Web3
from web3.providers.rpc import HTTPProvider

# Retry schedule for HTTP 429 / JSON-RPC -32005 (rate limit).
# Waits: 200 ms, 600 ms, 1 800 ms  (×3 factor, 3 attempts max).
_RETRY_DELAYS = (0.2, 0.6, 1.8)
_RATE_LIMIT_CODES = {429, -32005}


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
        for attempt, delay in enumerate((*_RETRY_DELAYS, None), start=1):
            resp = await self._client.post(self.url, json=payload, timeout=5.0)

            # HTTP-level rate limit (some providers return 429 directly)
            if resp.status_code == 429:
                last_exc = RuntimeError(f"RPC error 429: Too Many Requests")
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
