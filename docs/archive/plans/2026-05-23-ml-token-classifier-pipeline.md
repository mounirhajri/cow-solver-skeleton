# ML Token Classifier Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve the token-quality RF/LightGBM classifier with feature engineering, dynamic cold-start label thresholds, hyperparameter tuning via Optuna, and a chronological train/test split — so the CoWJohnsonSolver pre-filter is more accurate from day one.

**Architecture:** A new `feature_engineering.py` module transforms raw FEATURE_COLUMNS into log-scaled + derived columns before training and inference. `hyperopt.py` finds best RF params using Optuna + TimeSeriesSplit before the first real model lands. `train.py` gets a chronological 80/20 split (sorted by earliest `TokenOutcome.observed_at` per token) replacing the current random split.

**Tech Stack:** scikit-learn, optuna (new dep), pandas, numpy, pytest-asyncio, sqlalchemy (async)

---

## File Map

| File | Action |
|------|--------|
| `scripts/analyze_classifier_data.py` | Create — show label distribution + data readiness |
| `edge/classifier/feature_engineering.py` | Create — log transforms, derived features, missing indicators |
| `edge/classifier/label_generator.py` | Modify — dynamic WINS_FOR_LEGIT based on n_existing_labels |
| `edge/classifier/hyperopt.py` | Create — Optuna RF hyperparameter search |
| `edge/classifier/train.py` | Modify — chronological split + use feature engineering + hyperopt + richer metrics |
| `tests/test_edge/test_feature_engineering.py` | Create — unit tests for all transforms |
| `tests/test_edge/test_hyperopt.py` | Create — unit tests for hyperopt with tiny dataset |
| `tests/test_edge/test_label_generator_threshold.py` | Create — test dynamic threshold logic |
| `pyproject.toml` | Modify — add optuna>=3.6 |

---

## Task 1: Data Analysis Script

**Goal:** Understand current data before improving anything. Run this first to know if training is realistic.

**Files:**
- Create: `scripts/analyze_classifier_data.py`

- [ ] **Step 1: Create the script**

```python
# scripts/analyze_classifier_data.py
"""Analyze token classifier data readiness.

Usage:
    python -m scripts.analyze_classifier_data
"""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from edge.classifier.label_generator import (
    DEFAULT_WINDOW_DAYS,
    WINS_FOR_LEGIT,
    class_distribution,
    generate_labels,
)
from src.persistence.db import get_session_factory
from src.persistence.models import TokenFeatures, TokenOutcome


async def analyze() -> None:
    Session = get_session_factory()

    async with Session() as session:
        n_outcomes = (await session.execute(select(func.count()).select_from(TokenOutcome))).scalar()
        n_features = (await session.execute(select(func.count()).select_from(TokenFeatures))).scalar()
        n_with_tvl = (
            await session.execute(
                select(func.count()).where(TokenFeatures.tvl_usd.is_not(None))
            )
        ).scalar()
        n_with_holder = (
            await session.execute(
                select(func.count()).where(TokenFeatures.holder_count.is_not(None))
            )
        ).scalar()
        distinct_tokens = (
            await session.execute(
                select(func.count(TokenOutcome.token_address.distinct()))
            )
        ).scalar()

    print("=== Token Classifier Data Readiness ===")
    print(f"token_outcomes rows:           {n_outcomes}")
    print(f"distinct token addresses:      {distinct_tokens}")
    print(f"token_features rows:           {n_features}")
    print(f"  with tvl_usd:                {n_with_tvl}")
    print(f"  with holder_count:           {n_with_holder}")
    print(f"  missing rate tvl:            {1 - n_with_tvl / max(n_features, 1):.1%}")
    print(f"  missing rate holder:         {1 - n_with_holder / max(n_features, 1):.1%}")
    print()

    labeled = await generate_labels(window_days=DEFAULT_WINDOW_DAYS)
    dist = class_distribution(labeled)
    print(f"Labels (window={DEFAULT_WINDOW_DAYS}d, WINS_FOR_LEGIT={WINS_FOR_LEGIT}):")
    print(f"  legit:   {dist['legit']}")
    print(f"  scam:    {dist['scam']}")
    print(f"  unknown: {dist['unknown']}")
    trainable = dist["legit"] + dist["scam"]
    print(f"  => trainable samples: {trainable}")

    if trainable < 20:
        print("\n⚠️  CANNOT TRAIN — need at least 20 labeled samples")
    elif trainable < 100:
        print("\n⚠️  COLD START — RF with 2-win threshold recommended")
    else:
        print("\n✅  Ready to train")


def main() -> None:
    asyncio.run(analyze())


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it**

```bash
python -m scripts.analyze_classifier_data
```

Expected: prints counts and label distribution. Note the missing-rate for tvl/holder — this drives which features need missing-value indicators.

- [ ] **Step 3: Commit**

```bash
git add scripts/analyze_classifier_data.py
git commit -m "feat: add classifier data readiness script"
```

---

## Task 2: Feature Engineering Module

**Goal:** Transform raw FEATURE_COLUMNS before training and inference. Log-scale skewed continuous features, create derived features, add missing-value indicator flags.

**Files:**
- Create: `edge/classifier/feature_engineering.py`
- Create: `tests/test_edge/test_feature_engineering.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_edge/test_feature_engineering.py
"""Tests for feature engineering transforms."""
import math

