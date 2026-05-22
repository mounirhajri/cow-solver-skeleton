"""Shadow analytics CLI.

Usage:
    python -m src.shadow.cli analyze --days 7
"""

from __future__ import annotations

import argparse
import asyncio

from src.shadow.analyzer import AnalysisWindow, analyze, format_summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shadow.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="Aggregate stats over recent window")
    a.add_argument("--days", type=int, default=7, help="Lookback window in days")
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.cmd == "analyze":
        window = AnalysisWindow.days(args.days)
        summary = await analyze(window)
        print(format_summary(summary))
        return 0
    return 1


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    code = asyncio.run(_run(args))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
