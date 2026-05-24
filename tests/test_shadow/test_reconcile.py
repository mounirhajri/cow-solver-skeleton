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


async def test_reconcile_skips_corrupt_lines(tmp_path: Path) -> None:
    """A malformed JSONL line must not crash the loop. Valid neighbours still
    process; on rewrite, the corrupt line is dropped so the next iteration
    doesn't re-encounter it."""
    log_path = tmp_path / "log.jsonl"
    good_a = {"auction_id": "1", "winner_solution": None}
    good_b = {"auction_id": "2", "winner_solution": None}
    log_path.write_text(
        "\n".join([
            json.dumps(good_a),
            '{"auction_id": "X", "winner_solution":',  # truncated mid-write
            json.dumps(good_b),
        ]) + "\n"
    )

    cow_api = AsyncMock()
    cow_api.fetch_competition.side_effect = [
        CompetitionResult(auction_id=1, winner_solver="x", winner_score=10),
        CompetitionResult(auction_id=2, winner_solver="y", winner_score=20),
    ]

    updated = await reconcile_once(log_path, cow_api)
    assert updated == 2

    out = [json.loads(ln) for ln in log_path.read_text().strip().splitlines()]
    assert len(out) == 2  # corrupt line dropped on rewrite
    assert out[0]["winner_solution"] == {"solver": "x", "score": 10}
    assert out[1]["winner_solution"] == {"solver": "y", "score": 20}


async def test_reconcile_corrupt_only_still_rewrites(tmp_path: Path) -> None:
    """When the only change is dropping corrupt lines (no winner updates),
    we still rewrite so the bad lines don't trigger the same warning forever."""
    log_path = tmp_path / "log.jsonl"
    good = {"auction_id": "1", "winner_solution": {"solver": "x", "score": 1}}
    log_path.write_text(
        "\n".join([json.dumps(good), '{"broken'])  + "\n"
    )

    cow_api = AsyncMock()
    cow_api.fetch_competition.return_value = None

    await reconcile_once(log_path, cow_api)
    out = log_path.read_text().strip().splitlines()
    assert len(out) == 1
    assert json.loads(out[0]) == good


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
