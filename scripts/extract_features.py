"""One-off / cron entry point for token feature extraction.

Usage (in container):
    python -m scripts.extract_features --batch-size 100
"""

from __future__ import annotations

import argparse
import asyncio
import json

from edge.classifier import run_extraction
from src.config import settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    result = asyncio.run(run_extraction(settings.rpc_arbitrum, batch_size=args.batch_size))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
