from unittest.mock import MagicMock, patch

from src.routing.rpc import RpcClient


def test_gas_price_returns_int_wei() -> None:
    fake_web3 = MagicMock()
    fake_web3.eth.gas_price = 100_000_000  # 0.1 gwei
    with patch("src.routing.rpc.Web3", return_value=fake_web3):
        client = RpcClient("https://rpc.example")
        assert client.gas_price() == 100_000_000


def test_block_number() -> None:
    fake_web3 = MagicMock()
    fake_web3.eth.block_number = 12345
    with patch("src.routing.rpc.Web3", return_value=fake_web3):
        client = RpcClient("https://rpc.example")
        assert client.block_number() == 12345
