import os
import time
from pathlib import Path

import pytest

from scripts.liveness import is_alive, touch_liveness


def test_touch_liveness_creates_file(tmp_path):
    f = tmp_path / "alive"
    touch_liveness(f)
    assert f.exists()


def test_touch_liveness_creates_parent_dir(tmp_path):
    f = tmp_path / "subdir" / "alive"
    touch_liveness(f)
    assert f.exists()


def test_is_alive_within_window(tmp_path):
    f = tmp_path / "alive"
    touch_liveness(f)
    assert is_alive(f, max_age_seconds=120)


def test_is_alive_returns_false_when_file_missing(tmp_path):
    f = tmp_path / "alive"
    assert not is_alive(f, max_age_seconds=120)


def test_is_alive_stale(tmp_path):
    f = tmp_path / "alive"
    f.write_text("")
    old = time.time() - 600
    os.utime(f, (old, old))
    assert not is_alive(f, max_age_seconds=120)
