import json
from pathlib import Path

from src.log import get_logger
from src.shadow.cow_api import CowApiClient

log = get_logger(__name__)


async def reconcile_once(log_path: Path, cow_api: CowApiClient) -> int:
    """Walks the JSONL file once, fills missing winner_solution entries.

    Returns the number of records updated.
    """
    if not log_path.exists():
        return 0

    records = [json.loads(line) for line in log_path.read_text().strip().splitlines() if line]
    updated = 0

    for rec in records:
        if rec.get("winner_solution") is not None:
            continue
        comp = await cow_api.fetch_competition(int(rec["auction_id"]))
        if comp is None:
            continue
        rec["winner_solution"] = {"solver": comp.winner_solver, "score": comp.winner_score}
        updated += 1

    if updated:
        # Rewrite atomically: write to temp then rename
        tmp = log_path.with_suffix(log_path.suffix + ".tmp")
        with tmp.open("w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
        tmp.replace(log_path)

    log.info("reconcile_done", updated=updated, total=len(records))
    return updated
