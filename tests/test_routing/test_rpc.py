import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.routing.rpc import RpcClient


@pytest.mark.asyncio
async def test_eth_call_returns_result_on_success() -> None:
    fake_web3 = MagicMock()
    mock_post = AsyncMock(return_value=MagicMock(
        json=lambda: {"jsonrpc": "2.0", "id": 1, "result": "0xdeadbeef"}
    ))
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.post = mock_post
        client = RpcClient("https://rpc.example")
        result = await client.eth_call("0xabc", "0x1234")
    assert result == "0xdeadbeef"


@pytest.mark.asyncio
async def test_eth_call_raises_on_rpc_error() -> None:
    """RPC error response (e.g. rate limit) raises RuntimeError, not KeyError."""
    fake_web3 = MagicMock()
    error_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32005, "message": "rate limit exceeded"},
    }
    mock_post = AsyncMock(return_value=MagicMock(json=lambda: error_body))
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.post = mock_post
        client = RpcClient("https://rpc.example")
        with pytest.raises(RuntimeError, match="RPC error -32005"):
            await client.eth_call("0xabc", "0x1234")


@pytest.mark.asyncio
async def test_eth_call_raises_on_missing_result() -> None:
    """Malformed response (no 'result', no 'error') raises RuntimeError."""
    fake_web3 = MagicMock()
    mock_post = AsyncMock(return_value=MagicMock(json=lambda: {"jsonrpc": "2.0", "id": 1}))
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.post = mock_post
        client = RpcClient("https://rpc.example")
        with pytest.raises(RuntimeError, match="missing 'result'"):
            await client.eth_call("0xabc", "0x1234")


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