import pandas as pd
import pytest

from edge.classifier.feature_engineering import (
    ENGINEERED_FEATURE_COLUMNS,
    apply_feature_engineering,
)


def _row(**kwargs) -> pd.DataFrame:
    defaults = {
        "decimals": 18,
        "contract_verified": True,
        "has_transfer_tax": False,
        "bridge_canonical": False,
        "tvl_usd": None,
        "volume_24h_usd": None,
        "pool_count_v2": 0,
        "pool_count_v3": None,
        "pool_count_camelot": 0,
        "holder_count": None,
        "top10_concentration": None,
        "age_blocks": None,
        "on_arbitrum_token_list": False,
        "on_coingecko": False,
    }
    defaults.update(kwargs)
    return pd.DataFrame([defaults])


def test_log_transforms_positive_values():
    df = _row(tvl_usd=1000.0, volume_24h_usd=500.0, holder_count=200, age_blocks=5000)
    result = apply_feature_engineering(df)
    assert result["log_tvl_usd"].iloc[0] == pytest.approx(math.log1p(1000.0))
    assert result["log_volume_24h_usd"].iloc[0] == pytest.approx(math.log1p(500.0))
    assert result["log_holder_count"].iloc[0] == pytest.approx(math.log1p(200))
    assert result["log_age_blocks"].iloc[0] == pytest.approx(math.log1p(5000))


def test_log_transforms_zero_treated_as_log1p():
    df = _row(tvl_usd=0.0, volume_24h_usd=0.0)
    result = apply_feature_engineering(df)
    assert result["log_tvl_usd"].iloc[0] == pytest.approx(math.log1p(0.0))  # = 0.0


def test_missing_indicator_flags_none_as_one():
    df = _row(tvl_usd=None, volume_24h_usd=None, holder_count=None)
    result = apply_feature_engineering(df)
    assert result["tvl_missing"].iloc[0] == 1
    assert result["volume_missing"].iloc[0] == 1
    assert result["holder_missing"].iloc[0] == 1


def test_missing_indicator_flags_present_value_as_zero():
    df = _row(tvl_usd=100.0, volume_24h_usd=50.0, holder_count=10)
    result = apply_feature_engineering(df)
    assert result["tvl_missing"].iloc[0] == 0
    assert result["volume_missing"].iloc[0] == 0
    assert result["holder_missing"].iloc[0] == 0


def test_total_pool_count_sums_all_three():
    df = _row(pool_count_v2=3, pool_count_v3=2, pool_count_camelot=1)
    result = apply_feature_engineering(df)
    assert result["total_pool_count"].iloc[0] == 6


def test_total_pool_count_none_treated_as_zero():
    df = _row(pool_count_v2=2, pool_count_v3=None, pool_count_camelot=0)
    result = apply_feature_engineering(df)
    assert result["total_pool_count"].iloc[0] == 2


def test_has_any_pool_true_when_total_positive():
    df = _row(pool_count_v2=1, pool_count_v3=None, pool_count_camelot=0)
    result = apply_feature_engineering(df)
    assert result["has_any_pool"].iloc[0] == 1


def test_has_any_pool_false_when_all_zero():
    df = _row(pool_count_v2=0, pool_count_v3=0, pool_count_camelot=0)
    result = apply_feature_engineering(df)
    assert result["has_any_pool"].iloc[0] == 0


def test_nan_filled_to_zero_in_output():
    df = _row(top10_concentration=None, age_blocks=None)
    result = apply_feature_engineering(df)
    assert not result[ENGINEERED_FEATURE_COLUMNS].isnull().any().any()


def test_output_columns_match_ENGINEERED_FEATURE_COLUMNS():
    df = _row()
    result = apply_feature_engineering(df)
    for col in ENGINEERED_FEATURE_COLUMNS:
        assert col in result.columns, f"Missing column: {col}"


