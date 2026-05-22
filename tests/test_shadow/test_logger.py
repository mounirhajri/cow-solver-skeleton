import json
from pathlib import Path

import pytest

from src.models.solution import Solution, Trade
from src.shadow.logger import SolutionLogger


def test_logger_appends_jsonl(tmp_path: Path) -> None:
    logger = SolutionLogger(path=tmp_path / "shadow.jsonl")
    sol = Solution(id=1, prices={}, trades=[Trade(kind="fulfillment", order_uid="0x" + "a"*112, executed_amount=10**18)], interactions=[])
    logger.record(auction_id="123", our_solution=sol)
    logger.record(auction_id="124", our_solution=None)

    lines = (tmp_path / "shadow.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["auction_id"] == "123"
    assert first["our_solution"]["id"] == 1
    assert "timestamp" in first

    second = json.loads(lines[1])
    assert second["our_solution"] is None
