from web3 import Web3
from web3.providers.rpc import HTTPProvider


class RpcClient:
    """Minimal sync RPC client for L2 reads. Web3.py is sync; we accept the cost."""

    def __init__(self, url: str) -> None:
        self._w3 = Web3(HTTPProvider(url, request_kwargs={"timeout": 5}))

    def gas_price(self) -> int:
        return int(self._w3.eth.gas_price)

    def block_number(self) -> int:
        return int(self._w3.eth.block_number)
