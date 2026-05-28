"""Parse cow-driver container logs to summarise interaction-validation
outcomes per auction.

Designed to run on the Hetzner host right after starting the cow-stack
profile: it answers "is the driver actually accepting our solutions" or
"are we being rejected, and why" without needing to hand-grep verbose
Rust log output.

Usage:
    # Last 5 min of cow-driver logs, default summary
    python3 scripts/cow_driver_log_diagnose.py --since 5m

    # Full breakdown including per-auction outcomes
    python3 scripts/cow_driver_log_diagnose.py --since 24h --full

The script does not connect to the database — it shells out to
``docker logs cow-driver`` and parses the stream. This keeps it usable
even when the DB is briefly unreachable.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict


# Heuristic patterns. The CoW reference driver is in Rust and uses
# tracing-style structured logging; the exact event names below were
# inferred from the OpenAPI notify enum + the reference driver source
# tree. If a deploy reveals different actual names, update here.
_PATTERN_AUCTION_RECEIVED = re.compile(r"received auction\s+id=([\w-]+)")
_PATTERN_SOLVER_CALLED = re.compile(r"solving auction\s+id=([\w-]+)")
_PATTERN_SOLUTION_RECEIVED = re.compile(
    r"received (\d+) solutions? from solver\s+(\w+)"
)
_PATTERN_VALIDATION_FAIL = re.compile(
    r"(?i)(invalid|rejected|simulation\s*failed|missing\s+\w+|"
    r"invalid_clearing_prices|invalid_executed_amount|missing_price|"
    r"non_bufferable_tokens|insufficient_balance|driver_error)"
)
_PATTERN_INTERACTION_KIND = re.compile(r"interaction.*kind|kind.*interaction")
_PATTERN_SIMULATED_OK = re.compile(r"simulation\s+(?:succeeded|ok)|gas:\s*\d+")


def _read_logs(since: str) -> str:
    """Shell out to docker logs. Returns combined stdout+stderr — the
    driver writes most non-error events to stderr in Rust tracing format."""
    try:
        result = subprocess.run(
            ["docker", "logs", "--since", since, "cow-driver"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("ERROR: docker logs cow-driver timed out after 30s", file=sys.stderr)
        sys.exit(2)
    except FileNotFoundError:
        print("ERROR: docker command not found", file=sys.stderr)
        sys.exit(2)
    return (result.stdout or "") + "\n" + (result.stderr or "")


def _extract_json_payload(line: str) -> dict | None:
    """Some driver logs emit a trailing JSON object. Best-effort extract."""
    m = re.search(r"\{.*\}", line)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def analyze(log_text: str, full: bool = False) -> dict:
    """Parse the log dump and return aggregate counts.

    Counts are conservative (event-pattern matches) so a log format the
    script does not recognise simply yields zero — that's a clearer
    failure mode than fabricating numbers. Pair with ``--full`` to dump
    the raw matching lines for manual inspection.
    """
    n_auctions_received = 0
    n_solver_called = 0
    n_solutions_received = 0
    n_validation_failures = 0
    n_interaction_complaints = 0
    n_simulated_ok = 0

    rejection_messages: Counter[str] = Counter()
    per_auction: dict[str, list[str]] = defaultdict(list)

    current_auction: str | None = None

    for line in log_text.splitlines():
        if (m := _PATTERN_AUCTION_RECEIVED.search(line)):
            n_auctions_received += 1
            current_auction = m.group(1)
            per_auction[current_auction].append("received")
        if _PATTERN_SOLVER_CALLED.search(line):
            n_solver_called += 1
            if current_auction:
                per_auction[current_auction].append("solver_called")
        if (m := _PATTERN_SOLUTION_RECEIVED.search(line)):
            n = int(m.group(1))
            n_solutions_received += n
            if current_auction:
                per_auction[current_auction].append(f"solutions_received={n}")
        if _PATTERN_VALIDATION_FAIL.search(line):
            n_validation_failures += 1
            # Capture a short snippet of the error for the histogram.
            # Truncate to the first 80 chars so the counter doesn't explode
            # on incidental log noise (e.g. timestamps in the middle of the
            # line).
            normalized = re.sub(r"\d", "X", line[-200:].strip())[:80]
            rejection_messages[normalized] += 1
            if current_auction:
                per_auction[current_auction].append("validation_failed")
        if _PATTERN_INTERACTION_KIND.search(line):
            n_interaction_complaints += 1
        if _PATTERN_SIMULATED_OK.search(line):
            n_simulated_ok += 1
            if current_auction:
                per_auction[current_auction].append("simulated_ok")

    return {
        "n_auctions_received": n_auctions_received,
        "n_solver_called": n_solver_called,
        "n_solutions_received": n_solutions_received,
        "n_validation_failures": n_validation_failures,
        "n_interaction_complaints": n_interaction_complaints,
        "n_simulated_ok": n_simulated_ok,
        "rejection_messages": rejection_messages,
        "per_auction": per_auction if full else None,
    }


def render(summary: dict) -> None:
    print("=" * 60)
    print("CoW Driver Log Diagnosis")
    print("=" * 60)
    print(f"  auctions received:            {summary['n_auctions_received']:>5}")
    print(f"  solver-engine called:         {summary['n_solver_called']:>5}")
    print(f"  solutions received from us:   {summary['n_solutions_received']:>5}")
    print(f"  simulated successfully:       {summary['n_simulated_ok']:>5}")
    print(f"  validation failures:          {summary['n_validation_failures']:>5}")
    print(f"  interaction-shape complaints: {summary['n_interaction_complaints']:>5}")
    print()

    # Bucket validation outcomes.
    if summary["n_solutions_received"] > 0:
        ok_rate = (
            100 * summary["n_simulated_ok"] / summary["n_solutions_received"]
        )
        print(f"  ⇒ simulation-OK rate: {ok_rate:.1f}%")
        if ok_rate < 10:
            print(
                "    ⚠ near-zero OK rate — the driver is rejecting almost\n"
                "      everything. Check the interaction-shape complaints\n"
                "      below; likely the Solution.interactions schema gap\n"
                "      flagged in PR #44 review."
            )
    else:
        print(
            "  ⇒ no solutions received yet. Either the autopilot is not\n"
            "    dispatching (check `docker logs cow-autopilot`) or the\n"
            "    driver is not forwarding to the solver-engine."
        )

    if summary["rejection_messages"]:
        print()
        print("=== Top rejection-message patterns ===")
        for msg, n in summary["rejection_messages"].most_common(8):
            print(f"  [{n:>4}] {msg}")

    if summary["per_auction"] is not None:
        print()
        print(f"=== Per-auction trace (--full, n={len(summary['per_auction'])}) ===")
        for aid, events in list(summary["per_auction"].items())[:30]:
            print(f"  {aid}: {' → '.join(events)}")
        if len(summary["per_auction"]) > 30:
            print(f"  ... ({len(summary['per_auction']) - 30} more)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarise cow-driver log outcomes."
    )
    parser.add_argument(
        "--since",
        default="5m",
        help="docker logs --since value (e.g. 5m, 1h, 24h). Default 5m.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Include per-auction event traces in the output.",
    )
    args = parser.parse_args()

    log_text = _read_logs(args.since)
    if not log_text.strip():
        print(
            f"WARNING: no log output from cow-driver in last {args.since}.\n"
            "  Either the container is not running, or --since is too small.\n"
            "  Verify with: docker ps | grep cow-driver",
            file=sys.stderr,
        )
        sys.exit(1)
    summary = analyze(log_text, full=args.full)
    render(summary)


if __name__ == "__main__":
    main()
