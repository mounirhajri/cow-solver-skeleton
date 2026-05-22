from datetime import UTC, datetime

import pandas as pd
from sklearn.ensemble import RandomForestClassifier

from edge.classifier.model_registry import StoredModel, promote_to_current, save_versioned
from edge.classifier.predict import NEUTRAL_SCORE, TokenClassifier


def test_neutral_score_when_no_model(tmp_path):
    clf = TokenClassifier.load(models_dir=tmp_path)
    score = clf.score({"decimals": 18})
    assert score == NEUTRAL_SCORE


def test_score_with_loaded_model(tmp_path):
    # Train a tiny RF on synthetic data so we have a real model
    X = pd.DataFrame({"decimals": [18, 18, 6, 6, 18, 6], "pool_count_v2": [3, 4, 0, 1, 2, 0]})
    y = [1, 1, 0, 0, 1, 0]
    rf = RandomForestClassifier(n_estimators=5, random_state=42).fit(X, y)
    stored = StoredModel(model=rf, feature_columns=list(X.columns),
                         trained_at=datetime.now(UTC), metrics={"auc": 0.8})
    p = save_versioned(stored, models_dir=tmp_path)
    promote_to_current(p)

    clf = TokenClassifier.load(models_dir=tmp_path)
    score = clf.score({"decimals": 18, "pool_count_v2": 3})
    assert 0.0 <= score <= 1.0


def test_reload_picks_up_new_model(tmp_path):
    clf = TokenClassifier.load(models_dir=tmp_path)
    # Initially no model
    assert clf.model is None
    # Drop a model in
    rf = RandomForestClassifier(n_estimators=3).fit(
        pd.DataFrame({"x": [0, 1]}), [0, 1])
    stored = StoredModel(model=rf, feature_columns=["x"],
                         trained_at=datetime.now(UTC), metrics={})
    p = save_versioned(stored, models_dir=tmp_path)
    promote_to_current(p)
    clf.reload()
    assert clf.model is not None
