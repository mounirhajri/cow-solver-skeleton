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


async def test_fetch_competition_returns_winner() -> None:
    payload = json.dumps({
        "auctionId": 12345,
        "solutions": [
            {"solver": "barter", "ranking": 1, "score": "1000000000"},
            {"solver": "naive", "ranking": 2, "score": "950000000"},
        ],
    }).encode()

    with patch("urllib.request.urlopen", return_value=_mock_urlopen(payload)):
        client = CowApiClient(network="arbitrum_one")
        result = await client.fetch_competition(auction_id=12345)

    assert isinstance(result, CompetitionResult)
    assert result.winner_solver == "barter"
    assert result.winner_score == 1_000_000_000
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
