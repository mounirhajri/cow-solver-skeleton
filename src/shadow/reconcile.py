import json
from pathlib import Path
from typing import Any

from src.log import get_logger
from src.shadow.cow_api import CowApiClient

log = get_logger(__name__)


def _parse_jsonl_resilient(log_path: Path) -> tuple[list[dict[str, Any]], int]:
    """Parse a JSONL file, skipping (and logging) any malformed lines.

    A truncated or otherwise corrupted line — e.g. half-written from a
    container kill or disk-full event — must NOT kill the reconcile loop.
    Returns (parsed_records, n_skipped).
    """
    parsed: list[dict[str, Any]] = []
    n_skipped = 0
    for lineno, raw in enumerate(log_path.read_text().splitlines(), start=1):
        if not raw:
            continue
        try:
            parsed.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            n_skipped += 1
            # Log only first ~120 chars of the line so we keep evidence in
            # the journal without flooding it on a bulk corruption event.
            log.warning(
                "reconcile_bad_line",
                lineno=lineno,
                length=len(raw),
                error=str(exc),
                preview=raw[:120],
            )
    return parsed, n_skipped


async def reconcile_once(log_path: Path, cow_api: CowApiClient) -> int:
    """Walks the JSONL file once, fills missing winner_solution entries.

    Returns the number of records updated.  Corrupt lines are logged
    and skipped — the rewrite drops them, healing the file.
    """
    if not log_path.exists():
        return 0

    records, n_skipped = _parse_jsonl_resilient(log_path)
    updated = 0

    for rec in records:
        if rec.get("winner_solution") is not None:
            continue
        comp = await cow_api.fetch_competition(int(rec["auction_id"]))
        if comp is None:
            continue
        rec["winner_solution"] = {"solver": comp.winner_solver, "score": comp.winner_score}
        updated += 1

    # Rewrite if we either filled in winners OR observed corrupt lines that
    # need pruning.  Without this, bad lines persist forever and the warning
    # log fills up on every iteration.
    if updated or n_skipped:
        tmp = log_path.with_suffix(log_path.suffix + ".tmp")
        with tmp.open("w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
        tmp.replace(log_path)

    log.info(
        "reconcile_done",
        updated=updated,
        total=len(records),
        skipped_corrupt=n_skipped,
    )
    return updated
