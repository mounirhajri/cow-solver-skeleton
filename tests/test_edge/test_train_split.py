"""Test that the chronological train/test split is order-preserving."""
import pandas as pd
import pytest

from edge.classifier.train import _chronological_split


def _make_df(n: int) -> pd.DataFrame:
    """Make a DataFrame with a monotonically increasing 'first_seen' column."""
    return pd.DataFrame({
        "first_seen": pd.date_range("2025-01-01", periods=n, freq="1h"),
        "y": [i % 2 for i in range(n)],
        "f1": range(n),
    })


def test_split_ratio_is_roughly_80_20():
    df = _make_df(100)
    train, test = _chronological_split(df)
    assert len(train) == 80
    assert len(test) == 20


def test_train_rows_come_before_test_rows():
    df = _make_df(100)
    train, test = _chronological_split(df)
    assert train["first_seen"].max() <= test["first_seen"].min()


def test_no_rows_lost():
    df = _make_df(100)
    train, test = _chronological_split(df)
    assert len(train) + len(test) == 100


def test_split_with_odd_size():
    df = _make_df(101)
    train, test = _chronological_split(df)
    assert len(train) + len(test) == 101
    assert len(train) >= 80


def test_small_dataset_does_not_crash():
    df = _make_df(5)
    train, test = _chronological_split(df)
    assert len(train) + len(test) == 5
