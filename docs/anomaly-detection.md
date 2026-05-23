# Anomaly Detection Classifier

## Why Anomaly Detection

CoW Protocol pre-filters its token universe before tokens ever reach auction data.
Scam tokens, honeypots, and rug-pulls are screened at the protocol layer, so the
training data is organically one-sided: nearly all tokens with observable auction
history are legitimate.

The two sources that would produce scam labels in a balanced dataset are both
future work:

- **Phase 4 settlement reconciler** — will write `caused_revert=True` rows into
  `token_outcomes` when a batch reverts on-chain due to a specific token.
- **`has_transfer_tax` enrichment** — off-chain honeypot detection that sets a flag
  in `token_features`; this secondary signal is wired into label generation already
  but the enrichment job is not yet running.

Until either of these populates meaningful scam examples (`MIN_SCAM_SAMPLES = 10`),
training a binary classifier would fail silently: it has no negative class to learn
from, so it learns nothing. The anomaly-detection path trains IsolationForest on
legit-only examples and scores new tokens by how much they deviate from the
known-good feature distribution.

---

## Architecture: Three-Tier Model Selection

`_train_model()` in `edge/classifier/train.py` selects a model based on label counts:

```
n_scam < 10          →  IsolationForest   (AnomalyScorer)
10 ≤ n_scam, n < 2000 →  RandomForestClassifier  (Optuna-tuned)
n ≥ 2000             →  LGBMClassifier
```

The gate is checked at training time, so the system upgrades automatically the
next time the scheduler runs after enough scam labels accumulate. No code change
is needed.

```python
MIN_SCAM_SAMPLES = 10   # anomaly → binary flip
LGBM_THRESHOLD   = 2000 # RF → LightGBM flip
```

---

## AnomalyScorer

`AnomalyScorer` (in `edge/classifier/train.py`) wraps `sklearn.IsolationForest`
and exposes the same surface as `RandomForestClassifier` so the rest of the pipeline
requires no changes.

```python
class AnomalyScorer:
    n_classes_: int = 2            # satisfies hyperopt guard: if rf.n_classes_ < 2

    def fit(self, X: pd.DataFrame) -> AnomalyScorer: ...
    def predict_proba(self, X: pd.DataFrame) -> np.ndarray: ...  # shape (n, 2)
    def feature_importances_(self) -> None: ...                   # always None
```

**Probability normalization.** IsolationForest's `score_samples()` returns
anomaly scores in an arbitrary negative range — not probabilities. `AnomalyScorer`
calibrates them at fit time using the 1st and 99th percentiles of the training
set's score distribution:

```python
legit_prob = (raw_score - p1) / (p99 - p1)   # linearly maps [p1, p99] → [0, 1]
legit_prob = clip(legit_prob, 0.0, 1.0)
```

Column 0 of the output is `P(anomalous)`, column 1 is `P(legit)`. This mirrors
`sklearn` convention so `predict.py` can read `proba[0, 1]` without branching on
model type.

---

## Feature Pipeline

### Raw inputs (14 columns)

Sourced from `token_features`:

| Column | Type | Notes |
|---|---|---|
| `decimals` | int | ERC-20 decimals |
| `contract_verified` | bool | Sourcify / Etherscan verified |
| `has_transfer_tax` | bool | Honeypot flag |
| `bridge_canonical` | bool | Canonical bridge-wrapped token |
| `tvl_usd` | float | **100% NULL** in current data |
| `volume_24h_usd` | float | **100% NULL** in current data |
| `pool_count_v2/v3/camelot` | int | DEX pool presence |
| `holder_count` | int | **100% NULL** in current data |
| `top10_concentration` | float | Share held by top 10 addresses |
| `age_blocks` | int | Blocks since contract deploy |
| `on_arbitrum_token_list` | bool | Listed on Arbitrum token list |
| `on_coingecko` | bool | Listed on CoinGecko |

### Engineered outputs (16 columns)

`apply_feature_engineering()` in `edge/classifier/feature_engineering.py` produces:

- **Pass-through:** `decimals`, `contract_verified`, `has_transfer_tax`,
  `bridge_canonical`, `on_arbitrum_token_list`, `on_coingecko`, `top10_concentration`
- **Log-scaled:** `log_tvl_usd`, `log_volume_24h_usd`, `log_holder_count`,
  `log_age_blocks` — `log1p` handles zero safely; skewed distributions become
  more tree-friendly
- **Derived pool features:** `total_pool_count` (sum of all pool counts),
  `has_any_pool` (binary)
- **Missing-value indicators:** `tvl_missing`, `volume_missing`, `holder_missing` —
  encode "this token was never enriched" as a signal in its own right

All remaining NaNs are filled with `0` before model input. This means the
model currently operates on **8 populated features** (pool counts, age, booleans,
concentration). The three NULL-majority columns still contribute their
`_missing=1` indicators.

---

## Label Generation

`generate_labels()` in `edge/classifier/label_generator.py` scans a rolling
30-day window of `token_outcomes`:

