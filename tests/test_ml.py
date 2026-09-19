import pytest
import pandas as pd
import numpy as np
import os
import ml.fraud_model as fraud_model
from ml.fraud_model import extract_features, train_fraud_model, predict_fraud

def test_extract_features_training():
    # Create dummy dataframe
    df = pd.DataFrame({
        'transaction_id': [1, 2, 3],
        'merchant_id': ['M1', 'M1', 'M2'],
        'amount': [10.0, 20.0, 100.0],
        'transaction_ts': pd.to_datetime(['2026-01-01 10:00:00', '2026-01-01 11:00:00', '2026-01-01 12:00:00'])
    })
    
    df_features, features, stats = extract_features(df, is_training=True)
    
    assert 'log_amount' in df_features.columns
    assert 'transaction_hour' in df_features.columns
    assert 'merchant_avg' in df_features.columns
    assert 'merchant_freq' in df_features.columns
    
    # Check stats calculation
    assert stats is not None
    m1_stat = stats[stats['merchant_id'] == 'M1'].iloc[0]
    assert m1_stat['merchant_avg'] == 15.0
    assert m1_stat['merchant_freq'] == 2

def test_extract_features_inference():
    df = pd.DataFrame({
        'transaction_id': [4],
        'merchant_id': ['M1'], # Known merchant
        'amount': [15.0],
        'transaction_ts': pd.to_datetime(['2026-01-01 10:00:00'])
    })
    
    stats = pd.DataFrame({
        'merchant_id': ['M1', 'M2'],
        'merchant_avg': [10.0, 100.0],
        'merchant_freq': [5, 10]
    })
    
    df_features, features, _ = extract_features(df, is_training=False, stats=stats)
    
    assert df_features.iloc[0]['merchant_avg'] == 10.0
    assert df_features.iloc[0]['amount_deviation'] == 5.0 # 15 - 10
    assert df_features.iloc[0]['merchant_freq'] == 5

def test_extract_features_inference_unknown_merchant():
    df = pd.DataFrame({
        'transaction_id': [5],
        'merchant_id': ['M3'], # Unknown merchant
        'amount': [50.0],
        'transaction_ts': pd.to_datetime(['2026-01-01 10:00:00'])
    })

    stats = pd.DataFrame({
        'merchant_id': ['M1', 'M2'],
        'merchant_avg': [10.0, 20.0],
        'merchant_freq': [5, 15]
    })

    df_features, features, _ = extract_features(df, is_training=False, stats=stats)

    # For an unknown merchant, avg falls back to the transaction's own amount
    # (neutral: zero deviation), and freq falls back to the MEDIAN of known
    # merchants (a typical merchant), not 1 (the rarest possible) -- a brand
    # new merchant shouldn't automatically look like the most suspicious one.
    assert df_features.iloc[0]['merchant_avg'] == 50.0
    assert df_features.iloc[0]['amount_deviation'] == 0.0 # 50 - 50
    assert df_features.iloc[0]['merchant_freq'] == 10  # median of [5, 15]


def test_extract_features_unknown_merchant_uses_explicit_typical_freq_when_given():
    df = pd.DataFrame({
        'transaction_id': [5],
        'merchant_id': ['M3'],
        'amount': [50.0],
        'transaction_ts': pd.to_datetime(['2026-01-01 10:00:00'])
    })
    stats = pd.DataFrame({'merchant_id': ['M1'], 'merchant_avg': [10.0], 'merchant_freq': [5]})

    df_features, _, _ = extract_features(df, is_training=False, stats=stats, typical_merchant_freq=42)

    assert df_features.iloc[0]['merchant_freq'] == 42


def test_transaction_hour_reflects_real_timestamps_not_all_zero():
    """Regression test for the bug where transaction_hour was derived from a
    DATE column and was always 0, silently discarding the fraud time signal."""
    df = pd.DataFrame({
        'transaction_id': [1, 2, 3, 4],
        'merchant_id': ['M1', 'M1', 'M1', 'M1'],
        'amount': [10.0, 20.0, 30.0, 40.0],
        'transaction_ts': pd.to_datetime([
            '2026-01-01 03:00:00',
            '2026-01-01 14:30:00',
            '2026-01-02 21:15:00',
            '2026-01-02 09:45:00',
        ])
    })

    df_features, _, _ = extract_features(df, is_training=True)

    assert not (df_features['transaction_hour'] == 0).all()
    assert list(df_features['transaction_hour']) == [3, 14, 21, 9]


