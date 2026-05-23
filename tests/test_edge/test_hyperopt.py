"""Tests for Optuna-based RF hyperparameter optimization."""
import numpy as np
import pytest
from sklearn.datasets import make_classification

from edge.classifier.hyperopt import find_best_rf_params


@pytest.fixture
def small_dataset():
    """50 samples, 5 features, binary classification — small enough for fast tests."""
    X, y = make_classification(
        n_samples=50, n_features=5, n_informative=3,
        n_redundant=1, random_state=42, class_sep=1.5,
    )
    import pandas as pd
    X_df = pd.DataFrame(X, columns=[f"f{i}" for i in range(5)])
    return X_df, pd.Series(y)


def test_returns_dict_with_required_keys(small_dataset):
    X, y = small_dataset
    params = find_best_rf_params(X, y, n_trials=5, cv_splits=2)
    required_keys = {
        "n_estimators", "max_depth", "min_samples_split",
        "min_samples_leaf", "max_features",
    }
    assert required_keys.issubset(set(params.keys()))


def test_param_values_are_in_valid_range(small_dataset):
    X, y = small_dataset
    params = find_best_rf_params(X, y, n_trials=5, cv_splits=2)
    assert 50 <= params["n_estimators"] <= 500
    assert 3 <= params["max_depth"] <= 25
    assert 2 <= params["min_samples_split"] <= 20
    assert 1 <= params["min_samples_leaf"] <= 10
    assert params["max_features"] in ("sqrt", "log2", None)


def test_runs_with_single_trial(small_dataset):
    X, y = small_dataset
    params = find_best_rf_params(X, y, n_trials=1, cv_splits=2)
    assert isinstance(params, dict)


def test_imbalanced_dataset_does_not_crash():
    """Imbalanced class ratio — TimeSeriesSplit folds may have only one class."""
    import pandas as pd
    rng = np.random.default_rng(0)
    X = pd.DataFrame(rng.standard_normal((60, 4)), columns=list("abcd"))
    # 5:1 imbalance
    y = pd.Series([0] * 50 + [1] * 10)
    params = find_best_rf_params(X, y, n_trials=3, cv_splits=2)
    assert isinstance(params, dict)