def test_batch_of_rows_works():
    rows = [_row(tvl_usd=float(i * 100)) for i in range(10)]
    df = pd.concat(rows, ignore_index=True)
    result = apply_feature_engineering(df)
    assert len(result) == 10
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_edge/test_feature_engineering.py -v
```

Expected: `ImportError: cannot import name 'apply_feature_engineering'`

- [ ] **Step 3: Implement feature_engineering.py**

```python
# edge/classifier/feature_engineering.py
"""Feature engineering transforms for the token-quality classifier.

Applies log-scaling, derived features, and missing-value indicators
to a raw feature DataFrame. Used in both training and inference paths.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Raw columns consumed from token_features (same as FEATURE_COLUMNS in train.py)
RAW_FEATURE_COLUMNS = [
    "decimals",
    "contract_verified",
    "has_transfer_tax",
    "bridge_canonical",
    "tvl_usd",
    "volume_24h_usd",
    "pool_count_v2",
    "pool_count_v3",
    "pool_count_camelot",
    "holder_count",
    "top10_concentration",
    "age_blocks",
    "on_arbitrum_token_list",
    "on_coingecko",
]

# Output columns after engineering — these replace RAW_FEATURE_COLUMNS as model input
ENGINEERED_FEATURE_COLUMNS = [
    # Raw boolean/categorical (unchanged)
    "decimals",
    "contract_verified",
    "has_transfer_tax",
    "bridge_canonical",
    "on_arbitrum_token_list",
    "on_coingecko",
    "top10_concentration",
    # Log-scaled continuous (skewed distributions → more RF-friendly)
    "log_tvl_usd",
    "log_volume_24h_usd",
    "log_holder_count",
    "log_age_blocks",
    # Derived pool features
    "total_pool_count",
    "has_any_pool",
    # Missing-value indicators (signal "we never enriched this token")
    "tvl_missing",
    "volume_missing",
    "holder_missing",
]


def apply_feature_engineering(df: pd.DataFrame) -> pd.DataFrame:
    """Return a new DataFrame with ENGINEERED_FEATURE_COLUMNS.

    Input: DataFrame with RAW_FEATURE_COLUMNS (or subset — missing columns
    are treated as NaN).  Does NOT modify the input in-place.

    All NaN values in the output are filled with 0 so sklearn never sees
    missing values.
    """
    out = pd.DataFrame(index=df.index)

    # ── Pass-through columns ────────────────────────────────────────────────
    for col in ["decimals", "contract_verified", "has_transfer_tax",
                "bridge_canonical", "on_arbitrum_token_list", "on_coingecko",
                "top10_concentration"]:
        out[col] = df.get(col, pd.Series(np.nan, index=df.index))

    # ── Missing-value indicators (computed before filling NaN) ──────────────
    out["tvl_missing"] = df.get("tvl_usd", pd.Series(np.nan, index=df.index)).isna().astype(int)
    out["volume_missing"] = df.get("volume_24h_usd", pd.Series(np.nan, index=df.index)).isna().astype(int)
    out["holder_missing"] = df.get("holder_count", pd.Series(np.nan, index=df.index)).isna().astype(int)

    # ── Log-scaled continuous features (log1p handles zero safely) ──────────
    out["log_tvl_usd"] = np.log1p(df.get("tvl_usd", pd.Series(0.0, index=df.index)).fillna(0.0))
    out["log_volume_24h_usd"] = np.log1p(df.get("volume_24h_usd", pd.Series(0.0, index=df.index)).fillna(0.0))
    out["log_holder_count"] = np.log1p(df.get("holder_count", pd.Series(0.0, index=df.index)).fillna(0.0))
    out["log_age_blocks"] = np.log1p(df.get("age_blocks", pd.Series(0.0, index=df.index)).fillna(0.0))

    # ── Derived pool features ───────────────────────────────────────────────
    pv2 = df.get("pool_count_v2", pd.Series(0, index=df.index)).fillna(0)
    pv3 = df.get("pool_count_v3", pd.Series(0, index=df.index)).fillna(0)
    pc = df.get("pool_count_camelot", pd.Series(0, index=df.index)).fillna(0)
    out["total_pool_count"] = pv2 + pv3 + pc
    out["has_any_pool"] = (out["total_pool_count"] > 0).astype(int)

    # ── Final NaN fill (catch any stragglers from boolean/categorical cols) ──
    out[ENGINEERED_FEATURE_COLUMNS] = out[ENGINEERED_FEATURE_COLUMNS].fillna(0)

    return out
```

- [ ] **Step 4: Run tests — expect them to pass**

```bash
pytest tests/test_edge/test_feature_engineering.py -v
```

Expected: all 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add edge/classifier/feature_engineering.py tests/test_edge/test_feature_engineering.py
git commit -m "feat: add feature engineering module for token classifier (log transforms, derived features, missing indicators)"
```

---

## Task 3: Dynamic Cold-Start Label Threshold

**Goal:** With only ~291 auctions, WINS_FOR_LEGIT=5 is too strict — very few tokens ever reach 5 wins. Drop to 2 during cold start (<2000 labeled samples) so we get more training data.

**Files:**
- Modify: `edge/classifier/label_generator.py`
- Create: `tests/test_edge/test_label_generator_threshold.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_edge/test_label_generator_threshold.py
"""Test dynamic WINS_FOR_LEGIT threshold in label_generator."""
import pytest

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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_edge/test_label_generator_threshold.py -v
```

Expected: `ImportError: cannot import name 'WINS_FOR_LEGIT_COLD_START'`

- [ ] **Step 3: Modify label_generator.py**

Replace the top constants block and add `_wins_threshold()`:

```python
# Replace in edge/classifier/label_generator.py

# OLD (remove these lines):
# WINS_FOR_LEGIT = 5
# REVERTS_FOR_SCAM = 2
# DEFAULT_WINDOW_DAYS = 30

# NEW (add these lines in their place):
WINS_FOR_LEGIT_COLD_START = 2    # used when < LABEL_MATURITY_THRESHOLD labeled samples
WINS_FOR_LEGIT_MATURE = 5        # used when >= LABEL_MATURITY_THRESHOLD labeled samples
LABEL_MATURITY_THRESHOLD = 2000  # mirrors LGBM_THRESHOLD in train.py
WINS_FOR_LEGIT = WINS_FOR_LEGIT_MATURE  # kept for backward-compat references
REVERTS_FOR_SCAM = 2
DEFAULT_WINDOW_DAYS = 30


def _wins_threshold(n_existing_labels: int = 0) -> int:
    """Return the wins-for-legit threshold appropriate for the current data size."""
    if n_existing_labels >= LABEL_MATURITY_THRESHOLD:
        return WINS_FOR_LEGIT_MATURE
    return WINS_FOR_LEGIT_COLD_START
```

Then modify `generate_labels()` to accept and use the parameter:

```python
# In generate_labels(), change signature and label logic:

async def generate_labels(
    window_days: int = DEFAULT_WINDOW_DAYS,
    n_existing_labels: int = 0,
) -> list[LabeledToken]:
    """Compute labels for all tokens with sufficient observation history.
    
    n_existing_labels: pass the current training set size to select the
    appropriate legit-win threshold (cold start vs mature).
    """
    since = datetime.now(UTC) - timedelta(days=window_days)
    wins_needed = _wins_threshold(n_existing_labels)
    Session = get_session_factory()

    # ... (rest of function unchanged until the labeling logic) ...

    # Change label assignment line from:
    #   elif n_wins >= WINS_FOR_LEGIT:
    # to:
            elif n_wins >= wins_needed:
```

Full modified `generate_labels` body (replace in file):

```python
async def generate_labels(
    window_days: int = DEFAULT_WINDOW_DAYS,
    n_existing_labels: int = 0,
) -> list[LabeledToken]:
    """Compute labels for all tokens with sufficient observation history."""
    since = datetime.now(UTC) - timedelta(days=window_days)
    wins_needed = _wins_threshold(n_existing_labels)
    Session = get_session_factory()

    async with Session() as session:
        outcome_q = await session.execute(
            select(
                TokenOutcome.token_address,
                TokenOutcome.appeared_in_winner,
                TokenOutcome.caused_revert,
            ).where(TokenOutcome.observed_at >= since)
        )
        rows = outcome_q.all()
        if not rows:
            return []

        agg: dict[str, dict[str, int]] = {}
        for addr, appeared_in_winner, caused_revert in rows:
            if addr not in agg:
                agg[addr] = {"n_wins": 0, "n_reverts": 0}
            if appeared_in_winner:
                agg[addr]["n_wins"] += 1
            if caused_revert:
                agg[addr]["n_reverts"] += 1

        feat_q = await session.execute(
            select(TokenFeatures.token_address, TokenFeatures.has_transfer_tax)
        )
        honeypot_flags = {row[0]: bool(row[1]) for row in feat_q.all()}

        labeled: list[LabeledToken] = []
        for addr, counts in agg.items():
            n_wins = counts["n_wins"]
            n_reverts = counts["n_reverts"]
            is_honeypot = honeypot_flags.get(addr, False)

            label: Label
            if n_reverts >= REVERTS_FOR_SCAM or is_honeypot:
                label = "scam"
            elif n_wins >= wins_needed:
                label = "legit"
            else:
                label = "unknown"
            labeled.append(LabeledToken(
                address=addr,
                label=label,
                n_winner_appearances=n_wins,
                n_reverts=n_reverts,
            ))

        return labeled
```

- [ ] **Step 4: Run tests — expect them to pass**

```bash
pytest tests/test_edge/test_label_generator_threshold.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 5: Verify existing tests still pass**

```bash
pytest tests/test_edge/test_label_generator.py -v
```

Expected: all existing tests still PASS (WINS_FOR_LEGIT alias preserved).

- [ ] **Step 6: Commit**

```bash
git add edge/classifier/label_generator.py tests/test_edge/test_label_generator_threshold.py
git commit -m "feat: dynamic cold-start label threshold (2 wins cold, 5 mature) for token classifier"
```

---

## Task 4: Hyperparameter Optimization with Optuna

**Goal:** Find better RF hyperparameters using Optuna + TimeSeriesSplit CV instead of fixed `n_estimators=300, max_depth=8`.

**Files:**
- Modify: `pyproject.toml` (add optuna)
- Create: `edge/classifier/hyperopt.py`
- Create: `tests/test_edge/test_hyperopt.py`

- [ ] **Step 1: Add optuna to pyproject.toml**

Add to the `dependencies` list (main deps, not dev — training runs on the server):

```toml
# In pyproject.toml, inside dependencies = [...], add after lightgbm:
"optuna>=3.6",
```

Also add to `[tool.mypy]` ignore section:

```toml
[[tool.mypy.overrides]]
module = ["optuna", "optuna.*"]
ignore_missing_imports = true
```

- [ ] **Step 2: Install the dependency**

```bash
uv sync
```

Expected: optuna installed with no conflicts.

- [ ] **Step 3: Write the failing tests**

```python
# tests/test_edge/test_hyperopt.py
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
```

- [ ] **Step 4: Run tests to verify they fail**

```bash
pytest tests/test_edge/test_hyperopt.py -v
```

Expected: `ImportError: cannot import name 'find_best_rf_params'`

- [ ] **Step 5: Implement hyperopt.py**

```python
# edge/classifier/hyperopt.py
"""Optuna-based hyperparameter search for the RandomForest classifier.

