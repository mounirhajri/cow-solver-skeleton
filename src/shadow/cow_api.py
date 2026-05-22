from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class CompetitionResult:
    auction_id: int
    winner_solver: str
    winner_score: int


class CowApiClient:
    """Client for the public CoW Orderbook API.

    Used in shadow phase to fetch the actual winning solver and score per auction,
    so we can compute our surplus delta.
    """

    BASE_BY_NETWORK = {
        "mainnet": "https://api.cow.fi/mainnet/api/v1",
        "arbitrum_one": "https://api.cow.fi/arbitrum_one/api/v1",
        "base": "https://api.cow.fi/base/api/v1",
        "gnosis": "https://api.cow.fi/xdai/api/v1",
    }

    def __init__(self, network: str = "arbitrum_one", timeout: float = 5.0) -> None:
        base = self.BASE_BY_NETWORK[network]
        self._client = httpx.AsyncClient(base_url=base, timeout=timeout)

    async def fetch_competition(self, auction_id: int) -> CompetitionResult | None:
        resp = await self._client.get(f"/solver_competition/{auction_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        solutions = data.get("solutions", [])
        winner = next((s for s in solutions if s.get("ranking") == 1), None)
        if not winner:
            return None
        return CompetitionResult(
            auction_id=int(data["auctionId"]),
            winner_solver=winner["solver"],
            winner_score=int(winner["score"]),
        )

    async def close(self) -> None:
        await self._client.aclose()
