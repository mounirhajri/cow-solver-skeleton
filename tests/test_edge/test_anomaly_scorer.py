"""Tests for AnomalyScorer — the IsolationForest wrapper used when scam labels
are absent (< MIN_SCAM_SAMPLES).  Verifies the sklearn-compatible interface so
the rest of the pipeline (predict.py, train.py) needs no changes."""
import numpy as np
import pandas as pd

from edge.classifier.train import MIN_SCAM_SAMPLES, AnomalyScorer


def _make_feature_df(n: int, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "f1": rng.uniform(0, 1, n),
        "f2": rng.uniform(0, 10, n),
        "f3": rng.integers(0, 5, n).astype(float),
    })


def test_fit_does_not_crash():
    scorer = AnomalyScorer(n_estimators=10, random_state=42)
    X = _make_feature_df(50)
    scorer.fit(X)  # must not raise


def test_predict_proba_shape():
    scorer = AnomalyScorer(n_estimators=10, random_state=42)
    X = _make_feature_df(50)
    scorer.fit(X)
    proba = scorer.predict_proba(_make_feature_df(20, seed=1))
    assert proba.shape == (20, 2), "must return (n, 2) like sklearn classifiers"


def test_predict_proba_values_in_unit_interval():
    scorer = AnomalyScorer(n_estimators=10, random_state=42)
    X = _make_feature_df(50)
    scorer.fit(X)
    proba = scorer.predict_proba(_make_feature_df(30, seed=2))
    assert np.all(proba >= 0.0)
    assert np.all(proba <= 1.0)


def test_rows_sum_to_one():
    scorer = AnomalyScorer(n_estimators=10, random_state=42)
    X = _make_feature_df(50)
    scorer.fit(X)
    proba = scorer.predict_proba(_make_feature_df(20, seed=3))
    np.testing.assert_allclose(proba.sum(axis=1), 1.0, atol=1e-6)


def test_training_inliers_score_higher_than_obvious_outliers():
    """Tokens identical to the training distribution should score higher than
    tokens with extreme out-of-range values."""
    scorer = AnomalyScorer(n_estimators=50, random_state=42, contamination=0.05)
    X_train = _make_feature_df(100, seed=0)  # uniform [0,1], [0,10], [0,4]
    scorer.fit(X_train)

    inliers = _make_feature_df(20, seed=99)  # same distribution
    # Outliers: features far outside training range
    outliers = pd.DataFrame({
        "f1": np.full(20, 100.0),
        "f2": np.full(20, -50.0),
        "f3": np.full(20, 999.0),
    })

    inlier_scores = scorer.predict_proba(inliers)[:, 1]
    outlier_scores = scorer.predict_proba(outliers)[:, 1]

    assert inlier_scores.mean() > outlier_scores.mean(), (
        "inliers should get higher legit-probability than extreme outliers"
    )


def test_n_classes_is_2():
    """predict.py checks n_classes_ >= 2; must satisfy that guard."""
    scorer = AnomalyScorer(n_estimators=5, random_state=0)
    assert scorer.n_classes_ == 2


def test_feature_importances_is_none():
    """feature_importances_ must exist but return None (IsolationForest has none)."""
    scorer = AnomalyScorer(n_estimators=5, random_state=0)
    scorer.fit(_make_feature_df(20))
    assert scorer.feature_importances_ is None


def test_min_scam_samples_constant_is_positive():
    assert MIN_SCAM_SAMPLES > 0


def test_anomaly_path_taken_when_no_scam_labels(monkeypatch):
    """_train_model must return model_type='isolation_forest' when df has no scam rows."""
    from edge.classifier.feature_engineering import RAW_FEATURE_COLUMNS
    from edge.classifier.train import _train_model

    rng = np.random.default_rng(42)
    n = 30
    df = pd.DataFrame({col: rng.uniform(0, 1, n) for col in RAW_FEATURE_COLUMNS})
    df["y"] = 1  # all legit — no scam labels
    df["first_seen"] = pd.date_range("2025-01-01", periods=n, freq="1h")

    _, metrics, model_type = _train_model(df, n_samples=n)

    assert model_type == "isolation_forest"
    assert metrics["n_scam"] == 0.0
    assert "mean_legit_score" in metrics


def test_contamination_default_is_low():
    """Default contamination must trust the training corpus is almost entirely
    legit.  contamination='auto' (sklearn ~0.10) baked a 10 % anomaly fraction
    into the decision boundary, which under-classified ~80 % of inference
    orders as anomalies — verified live 2026-05-24."""
    from edge.classifier.train import DEFAULT_ANOMALY_CONTAMINATION

    assert 0.0 < DEFAULT_ANOMALY_CONTAMINATION <= 0.05, (
        f"DEFAULT_ANOMALY_CONTAMINATION={DEFAULT_ANOMALY_CONTAMINATION} is too "
        f"aggressive; values >0.05 produce overly tight filters on cold-start "
        f"data sets where confirmed scams are rare."
    )


def test_contamination_override_is_propagated():
    """A caller passing contamination=0.2 must actually train with that value —
    not silently fall back to the default.  Guards against a future refactor
    that drops the kwarg from the AnomalyScorer constructor."""
    from edge.classifier.feature_engineering import RAW_FEATURE_COLUMNS
    from edge.classifier.train import _train_model

    rng = np.random.default_rng(123)
    n = 50
    df = pd.DataFrame({col: rng.uniform(0, 1, n) for col in RAW_FEATURE_COLUMNS})
    df["y"] = 1
    df["first_seen"] = pd.date_range("2025-01-01", periods=n, freq="1h")

    model, _, model_type = _train_model(df, n_samples=n, contamination=0.2)
    assert model_type == "isolation_forest"
    assert model._clf.contamination == 0.2


def test_binary_path_taken_when_enough_scam_labels():
    """_train_model must return a binary model when scam labels >= MIN_SCAM_SAMPLES."""
    from edge.classifier.feature_engineering import RAW_FEATURE_COLUMNS
    from edge.classifier.train import MIN_SCAM_SAMPLES, _train_model

    rng = np.random.default_rng(7)
    n_legit = 40
    n_scam = MIN_SCAM_SAMPLES  # exactly at threshold
    n = n_legit + n_scam

    df = pd.DataFrame({col: rng.uniform(0, 1, n) for col in RAW_FEATURE_COLUMNS})
    df["y"] = [1] * n_legit + [0] * n_scam
    df["first_seen"] = pd.date_range("2025-01-01", periods=n, freq="1h")

    _, _, model_type = _train_model(df, n_samples=n)

    assert model_type == "random_forest", (
        "with enough scam labels the binary RF path should be taken"
    )
