from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.routing.rpc import RpcClient, _decode_revert_data

# Canonical Solidity Error(string) reverts: selector 08c379a0 + abi.encode(string).
# Many providers (e.g. PublicNode) return a generic "execution reverted" message
# and put the real reason here in error.data. Fixtures generated via eth_abi.
_ERR_STF = (
    "0x08c379a0"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000003"
    "5354460000000000000000000000000000000000000000000000000000000000"
)
_ERR_NOT_A_SOLVER = (
    "0x08c379a0"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000012"
    "475076323a206e6f74206120736f6c7665720000000000000000000000000000"
)


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


# ── eth_call_capture ──────────────────────────────────────────────────────────

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
async def test_eth_call_capture_raises_on_non_revert_rpc_error() -> None:
    """A non-revert JSON-RPC error (infra) RAISES rather than reporting phantom."""
    fake_web3 = MagicMock()
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client([
            _resp({"jsonrpc": "2.0", "id": 1,
                   "error": {"code": -32602, "message": "invalid argument 0"}})
        ])
        client = RpcClient("https://rpc.example")
        with pytest.raises(RuntimeError, match="-32602"):
            await client.eth_call_capture("0xto", "0xdata")


@pytest.mark.asyncio
async def test_eth_call_capture_raises_on_not_a_solver_revert() -> None:
    """`GPv2: not a solver` is a caller-auth (config) revert, NOT a phantom.

    The onlySolver gate rejects our eth_call `from` when it isn't allowlisted.
    This MUST raise (→ UNKNOWN verdict), never report (False, reason): otherwise
    every solution is falsely condemned as phantom whenever the configured solver
    address isn't on the allowlist.
    """
    fake_web3 = MagicMock()
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client([
            _resp({"jsonrpc": "2.0", "id": 1,
                   "error": {"code": 3, "message": "execution reverted: GPv2: not a solver"}})
        ])
        client = RpcClient("https://rpc.example")
        with pytest.raises(RuntimeError, match="not a solver"):
            await client.eth_call_capture("0xto", "0xdata", from_addr="0xunallowlisted")


@pytest.mark.asyncio
async def test_eth_call_capture_invalid_signature_stays_phantom() -> None:
    """A real solution revert (bad signature) is still reported as phantom."""
    fake_web3 = MagicMock()
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client([
            _resp({"jsonrpc": "2.0", "id": 1,
                   "error": {"code": 3, "message": "execution reverted: GPv2: invalid signature"}})
        ])
        client = RpcClient("https://rpc.example")
        ok, payload = await client.eth_call_capture("0xto", "0xdata", from_addr="0xsolver")
    assert ok is False
    assert "invalid signature" in payload


@pytest.mark.asyncio
async def test_eth_call_capture_raises_on_missing_result() -> None:
    """Missing 'result' (malformed/infra) RAISES rather than reporting phantom."""
    fake_web3 = MagicMock()
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client([
            _resp({"jsonrpc": "2.0", "id": 1})
        ])
        client = RpcClient("https://rpc.example")
        with pytest.raises(RuntimeError, match="missing 'result'"):
            await client.eth_call_capture("0xto", "0xdata")


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


# ── _decode_revert_data ───────────────────────────────────────────────────────

def test_decode_revert_data_error_string() -> None:
    """Canonical Error(string) data decodes back to the human reason."""
    assert _decode_revert_data(_ERR_STF) == "STF"


def test_decode_revert_data_custom_selector_surfaces_hex() -> None:
    """A custom-error selector (no decodable string) surfaces the raw hex."""
    custom = "0xdeadbeef"
    assert _decode_revert_data(custom) == "0xdeadbeef"


def test_decode_revert_data_empty_returns_none() -> None:
    assert _decode_revert_data("0x") is None
    assert _decode_revert_data("") is None
    assert _decode_revert_data(None) is None


def test_decode_revert_data_nested_dict() -> None:
    """Some providers nest the payload under data.data — unwrap it."""
    assert _decode_revert_data({"data": _ERR_STF}) == "STF"


@pytest.mark.asyncio
async def test_eth_call_capture_surfaces_err_data_reason() -> None:
    """A generic 'execution reverted' message + Error(string) data → decoded reason.

    This is the PublicNode shape: the message is generic and the real revert
    string lives in error.data. Without decoding it, every such revert is stored
    as the useless 'execution reverted' (the bare-revert capture artifact).
    """
    fake_web3 = MagicMock()
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client([
            _resp({"jsonrpc": "2.0", "id": 1,
                   "error": {"code": 3, "message": "execution reverted",
                             "data": _ERR_STF}})
        ])
        client = RpcClient("https://rpc.example")
        ok, payload = await client.eth_call_capture("0xto", "0xdata", from_addr="0xsolver")
    assert ok is False
    assert "STF" in payload


@pytest.mark.asyncio
async def test_eth_call_capture_caller_auth_detected_in_err_data() -> None:
    """`not a solver` hidden in err.data (generic message) must still RAISE.

    PublicNode masks the message as 'execution reverted' and puts the caller-auth
    string in data. The caller-auth guard must inspect the decoded reason, else a
    config misconfiguration would be falsely reported as a phantom solution.
    """
    fake_web3 = MagicMock()
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client([
            _resp({"jsonrpc": "2.0", "id": 1,
                   "error": {"code": 3, "message": "execution reverted",
                             "data": _ERR_NOT_A_SOLVER}})
        ])
        client = RpcClient("https://rpc.example")
        with pytest.raises(RuntimeError, match="not a solver"):
            await client.eth_call_capture("0xto", "0xdata", from_addr="0xunallowlisted")


@pytest.mark.asyncio
async def test_eth_call_capture_raises_after_retries_exhausted() -> None:
    """All 4 attempts rate-limited → RAISES (infra failure, not a revert).

    Rate-limit exhaustion must NOT be reported as (False, reason): that would
    record the solution as phantom. It is an infra-class failure, so it raises
    and validate_solution maps it to an UNKNOWN (None) verdict.
    """
    fake_web3 = MagicMock()
    responses = [_resp({}, status=429)] * 4
    with patch("src.routing.rpc.Web3", return_value=fake_web3), \
         patch("src.routing.rpc.asyncio.sleep"), \
         patch("src.routing.rpc.httpx.AsyncClient") as mock_cls:
        mc = _mock_client(responses)
        mock_cls.return_value = mc
        client = RpcClient("https://rpc.example")
        with pytest.raises(RuntimeError, match="429"):
            await client.eth_call_capture("0xto", "0xdata")
    assert mc.post.call_count == 4  # initial + 3 retries