Uses TimeSeriesSplit cross-validation (order matters — no shuffle) so
the search respects temporal ordering of auction data.

Usage:
    from edge.classifier.hyperopt import find_best_rf_params
    best_params = find_best_rf_params(X_train, y_train, n_trials=50)
    rf = RandomForestClassifier(**best_params, class_weight="balanced", random_state=42)
"""
from __future__ import annotations

import logging

import optuna  # type: ignore[import-untyped]
import pandas as pd
from sklearn.ensemble import RandomForestClassifier  # type: ignore[import-untyped]
from sklearn.metrics import roc_auc_score  # type: ignore[import-untyped]
from sklearn.model_selection import TimeSeriesSplit  # type: ignore[import-untyped]

# Silence optuna's per-trial INFO noise — we emit our own summary
optuna.logging.set_verbosity(logging.WARNING)


def find_best_rf_params(
    X: pd.DataFrame,
    y: pd.Series,
    n_trials: int = 50,
    cv_splits: int = 3,
) -> dict:
    """Search for best RandomForest hyperparameters using Optuna + TimeSeriesSplit.

    Args:
        X: Feature DataFrame (already engineered, no NaN)
        y: Binary labels (0/1)
        n_trials: Number of Optuna trials
        cv_splits: Number of TimeSeriesSplit folds

    Returns:
        dict of best params — pass directly to RandomForestClassifier(**params)
    """
    tscv = TimeSeriesSplit(n_splits=cv_splits)

    def objective(trial: optuna.Trial) -> float:
        params = {
            "n_estimators": trial.suggest_int("n_estimators", 50, 500),
            "max_depth": trial.suggest_int("max_depth", 3, 25),
            "min_samples_split": trial.suggest_int("min_samples_split", 2, 20),
            "min_samples_leaf": trial.suggest_int("min_samples_leaf", 1, 10),
            "max_features": trial.suggest_categorical("max_features", ["sqrt", "log2", None]),
            "class_weight": "balanced",
            "random_state": 42,
            "n_jobs": -1,
        }
        fold_aucs: list[float] = []
        for train_idx, val_idx in tscv.split(X):
            X_tr, X_val = X.iloc[train_idx], X.iloc[val_idx]
            y_tr, y_val = y.iloc[train_idx], y.iloc[val_idx]

            # Skip fold if validation set has only one class
            if len(y_val.unique()) < 2:
                continue

            rf = RandomForestClassifier(**params)
            rf.fit(X_tr, y_tr)
            proba = rf.predict_proba(X_val)[:, 1]
            try:
                fold_aucs.append(roc_auc_score(y_val, proba))
            except ValueError:
                continue  # degenerate fold — skip

        return float(sum(fold_aucs) / len(fold_aucs)) if fold_aucs else 0.0

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    best = study.best_params
    # Remove internal params not passed to sklearn
    best.pop("class_weight", None)
    best.pop("random_state", None)
    return best
```

- [ ] **Step 6: Run tests — expect them to pass**

```bash
pytest tests/test_edge/test_hyperopt.py -v
```

Expected: all 4 tests PASS (may take ~15s for the 5-trial test).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml edge/classifier/hyperopt.py tests/test_edge/test_hyperopt.py
git commit -m "feat: Optuna RF hyperparameter optimization with TimeSeriesSplit CV"
```

---

## Task 5: Chronological Train/Test Split in train.py

**Goal:** Replace the random 80/20 split with a chronological split sorted by the earliest `TokenOutcome.observed_at` per token. Prevents data leakage: we train on older observations, test on newer ones — mirrors how the model will be used in production.

**Files:**
- Modify: `edge/classifier/train.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_edge/test_train_split.py`:

```python
# tests/test_edge/test_train_split.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_edge/test_train_split.py -v
```

Expected: `ImportError: cannot import name '_chronological_split'`

- [ ] **Step 3: Modify train.py — add first_seen to build_training_frame + add _chronological_split**

Add to imports at top of `train.py`:

```python
from sqlalchemy import func  # add to existing sqlalchemy import
from src.persistence.models import TokenFeatures, TokenOutcome  # add TokenOutcome
```

Add `_chronological_split` function after `FEATURE_COLUMNS`:

```python
def _chronological_split(
    df: pd.DataFrame, test_fraction: float = 0.2
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split df into train/test preserving temporal order.

    Rows are sorted by 'first_seen' column (ascending). The first
    (1 - test_fraction) rows form the train set; the remainder form the test set.
    """
    df_sorted = df.sort_values("first_seen", ascending=True).reset_index(drop=True)
    n = len(df_sorted)
    split_idx = max(1, int(n * (1 - test_fraction)))
    return df_sorted.iloc[:split_idx], df_sorted.iloc[split_idx:]
