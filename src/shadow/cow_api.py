import asyncio
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

_UA = "curl/8.5.0"
_SSL_CTX = ssl.create_default_context()


@dataclass(frozen=True)
class CompetitionResult:
    auction_id: int
    winner_solver: str
    winner_score: int


class CowApiClient:
    """Client for the public CoW Orderbook API.

    Uses urllib (not httpx) — the CoW API blocks httpx's TLS fingerprint.
    """

    BASE_BY_NETWORK = {
        "mainnet": "https://api.cow.fi/mainnet/api/v1",
        "arbitrum_one": "https://api.cow.fi/arbitrum_one/api/v1",
        "base": "https://api.cow.fi/base/api/v1",
        "gnosis": "https://api.cow.fi/xdai/api/v1",
    }

    def __init__(self, network: str = "arbitrum_one") -> None:
        self._base = self.BASE_BY_NETWORK[network]

    def _get(self, path: str) -> "dict[str, Any] | None":
        req = urllib.request.Request(
            f"{self._base}{path}", headers={"User-Agent": _UA}
        )
        try:
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=10) as resp:
                result: dict[str, Any] = json.loads(resp.read())
                return result
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    async def fetch_competition(self, auction_id: int) -> CompetitionResult | None:
        data = await asyncio.to_thread(self._get, f"/solver_competition/{auction_id}")
        if data is None:
            return None
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
        pass  # no persistent connection to close
