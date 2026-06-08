import json
from unittest.mock import MagicMock, patch

from src.shadow.cow_api import CompetitionResult, CowApiClient


def _mock_urlopen(body: bytes, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    resp.read.return_value = body
    resp.status = status
    return resp


async def test_fetch_competition_returns_winner_v2_schema() -> None:
    # v2 solver_competition: no ``solver`` name field, only ``solverAddress``;
    # winner identified by ``isWinner``. Verify we hit /api/v2 and fall back to
    # the address as the solver identifier.
    payload = json.dumps({
        "auctionId": 12345,
        "solutions": [
            {"solverAddress": "0xabc", "ranking": 1, "isWinner": True, "score": "1000000000"},
            {"solverAddress": "0xdef", "ranking": 2, "isWinner": False, "score": "950000000"},
        ],
    }).encode()

    with patch("urllib.request.urlopen", return_value=_mock_urlopen(payload)) as m:
        client = CowApiClient(network="arbitrum_one")
        result = await client.fetch_competition(auction_id=12345)

    # The competition call must target /api/v2 (v1 now 404s).
    called_url = m.call_args.args[0].full_url
    assert "/api/v2/solver_competition/12345" in called_url
    assert isinstance(result, CompetitionResult)
    assert result.winner_solver == "0xabc"  # fell back to solverAddress
    assert result.winner_score == 1_000_000_000
    await client.close()


async def test_fetch_competition_prefers_solver_name_when_present() -> None:
    # Defensive: if a (cached/older) payload still carries ``solver``, prefer it.
    payload = json.dumps({
        "auctionId": 1,
        "solutions": [
            {"solver": "barter", "solverAddress": "0xabc", "ranking": 1, "score": "5"},
        ],
    }).encode()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(payload)):
        client = CowApiClient(network="arbitrum_one")
        result = await client.fetch_competition(auction_id=1)
    assert result is not None
    assert result.winner_solver == "barter"
    await client.close()


async def test_fetch_competition_missing_score_defaults_zero() -> None:
    # A winner row without a score must not crash (int(None) → ValueError);
    # `score or 0` keeps it at 0.
    payload = json.dumps({
        "auctionId": 7,
        "solutions": [{"solverAddress": "0xabc", "isWinner": True}],
    }).encode()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(payload)):
        client = CowApiClient(network="arbitrum_one")
        result = await client.fetch_competition(auction_id=7)
    assert result is not None
    assert result.winner_score == 0
    await client.close()


async def test_fetch_order_uses_v1_base() -> None:
    # Orders stay on /api/v1 even though competition moved to v2.
    payload = json.dumps({"uid": "0xabc", "signature": "0x"}).encode()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(payload)) as m:
        client = CowApiClient(network="arbitrum_one")
        await client.fetch_order("0xabc")
    called_url = m.call_args.args[0].full_url
    assert "/api/v1/orders/0xabc" in called_url
    await client.close()


async def test_fetch_competition_returns_none_on_404() -> None:
    import urllib.error

    http_err = urllib.error.HTTPError(url="", code=404, msg="Not Found", hdrs=MagicMock(), fp=None)  # type: ignore[arg-type]

    with patch("urllib.request.urlopen", side_effect=http_err):
        client = CowApiClient(network="arbitrum_one")
        result = await client.fetch_competition(auction_id=999)

    assert result is None
    await client.close()


async def test_fetch_order_returns_order_with_signature() -> None:
    payload = json.dumps({
        "uid": "0xabc",
        "sellToken": "0x1111111111111111111111111111111111111111",
        "buyToken": "0x2222222222222222222222222222222222222222",
        "sellAmount": "1000",
        "buyAmount": "900",
        "validTo": 1900000000,
        "appData": "0x" + "00" * 32,
        "feeAmount": "0",
        "kind": "sell",
        "partiallyFillable": False,
        "receiver": "0x3333333333333333333333333333333333333333",
        "signingScheme": "eip712",
        "signature": "0x" + "ab" * 65,
    }).encode()

    with patch("urllib.request.urlopen", return_value=_mock_urlopen(payload)):
        client = CowApiClient(network="arbitrum_one")
        order = await client.fetch_order("0xabc")

    assert order is not None
    assert order["signature"] == "0x" + "ab" * 65
    assert order["signingScheme"] == "eip712"
    await client.close()


async def test_fetch_order_returns_none_on_404() -> None:
    import urllib.error

    http_err = urllib.error.HTTPError(url="", code=404, msg="Not Found", hdrs=MagicMock(), fp=None)  # type: ignore[arg-type]

    with patch("urllib.request.urlopen", side_effect=http_err):
        client = CowApiClient(network="arbitrum_one")
        order = await client.fetch_order("0xdead")

    assert order is None
    await client.close()
