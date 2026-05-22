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
