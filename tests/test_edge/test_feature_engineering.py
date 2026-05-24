"""Tests for feature engineering transforms."""
import math
import sys
from pathlib import Path

import pandas as pd
import pytest

# Import directly to avoid loading edge.__init__ which has Python 3.8+ compatibility issues
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
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


# ── GoPlus security feature engineering ───────────────────────────────────────


def test_goplus_bool_features_pass_through_as_zero_one():
    """is_proxy=True → 1, is_mintable=False → 0, others missing → 0."""
    df = _row(is_proxy=True, is_mintable=False)
    out = apply_feature_engineering(df)
    assert out["is_proxy"].iloc[0] == 1
    assert out["is_mintable"].iloc[0] == 0
    # Other bools default to 0 when missing.
    assert out["hidden_owner"].iloc[0] == 0


def test_goplus_taxes_clipped_to_unit_interval():
    """A 1.5 buy_tax (GoPlus parser glitch) clips to 1.0, negative to 0."""
    df = _row(buy_tax=1.5, sell_tax=-0.2)
    out = apply_feature_engineering(df)
    assert out["buy_tax"].iloc[0] == 1.0
    assert out["sell_tax"].iloc[0] == 0.0


def test_goplus_enriched_flag_off_when_no_data():
    df = _row()  # no GoPlus fields set
    out = apply_feature_engineering(df)
    assert out["goplus_enriched"].iloc[0] == 0


def test_goplus_enriched_flag_on_when_any_field_present():
    df = _row(is_proxy=True)
    out = apply_feature_engineering(df)
    assert out["goplus_enriched"].iloc[0] == 1


def test_engineered_column_count_locked():
    """Tripwire: 16 legacy columns + 13 GoPlus security + 1 enriched flag = 30
    + 1 (decimals + log + missing-indicator extras = 16) so total stays at 30.
    Update this number when intentionally adding columns."""
    assert len(ENGINEERED_FEATURE_COLUMNS) == 30, (
        f"ENGINEERED_FEATURE_COLUMNS has {len(ENGINEERED_FEATURE_COLUMNS)} "
        f"columns; update test if intentional."
    )
