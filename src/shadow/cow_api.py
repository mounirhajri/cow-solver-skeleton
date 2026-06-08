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
        # CoW moved the solver_competition endpoints to /api/v2 in 2026-06;
        # the old /api/v1/solver_competition/* now 404s (this silently killed
        # the shadow poller for ~6 days). The order endpoints (/orders/...)
        # are STILL v1, so we keep a separate v2 base only for competition
        # calls rather than bumping the whole client to v2.
        self._comp_base = self._base.replace("/api/v1", "/api/v2")

    def _get(self, path: str, base: str | None = None) -> "dict[str, Any] | None":
        req = urllib.request.Request(
            f"{base or self._base}{path}", headers={"User-Agent": _UA}
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
        data = await asyncio.to_thread(
            self._get, f"/solver_competition/{auction_id}", self._comp_base
        )
        if data is None:
            return None
        solutions = data.get("solutions", [])
        winner = next(
            (s for s in solutions if s.get("isWinner") or s.get("ranking") == 1), None
        )
        if not winner:
            return None
        # v2 dropped the human-readable ``solver`` name field; only
        # ``solverAddress`` remains. Fall back so the identifier is never empty.
        return CompetitionResult(
            auction_id=int(data["auctionId"]),
            winner_solver=str(winner.get("solver") or winner.get("solverAddress") or "unknown"),
            winner_score=int(winner.get("score") or 0),
        )

    async def fetch_order(self, uid: str) -> dict[str, Any] | None:
        """Fetch a single order (signature + signingScheme included).

        Hits the public, ungated ``GET /orders/{uid}`` endpoint — unlike
        ``/auction`` this is open to anyone. Returns the raw order dict, or
        ``None`` on 404 (order purged after settlement).
        """
        return await asyncio.to_thread(self._get, f"/orders/{uid}")

    async def close(self) -> None:
        pass  # no persistent connection to close
