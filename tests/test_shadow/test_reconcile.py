import json
from pathlib import Path
from unittest.mock import AsyncMock

from src.shadow.cow_api import CompetitionResult
from src.shadow.reconcile import reconcile_once


async def test_reconcile_fills_winner(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    lines = [
        {"auction_id": "1", "our_solution": None, "winner_solution": None, "timestamp": "t1"},
        {"auction_id": "2", "our_solution": {"id": 2}, "winner_solution": None, "timestamp": "t2"},
        {
            "auction_id": "3",
            "our_solution": None,
            "winner_solution": {"solver": "x", "score": 1},
            "timestamp": "t3",
        },
    ]
    log_path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

    cow_api = AsyncMock()
    cow_api.fetch_competition.side_effect = [
        CompetitionResult(auction_id=1, winner_solver="barter", winner_score=100),
        CompetitionResult(auction_id=2, winner_solver="rizzolver", winner_score=200),
    ]

    updated = await reconcile_once(log_path, cow_api)
    assert updated == 2

    result_lines = [json.loads(ln) for ln in log_path.read_text().strip().splitlines()]
    assert result_lines[0]["winner_solution"] == {"solver": "barter", "score": 100}
    assert result_lines[1]["winner_solution"] == {"solver": "rizzolver", "score": 200}
    assert result_lines[2]["winner_solution"] == {"solver": "x", "score": 1}  # unchanged


async def test_reconcile_skips_missing_competition(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    record = {
        "auction_id": "99",
        "our_solution": None,
        "winner_solution": None,
        "timestamp": "t",
    }
    log_path.write_text(json.dumps(record) + "\n")
    cow_api = AsyncMock()
    cow_api.fetch_competition.return_value = None

    updated = await reconcile_once(log_path, cow_api)
    assert updated == 0
