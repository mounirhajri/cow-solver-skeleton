from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.routing.rpc import RpcClient


def _mock_client(responses: list) -> MagicMock:
    """Build a patched httpx.AsyncClient whose post() returns responses in sequence."""
    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=responses)
    return mock_client


def _resp(body: dict, status: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json = lambda: body
    return r


# ── Happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_eth_call_returns_result_on_success() -> None:
    fake_web3 = MagicMock()
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client([
            _resp({"jsonrpc": "2.0", "id": 1, "result": "0xdeadbeef"})
        ])
        client = RpcClient("https://rpc.example")
        result = await client.eth_call("0xabc", "0x1234")
    assert result == "0xdeadbeef"


# ── Error handling ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_eth_call_raises_immediately_on_non_rate_limit_rpc_error() -> None:
    """Non-retryable RPC errors (e.g. execution reverted) raise right away."""
    fake_web3 = MagicMock()
    error_body = {"jsonrpc": "2.0", "id": 1,
                  "error": {"code": -32000, "message": "execution reverted"}}
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_cls:
        mc = _mock_client([_resp(error_body)])
        mock_cls.return_value = mc
        client = RpcClient("https://rpc.example")
        with pytest.raises(RuntimeError, match="RPC error -32000"):
            await client.eth_call("0xabc", "0x1234")
    # Should not have retried
    assert mc.post.call_count == 1


@pytest.mark.asyncio
async def test_eth_call_raises_on_missing_result() -> None:
    fake_web3 = MagicMock()
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client([
            _resp({"jsonrpc": "2.0", "id": 1})
        ])
        client = RpcClient("https://rpc.example")
        with pytest.raises(RuntimeError, match="missing 'result'"):
            await client.eth_call("0xabc", "0x1234")


# ── Retry logic ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_eth_call_retries_on_http_429_and_succeeds() -> None:
    """HTTP 429 is retried; succeeds on 2nd attempt."""
    fake_web3 = MagicMock()
    responses = [
        _resp({}, status=429),
        _resp({"jsonrpc": "2.0", "id": 1, "result": "0xaabb"}),
    ]
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.asyncio.sleep") as mock_sleep, \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_cls:
        mc = _mock_client(responses)
        mock_cls.return_value = mc
        client = RpcClient("https://rpc.example")
        result = await client.eth_call("0xabc", "0x1234")
    assert result == "0xaabb"
    assert mc.post.call_count == 2
    mock_sleep.assert_called_once()  # slept once between attempts


@pytest.mark.asyncio
async def test_eth_call_retries_on_jsonrpc_rate_limit_code() -> None:
    """JSON-RPC error code -32005 (rate limit) is retried; succeeds on 3rd attempt."""
    fake_web3 = MagicMock()
    rate_err = {"jsonrpc": "2.0", "id": 1,
                "error": {"code": -32005, "message": "rate limit"}}
    responses = [
        _resp(rate_err),
        _resp(rate_err),
        _resp({"jsonrpc": "2.0", "id": 1, "result": "0xcafe"}),
    ]
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.asyncio.sleep") as mock_sleep, \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_cls:
        mc = _mock_client(responses)
        mock_cls.return_value = mc
        client = RpcClient("https://rpc.example")
        result = await client.eth_call("0xabc", "0x1234")
    assert result == "0xcafe"
    assert mc.post.call_count == 3
    assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_eth_call_raises_after_all_retries_exhausted() -> None:
    """If all 4 attempts return 429, raises RuntimeError with the 429 message."""
    fake_web3 = MagicMock()
    responses = [_resp({}, status=429)] * 4
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.asyncio.sleep"), \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_cls:
        mc = _mock_client(responses)
        mock_cls.return_value = mc
        client = RpcClient("https://rpc.example")
        with pytest.raises(RuntimeError, match="429"):
            await client.eth_call("0xabc", "0x1234")
    assert mc.post.call_count == 4  # initial + 3 retries


# ── Sync helpers ──────────────────────────────────────────────────────────────

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