```

Modify `_build_training_frame` to also fetch `min(observed_at)` per token and include it as `first_seen` column:

```python
async def _build_training_frame(window_days: int) -> pd.DataFrame:
    # Get current label count to select appropriate threshold
    Session = get_session_factory()
    async with Session() as session:
        existing_count = (
            await session.execute(select(func.count()).select_from(TokenFeatures))
        ).scalar() or 0

    labeled = await generate_labels(window_days=window_days, n_existing_labels=existing_count)
    if not labeled:
        return pd.DataFrame()
    rows = [{"token_address": t.address, "label": t.label} for t in labeled if t.label != "unknown"]
    if not rows:
        return pd.DataFrame()

    async with Session() as session:
        feat_q = await session.execute(select(TokenFeatures))
        feature_rows = [
            {
                "token_address": tf.token_address,
                "decimals": tf.decimals,
                "contract_verified": tf.contract_verified,
                "has_transfer_tax": tf.has_transfer_tax,
                "bridge_canonical": tf.bridge_canonical,
                "tvl_usd": float(tf.tvl_usd) if tf.tvl_usd is not None else None,
                "volume_24h_usd": (
                    float(tf.volume_24h_usd) if tf.volume_24h_usd is not None else None
                ),
                "pool_count_v2": tf.pool_count_v2,
                "pool_count_v3": tf.pool_count_v3,
                "pool_count_camelot": tf.pool_count_camelot,
                "holder_count": tf.holder_count,
                "top10_concentration": (
                    float(tf.top10_concentration) if tf.top10_concentration is not None else None
                ),
                "age_blocks": tf.age_blocks,
                "on_arbitrum_token_list": tf.on_arbitrum_token_list,
                "on_coingecko": tf.on_coingecko,
            }
            for tf in feat_q.scalars().all()
        ]

        # Fetch earliest observed_at per token for chronological split
        first_seen_q = await session.execute(
            select(
                TokenOutcome.token_address,
                func.min(TokenOutcome.observed_at).label("first_seen"),
            ).group_by(TokenOutcome.token_address)
        )
        first_seen_map = {row[0]: row[1] for row in first_seen_q.all()}

    labels_df = pd.DataFrame(rows)
    features_df = pd.DataFrame(feature_rows)
    if features_df.empty:
        return pd.DataFrame()
    df = labels_df.merge(features_df, on="token_address", how="inner")
    df["y"] = (df["label"] == "legit").astype(int)
    df["first_seen"] = df["token_address"].map(first_seen_map)
    # Tokens with no outcome row get current time (safe fallback — treated as "newest")
    df["first_seen"] = df["first_seen"].fillna(pd.Timestamp.utcnow())
    return df