| Condition | Label |
|---|---|
| `n_reverts >= 2` OR `has_transfer_tax=True` | `scam` |
| `n_wins >= threshold` | `legit` |
| Otherwise | `unknown` (excluded from training) |

The wins threshold adapts to dataset maturity:

```python
WINS_FOR_LEGIT_COLD_START = 2   # when total labeled tokens < 2000
WINS_FOR_LEGIT_MATURE     = 5   # when total labeled tokens >= 2000
```

`appeared_in_winner` (set by the shadow poller from CoW Protocol's
`/solver_competition` API — all tokens in an auction that had a winning solution)
is the sole legit signal today. A token must appear in at least 2 winning
solutions — across a 30-day window — before it earns the `legit` label. Tokens
with insufficient history remain `unknown` and are silently dropped from the
training frame.

---

## Training Flow

```
python -m scripts.train_classifier [--window-days 60] [--models-dir /data/models]
          │
          ▼
      train()
          │
          ├─ _build_training_frame()
          │     ├─ generate_labels(window_days)
          │     ├─ JOIN token_features ON token_address
          │     └─ attach first_seen (min observed_at per token)
          │
          ├─ _chronological_split(df, test_fraction=0.2)
          │     └─ sort by first_seen → oldest 80% train, newest 20% test
          │
          ├─ _train_model(df, n_samples)
          │     └─ selects IsolationForest / RF / LightGBM per table above
          │
          ├─ save_versioned(StoredModel, models_dir)
          │
          └─ promote_to_current(path)  ← only if AUC improves > 0.01 over current
```

The chronological split is intentional: the model must generalize to tokens it
has not seen, which in production always means tokens that arrive after training
time.

`StoredModel` bundles the fitted model object with `feature_columns`
(`ENGINEERED_FEATURE_COLUMNS` at the time of training), `trained_at`, and
`metrics`. This means the correct feature list is always co-located with the
model — no external config to drift.

---

## Inference

`TokenClassifier.score(features: dict) -> float` in `edge/classifier/predict.py`:

1. Accepts a raw feature dict keyed by `RAW_FEATURE_COLUMNS`.
2. Calls `apply_feature_engineering()` to produce the engineered frame.
3. Reads `self.model.feature_columns` from the stored model to select columns.
4. Calls `model.predict_proba(df[cols])` — works identically for `AnomalyScorer`,
   `RandomForestClassifier`, and `LGBMClassifier`.
5. Returns `proba[0, 1]` (P(legit)).

Falls back to `0.5` (neutral) on any exception or if no model is loaded.

---

## Metrics

In anomaly mode the reported metrics are:

| Metric | Value | Meaning |
|---|---|---|
| `auc` | `0.5` (fixed) | Undefined without a negative class — set explicitly to signal anomaly mode |
| `mean_legit_score` | ~`0.811` | Mean P(legit) on held-out legit tokens; primary health indicator |
| `f1`, `precision_legit`, `recall_legit` | `0.0` | Undefined in anomaly mode |

A healthy `mean_legit_score` above ~0.75 confirms the model recognises its own
training distribution. A value near 0.5 suggests the calibration collapsed (flat
score distribution) or the training set is too small.

`auc=0.5` in the registry is **expected and correct** in anomaly mode. Promotion
logic compares new AUC against current AUC; when both are `0.5` the first model
is always promoted (no current model exists) and subsequent retrains are promoted
if the score distribution genuinely changes (which would require the binary path
to activate).

---

## Upgrade Path to Binary Classification

No manual intervention is needed. The upgrade happens automatically at the next
scheduled retrain once either condition is satisfied:

1. **Phase 4 settlement reconciler** writes `caused_revert=True` rows into
   `token_outcomes`. `generate_labels()` will start producing `scam` labels.
2. **`has_transfer_tax` enrichment** is enabled and the enrichment job marks
   honeypot tokens in `token_features`.

Once `n_scam >= 10` at training time:

- `_train_model()` takes the binary path.
- With `n_total < 2000`: Optuna searches 50 RF hyperparameter trials using
  `TimeSeriesSplit(n_splits=3)` cross-validation.
- With `n_total >= 2000`: LightGBM is used directly.
- Real AUC replaces the fixed `0.5`, so the promotion gate becomes meaningful.

---

## Operational Notes

**Retrain:**
```bash
python -m scripts.train_classifier
python -m scripts.train_classifier --window-days 90 --models-dir /data/models
```

**Model storage** (`/data/models/`):
```
/data/models/
  current.pkl                          ← symlink, atomic swap on promotion
  model_v20260523T020000_123456Z.pkl   ← timestamped versioned files
  model_v20260522T020000_654321Z.pkl
  ...
```

`promote_to_current()` writes a `.tmp` symlink and uses `os.replace()` for
atomic promotion — live inference sees either the old or the new model, never
a partial write.

`prune_old(keep=8)` runs at the end of every training run and deletes versioned
files beyond the 8 most recent. `current.pkl` is never deleted by pruning.

**No model loaded:** `TokenClassifier.score()` returns `0.5` (neutral). The
solver treats tokens as neither trusted nor blocked until a model is promoted.
