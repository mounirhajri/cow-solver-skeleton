from dataclasses import dataclass

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


@dataclass(frozen=True)
class OneInchQuote:
    dst_amount: int
    gas: int


class OneInchClient:
    """Async client for 1inch swap API v6."""

    BASE_URL = "https://api.1inch.dev/swap/v6.0"

    def __init__(self, api_key: str, chain_id: int, timeout: float = 5.0) -> None:
        self._chain_id = chain_id
        self._client = httpx.AsyncClient(
            base_url=f"{self.BASE_URL}/{chain_id}",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
        )

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.ReadTimeout)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.2, max=1.0),
    )
    async def quote(self, src: str, dst: str, amount: int) -> OneInchQuote:
        resp = await self._client.get(
            "/quote",
            params={"src": src, "dst": dst, "amount": str(amount)},
        )
        resp.raise_for_status()
        data = resp.json()
        return OneInchQuote(dst_amount=int(data["dstAmount"]), gas=int(data.get("gas", 0)))

    async def close(self) -> None:
        await self._client.aclose()