```

- [ ] **Step 4: Update `_train_model` to use `_chronological_split` instead of `train_test_split`**

Replace the split logic inside `_train_model`:

```python
def _train_model(df: pd.DataFrame, n_samples: int) -> tuple[Any, dict[str, float], str]:
    from edge.classifier.feature_engineering import (
        ENGINEERED_FEATURE_COLUMNS,
        apply_feature_engineering,
    )

    X_raw = df[FEATURE_COLUMNS]
    y = df["y"]

    # Chronological split — train on older data, test on newer
    df_with_time = df.assign(first_seen=df["first_seen"])
    train_df, test_df = _chronological_split(df_with_time)

    X_train_raw = train_df[FEATURE_COLUMNS]
    X_test_raw = test_df[FEATURE_COLUMNS]
    y_train = train_df["y"]
    y_test = test_df["y"]

    # Apply feature engineering
    X_train = apply_feature_engineering(X_train_raw)
    X_test = apply_feature_engineering(X_test_raw)
    X_cols = ENGINEERED_FEATURE_COLUMNS

    if n_samples >= LGBM_THRESHOLD:
        try:
            from lightgbm import LGBMClassifier  # type: ignore[import-untyped]
            model: Any = LGBMClassifier(
                objective="binary",
                num_leaves=31,
                max_depth=6,
                min_data_in_leaf=20,
                learning_rate=0.05,
                n_estimators=500,
                class_weight="balanced",
                feature_fraction=0.9,
                bagging_fraction=0.8,
                bagging_freq=5,
                verbose=-1,
            )
            model_type = "lightgbm"
        except ImportError:
            model = RandomForestClassifier(
                n_estimators=300, max_depth=8, class_weight="balanced", random_state=42,
            )
            model_type = "random_forest"
    else:
        # Cold start: use Optuna to find best RF params
        from edge.classifier.hyperopt import find_best_rf_params
        best_params = find_best_rf_params(
            X_train[X_cols], y_train, n_trials=50, cv_splits=3
        )
        log.info("hyperopt_complete", best_params=best_params)
        model = RandomForestClassifier(
            **best_params, class_weight="balanced", random_state=42,
        )
        model_type = "random_forest"

    model.fit(X_train[X_cols], y_train)

    y_proba = model.predict_proba(X_test[X_cols])
    proba_pos = y_proba[:, 1] if y_proba.shape[1] >= 2 else y_proba[:, 0]
    y_pred = (proba_pos >= 0.5).astype(int)

    try:
        auc = float(roc_auc_score(y_test, proba_pos))
    except ValueError:
        auc = 0.0

    from sklearn.metrics import classification_report  # type: ignore[import-untyped]
    report = classification_report(y_test, y_pred, output_dict=True, zero_division=0)
    f1 = float(report.get("weighted avg", {}).get("f1-score", 0.0))
    precision_legit = float(report.get("1", {}).get("precision", 0.0))
    recall_legit = float(report.get("1", {}).get("recall", 0.0))

    # Feature importance (RF only; LGBM has its own attribute)
    if hasattr(model, "feature_importances_"):
        importances = sorted(
            zip(X_cols, model.feature_importances_),
            key=lambda x: x[1], reverse=True,
        )
        log.info("feature_importance", top5=importances[:5])

    metrics = {
        "auc": auc,
        "f1": f1,
        "precision_legit": precision_legit,
        "recall_legit": recall_legit,
        "n_train": float(len(X_train)),
        "n_test": float(len(X_test)),
    }

    # StoredModel needs to know to use ENGINEERED_FEATURE_COLUMNS at inference
    model._feature_columns = X_cols  # store on model for predict.py compatibility
    return model, metrics, model_type