def _synthetic_fraud_dataset(n_normal=200, n_fraud=6):
    rng = np.random.default_rng(42)
    normal = pd.DataFrame({
        'transaction_id': [f'n{i}' for i in range(n_normal)],
        'merchant_id': rng.choice(['ZOMATO', 'AMAZON', 'UBER'], n_normal),
        'amount': rng.uniform(50, 2000, n_normal),
        'transaction_ts': pd.date_range('2026-01-01', periods=n_normal, freq='h')
    })
    fraud = pd.DataFrame({
        'transaction_id': [f'f{i}' for i in range(n_fraud)],
        'merchant_id': ['UNKNOWN_MERCHANT'] * n_fraud,
        'amount': rng.uniform(50000, 90000, n_fraud),
        'transaction_ts': pd.date_range('2026-01-01 02:00:00', periods=n_fraud, freq='h')
    })
    return pd.concat([normal, fraud], ignore_index=True)


def test_train_fraud_model_persists_calibrated_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(fraud_model, 'MODEL_PATH', str(tmp_path / 'model.pkl'))
    monkeypatch.setattr(fraud_model, 'STATS_PATH', str(tmp_path / 'stats.pkl'))
    monkeypatch.setattr(fraud_model, 'MODELS_DIR', str(tmp_path))

    df = _synthetic_fraud_dataset()
    model, metadata = train_fraud_model(df, contamination=0.03)

    assert 'threshold' in metadata
    assert 'training_date' in metadata
    assert metadata['feature_columns'] == fraud_model.FEATURE_COLUMNS
    assert os.path.exists(fraud_model.MODEL_PATH)
    assert os.path.exists(fraud_model.STATS_PATH)


def test_predict_fraud_is_batch_invariant(tmp_path, monkeypatch):
    """The same transaction must get the same probability and flag whether it
    is scored alone or alongside a very different batch -- this is the
    regression test for the old per-batch min/max normalization bug."""
    monkeypatch.setattr(fraud_model, 'MODEL_PATH', str(tmp_path / 'model.pkl'))
    monkeypatch.setattr(fraud_model, 'STATS_PATH', str(tmp_path / 'stats.pkl'))
    monkeypatch.setattr(fraud_model, 'MODELS_DIR', str(tmp_path))

    df = _synthetic_fraud_dataset()
    model, metadata = train_fraud_model(df, contamination=0.03)

    target_row = df.iloc[[0]].copy()

    small_batch = target_row
    large_batch = pd.concat([target_row, df.iloc[200:206]], ignore_index=True)

    result_small = predict_fraud(small_batch, model=model, metadata=metadata)
    result_large = predict_fraud(large_batch, model=model, metadata=metadata)

    assert result_small['fraud_probability'].iloc[0] == pytest.approx(
        result_large['fraud_probability'].iloc[0]
    )
    assert result_small['is_flagged'].iloc[0] == result_large['is_flagged'].iloc[0]


def test_predict_fraud_flags_clear_outliers(tmp_path, monkeypatch):
    monkeypatch.setattr(fraud_model, 'MODEL_PATH', str(tmp_path / 'model.pkl'))
    monkeypatch.setattr(fraud_model, 'STATS_PATH', str(tmp_path / 'stats.pkl'))
    monkeypatch.setattr(fraud_model, 'MODELS_DIR', str(tmp_path))

    df = _synthetic_fraud_dataset()
    model, metadata = train_fraud_model(df, contamination=0.03)

    result = predict_fraud(df, model=model, metadata=metadata)

    fraud_rows = result.loc[df['merchant_id'] == 'UNKNOWN_MERCHANT']
    assert fraud_rows['is_flagged'].any()
    assert result['fraud_probability'].between(0, 1).all()
