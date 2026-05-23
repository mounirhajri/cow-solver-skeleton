"""Test dynamic WINS_FOR_LEGIT threshold in label_generator."""
from edge.classifier.label_generator import (
    WINS_FOR_LEGIT_COLD_START,
    WINS_FOR_LEGIT_MATURE,
    _wins_threshold,
)


def test_cold_start_threshold_below_2000():
    assert _wins_threshold(n_existing_labels=0) == WINS_FOR_LEGIT_COLD_START
    assert _wins_threshold(n_existing_labels=100) == WINS_FOR_LEGIT_COLD_START
    assert _wins_threshold(n_existing_labels=1999) == WINS_FOR_LEGIT_COLD_START


def test_mature_threshold_at_and_above_2000():
    assert _wins_threshold(n_existing_labels=2000) == WINS_FOR_LEGIT_MATURE
    assert _wins_threshold(n_existing_labels=5000) == WINS_FOR_LEGIT_MATURE


def test_default_is_cold_start_when_no_arg():
    # Called without arg → defaults to cold start (safe for first-run)
    assert _wins_threshold() == WINS_FOR_LEGIT_COLD_START