```

Also update the `StoredModel` construction in `train()` to use ENGINEERED_FEATURE_COLUMNS:

```python
# In train(), replace:
#   stored = StoredModel(model=model, feature_columns=FEATURE_COLUMNS, ...)
# with:
    from edge.classifier.feature_engineering import ENGINEERED_FEATURE_COLUMNS
    stored = StoredModel(
        model=model,
        feature_columns=ENGINEERED_FEATURE_COLUMNS,
        trained_at=datetime.now(UTC),
        metrics=metrics,
    )
```

- [ ] **Step 5: Update predict.py to apply feature engineering before scoring**

The `TokenClassifier.score()` method currently receives raw feature dicts. It needs to apply feature engineering before calling `predict_proba`:

```python
# In edge/classifier/predict.py, replace score() method:

    def score(self, features: dict) -> float:
        """Return P(legit) in [0, 1] for a single token.

        `features` is a dict matching RAW_FEATURE_COLUMNS schema. Feature
        engineering is applied internally. Missing keys → NaN → filled by
        feature_engineering.
        """
        if self.model is None:
            return NEUTRAL_SCORE
        try:
            from edge.classifier.feature_engineering import (
                RAW_FEATURE_COLUMNS,
                apply_feature_engineering,
            )
            import pandas as pd
            row = {col: features.get(col) for col in RAW_FEATURE_COLUMNS}
            df_raw = pd.DataFrame([row])
            df_eng = apply_feature_engineering(df_raw)
            # Use the model's stored feature columns (handles old/new model compat)
            cols = self.model.feature_columns
            proba = self.model.model.predict_proba(df_eng[cols])
        except Exception as e:  # noqa: BLE001
            log.warning("score_failed", error=str(e))
            return NEUTRAL_SCORE
        if proba.shape[1] >= 2:
            return float(proba[0, 1])
        return float(proba[0, 0])
