"""File-based liveness probe for worker containers.

Worker scripts call ``touch_liveness()`` at the end of each cycle. The
docker healthcheck calls ``is_alive()`` to verify the worker is making
progress.
"""

from __future__ import annotations

import time
from pathlib import Path


def touch_liveness(path: Path) -> None:
    """Update the liveness file's mtime to now."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def is_alive(path: Path, max_age_seconds: int) -> bool:
    """Return True if the liveness file was touched within max_age_seconds."""
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < max_age_seconds
