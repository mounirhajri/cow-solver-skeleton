import time
from datetime import UTC, datetime

from edge.classifier.model_registry import (
    StoredModel,
    load_current,
    promote_to_current,
    prune_old,
    save_versioned,
)


def test_save_versioned_creates_file(tmp_path):
    model = StoredModel(
        model={"dummy": True}, feature_columns=["x"],
        trained_at=datetime.now(UTC), metrics={"auc": 0.7},
    )
    path = save_versioned(model, models_dir=tmp_path)
    assert path.exists()
    assert path.name.startswith("model_v")
    assert path.suffix == ".pkl"


def test_promote_creates_symlink(tmp_path):
    model = StoredModel(model={"dummy": True}, feature_columns=[],
                        trained_at=datetime.now(UTC), metrics={})
    path = save_versioned(model, models_dir=tmp_path)
    promote_to_current(path)
    current = tmp_path / "current.pkl"
    assert current.is_symlink() or current.exists()


def test_load_current_after_promote(tmp_path):
    model = StoredModel(model={"dummy": "value"}, feature_columns=["a"],
                        trained_at=datetime.now(UTC), metrics={"auc": 0.5})
    path = save_versioned(model, models_dir=tmp_path)
    promote_to_current(path)
    loaded = load_current(models_dir=tmp_path)
    assert loaded is not None
    assert loaded.feature_columns == ["a"]


def test_load_current_none_when_missing(tmp_path):
    assert load_current(models_dir=tmp_path) is None


def test_prune_old_keeps_recent(tmp_path):
    for i in range(5):
        model = StoredModel(model={"i": i}, feature_columns=[],
                            trained_at=datetime.now(UTC), metrics={})
        save_versioned(model, models_dir=tmp_path)
        time.sleep(0.01)  # ensure distinct mtimes
    deleted = prune_old(models_dir=tmp_path, keep=2)
    assert deleted == 3
    remaining = list(tmp_path.glob("model_v*.pkl"))
    assert len(remaining) == 2
