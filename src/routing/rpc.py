import httpx
from web3 import Web3
from web3.providers.rpc import HTTPProvider


class RpcClient:
    """Minimal sync RPC client for L2 reads. Web3.py is sync; we accept the cost.

    Also exposes an async eth_call method backed by httpx, used by Multicall3
    to batch eth_calls without blocking the event loop.
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
        """Single eth_call. Returns 0x-prefixed hex string."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": to, "data": data}, block],
        }
        resp = await self._client.post(self.url, json=payload, timeout=5.0)
        return str(resp.json()["result"])
