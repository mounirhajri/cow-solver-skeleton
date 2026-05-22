import httpx
import respx

from src.shadow.cow_api import CompetitionResult, CowApiClient


@respx.mock
async def test_fetch_competition_returns_winner() -> None:
    respx.get(
        "https://api.cow.fi/arbitrum_one/api/v1/solver_competition/12345"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "auctionId": 12345,
                "solutions": [
                    {"solver": "barter", "ranking": 1, "score": "1000000000"},
                    {"solver": "naive", "ranking": 2, "score": "950000000"},
                ],
            },
        )
    )
    client = CowApiClient(network="arbitrum_one")
    result = await client.fetch_competition(auction_id=12345)
    assert isinstance(result, CompetitionResult)
    assert result.winner_solver == "barter"
    assert result.winner_score == 1_000_000_000
    await client.close()


@respx.mock
async def test_fetch_competition_returns_none_on_404() -> None:
    respx.get(
        "https://api.cow.fi/arbitrum_one/api/v1/solver_competition/999"
    ).mock(return_value=httpx.Response(404))
    client = CowApiClient(network="arbitrum_one")
    result = await client.fetch_competition(auction_id=999)
    assert result is None
    await client.close()
