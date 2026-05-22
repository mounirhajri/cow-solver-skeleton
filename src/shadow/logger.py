import json
from datetime import UTC, datetime
from pathlib import Path

from src.models.solution import Solution


class SolutionLogger:
    """Append-only JSONL log of (auction_id, our_solution, winner_solution).

    Winner is filled in later by a background reconciliation task that polls
    the CoW Orderbook API (Task 14).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, auction_id: str, our_solution: Solution | None) -> None:
        record = {
            "auction_id": auction_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "our_solution": (
                our_solution.model_dump(by_alias=True, mode="json") if our_solution else None
            ),
            "winner_solution": None,  # filled later
        }
        with self._path.open("a") as f:
            f.write(json.dumps(record) + "\n")
