"""JSONL-based surplus analyzer for shadow auction outcomes (legacy path).

Reads a JSONL log file written by the reconciler and returns aggregate stats.
For Postgres-backed analytics, see src.shadow.analyzer.
"""

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def analyze(log_path: Path) -> dict[str, Any]:
    total = 0
    our_attempts = 0
    winners_seen = 0
    winner_counts: Counter[str] = Counter()

    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            total += 1
            if rec.get("our_solution") is not None:
                our_attempts += 1
            w = rec.get("winner_solution")
            if w is not None:
                winners_seen += 1
                winner_counts[w["solver"]] += 1

    return {
        "total_auctions": total,
        "our_attempts": our_attempts,
        "winners_seen": winners_seen,
        "winner_counts": dict(winner_counts),
        "attempt_rate": our_attempts / total if total else 0.0,
    }


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m src.shadow.jsonl_analyzer <log.jsonl>", file=sys.stderr)
        return 2
    stats = analyze(Path(sys.argv[1]))
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
