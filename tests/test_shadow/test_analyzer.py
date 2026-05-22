import json
from pathlib import Path

from src.shadow.analyzer import analyze


def test_analyzer_reports_basic_stats(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    lines = [
        {"auction_id": "1", "our_solution": {"id": 1, "trades": [{"kind": "fulfillment"}]}, "winner_solution": {"solver": "barter"}, "timestamp": "2026-05-22T12:00:00+00:00"},
        {"auction_id": "2", "our_solution": None, "winner_solution": {"solver": "rizzolver"}, "timestamp": "2026-05-22T12:01:00+00:00"},
        {"auction_id": "3", "our_solution": {"id": 3, "trades": []}, "winner_solution": None, "timestamp": "2026-05-22T12:02:00+00:00"},
    ]
    log_path.write_text("\n".join(json.dumps(l) for l in lines))

    stats = analyze(log_path)
    assert stats["total_auctions"] == 3
    assert stats["our_attempts"] == 2  # 1 + 3 had our_solution non-None
    assert stats["winners_seen"] == 2  # 1 + 2 had winner
    assert stats["winner_counts"] == {"barter": 1, "rizzolver": 1}