```

- [ ] **Step 6: Run all tests — expect them to pass**

```bash
pytest tests/test_edge/ -v
```

Expected: All tests PASS including new split tests and existing predictor/registry tests.

- [ ] **Step 7: Commit**

```bash
git add edge/classifier/train.py edge/classifier/predict.py tests/test_edge/test_train_split.py
git commit -m "feat: chronological train/test split + feature engineering + Optuna hyperopt in classifier pipeline"
```

---

## Task 6: Smoke-Test the Full Pipeline

**Goal:** Verify the whole pipeline runs end-to-end with real DB data.

- [ ] **Step 1: Run data analysis to confirm readiness**

```bash
python -m scripts.analyze_classifier_data
```

Expected: shows label counts. Note n_trainable.

- [ ] **Step 2: Run feature extraction (populate token_features)**

```bash
python -m edge.classifier.feature_extractor
```

Expected: `feature_extraction_done n_addresses=N n_upserted=N` in logs.

- [ ] **Step 3: Run training**

```bash
python -m edge.classifier.train
```

Expected output (approximate):

```
n_samples=NNN model_type=random_forest promoted=True auc=0.XXX f1=0.XXX
```

If `n_samples < 20`: "not_enough_samples" warning — run feature extraction first.

- [ ] **Step 4: Verify model was written to disk**

```bash
ls -la /data/models/
```

Expected: `current.pkl -> model_vXXXXXXXXT...pkl` symlink present.

- [ ] **Step 5: Quick inference smoke test**

```python
python -c "
from edge.classifier.predict import TokenClassifier
clf = TokenClassifier.load()
score = clf.score({
    'decimals': 18, 'contract_verified': True, 'tvl_usd': 500000.0,
    'volume_24h_usd': 100000.0, 'pool_count_v2': 3, 'pool_count_v3': 1,
    'pool_count_camelot': 1, 'holder_count': 1500, 'on_coingecko': True,
})
print(f'Score for well-known token: {score:.3f} (expect >0.5)')
"
```

Expected: score > 0.5 for a token with good features.

- [ ] **Step 6: Commit (if any minor fixes needed)**

```bash
git add -p
git commit -m "fix: pipeline smoke test adjustments"
```

---

## Self-Review Checklist

**Spec coverage:**
- ✅ Feature engineering (log transforms, derived features, missing indicators) → Task 2
- ✅ Cold-start label threshold (2 wins vs 5 wins) → Task 3
- ✅ Hyperparameter optimization with Optuna → Task 4
- ✅ Time-based CV split → Task 5
- ✅ Feature importance output → Task 5 (inside `_train_model`)
- ✅ Per-class precision/recall → Task 5 (classification_report)
- ✅ Feature engineering applied at inference → Task 5 (predict.py)
- ✅ Optuna added to deps → Task 4

**Placeholder scan:** No TBD, no TODOs, no "similar to task N" — all code blocks are complete.

**Type consistency:**
- `apply_feature_engineering(df: pd.DataFrame) -> pd.DataFrame` — used consistently in train.py and predict.py
- `find_best_rf_params(X, y, n_trials, cv_splits) -> dict` — consistent across hyperopt.py and train.py
- `_chronological_split(df, test_fraction) -> tuple[DataFrame, DataFrame]` — consistent across test and train.py
- `ENGINEERED_FEATURE_COLUMNS` imported from `feature_engineering` everywhere it's used

**Execution order dependency check:**
- Task 2 (feature_engineering.py) must be done before Task 5 (train.py uses it)
- Task 3 (label threshold) must be done before Task 5 (train.py calls `generate_labels(n_existing_labels=...)`)
- Task 4 (hyperopt.py) must be done before Task 5 (train.py imports it)
- Task 5 depends on Tasks 2, 3, 4 all being complete
- Task 6 depends on all others

Tasks 2, 3, 4 are independent and can be done in any order.
