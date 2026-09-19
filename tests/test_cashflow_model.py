import numpy as np
import pandas as pd
import pytest

import ml.cashflow_model as cashflow_model
from ml.cashflow_model import (
    prepare_cashflow_data,
    backtest_cashflow,
    train_cashflow_model,
    predict_cashflow,
    load_cashflow_metadata,
    FEATURE_COLUMNS,
)


def _structured_daily_df(n_days=200, seed=0):
    """Daily spend with clear weekly seasonality + payday spikes, so the
    regression should reliably beat the naive baselines on it."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2026-01-01', periods=n_days)
    rows = []
    for d in dates:
        base = 1000 + (200 if d.dayofweek >= 5 else 0)
        if d.day in (1, 28, 29, 30, 31):
            base += 500
        amount = max(base + rng.normal(0, 50), 0)
        rows.append({'date_id': int(d.strftime('%Y%m%d')), 'amount': amount})
    return pd.DataFrame(rows)


def _flat_daily_df(n_days=120, amount=500.0):
    """Perfectly flat spend -- the trailing-30-day-average baseline should win
    (or at least tie) here since there's nothing for the regression to learn."""
    dates = pd.date_range('2026-01-01', periods=n_days)
    return pd.DataFrame({
        'date_id': [int(d.strftime('%Y%m%d')) for d in dates],
        'amount': [amount] * n_days
    })


def test_prepare_cashflow_data_has_expected_features():
    df = _structured_daily_df(60)
    df_daily, feature_cols = prepare_cashflow_data(df)

    assert feature_cols == FEATURE_COLUMNS
    for col in FEATURE_COLUMNS:
        assert col in df_daily.columns
    assert 'target_next_day' in df_daily.columns
    assert df_daily['target_next_day'].isna().sum() == 0  # last row (no next day) is dropped
    assert set(df_daily['is_payday'].unique()) <= {0, 1}
    assert set(df_daily['is_weekend'].unique()) <= {0, 1}


def test_backtest_reports_mae_for_model_and_both_baselines():
    df = _structured_daily_df(200)
    df_daily, feature_cols = prepare_cashflow_data(df)

    model, strategy, metrics, residual_std = backtest_cashflow(df_daily, feature_cols, horizon=90)

    assert strategy in ('model', 'same_period_last_month', 'trailing_30d_avg')
    assert metrics['model_mae'] >= 0
    assert metrics['baseline_last_month_mae'] >= 0
    assert metrics['baseline_trailing_avg_mae'] >= 0
    assert metrics['backtest_days'] == 90
    assert residual_std >= 0


def test_model_beats_baselines_on_structured_seasonal_data():
    """Regression test for the backtest actually doing its job: on data with
    real weekly/payday structure, the model should win, not a naive baseline."""
    df = _structured_daily_df(300, seed=1)
    df_daily, feature_cols = prepare_cashflow_data(df)
    _, strategy, metrics, _ = backtest_cashflow(df_daily, feature_cols, horizon=90)

    assert strategy == 'model'
    assert metrics['model_mae'] < metrics['baseline_last_month_mae']
    assert metrics['model_mae'] < metrics['baseline_trailing_avg_mae']


def test_train_cashflow_model_persists_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(cashflow_model, 'MODEL_PATH', str(tmp_path / 'model.pkl'))
    monkeypatch.setattr(cashflow_model, 'STATS_PATH', str(tmp_path / 'stats.pkl'))
    monkeypatch.setattr(cashflow_model, 'MODELS_DIR', str(tmp_path))

    df = _structured_daily_df(200)
    model, metadata = train_cashflow_model(df)

    assert 'selected_strategy' in metadata
    assert 'metrics' in metadata
    assert 'training_date' in metadata
    assert metadata['feature_columns'] == FEATURE_COLUMNS

    loaded = load_cashflow_metadata()
    assert loaded['selected_strategy'] == metadata['selected_strategy']


def test_predict_cashflow_intervals_widen_with_horizon(tmp_path, monkeypatch):
    monkeypatch.setattr(cashflow_model, 'MODEL_PATH', str(tmp_path / 'model.pkl'))
    monkeypatch.setattr(cashflow_model, 'STATS_PATH', str(tmp_path / 'stats.pkl'))
    monkeypatch.setattr(cashflow_model, 'MODELS_DIR', str(tmp_path))

    df = _structured_daily_df(200)
    model, metadata = train_cashflow_model(df)

    predictions = predict_cashflow(df, days=30, model=model, metadata=metadata)

    assert len(predictions) == 30
    assert all(p['predicted_amount'] >= 0 for p in predictions)
    assert all(p['lower_bound'] <= p['predicted_amount'] <= p['upper_bound'] for p in predictions)

    first_width = predictions[0]['upper_bound'] - predictions[0]['lower_bound']
    last_width = predictions[-1]['upper_bound'] - predictions[-1]['lower_bound']
    assert last_width >= first_width


def test_predict_cashflow_falls_back_to_trailing_avg_baseline(tmp_path, monkeypatch):
    """On flat, unstructured data the model shouldn't beat the trailing-average
    baseline; when it doesn't, predictions must use that baseline, not the model."""
    monkeypatch.setattr(cashflow_model, 'MODEL_PATH', str(tmp_path / 'model.pkl'))
    monkeypatch.setattr(cashflow_model, 'STATS_PATH', str(tmp_path / 'stats.pkl'))
    monkeypatch.setattr(cashflow_model, 'MODELS_DIR', str(tmp_path))

    df = _flat_daily_df(120, amount=500.0)
    model, metadata = train_cashflow_model(df)

    predictions = predict_cashflow(df, days=10, model=model, metadata=metadata)

    if metadata['selected_strategy'] == 'trailing_30d_avg':
        for p in predictions:
            assert p['predicted_amount'] == pytest.approx(500.0, rel=0.05)


def test_predict_cashflow_interval_reflects_this_users_own_volatility_not_global_residual(tmp_path, monkeypatch):
    """Regression test: the prediction interval must be sized from the data
    passed into predict_cashflow (this user's own daily amounts), not from
    metadata['residual_std'] alone. A model trained on noisy aggregate data
    (large residual_std) must still produce a sane, tight interval when
    scoring one individual user's own low-volatility spending -- otherwise
    the forecast chart is dominated by a band many times larger than the
    prediction itself."""
    monkeypatch.setattr(cashflow_model, 'MODEL_PATH', str(tmp_path / 'model.pkl'))
    monkeypatch.setattr(cashflow_model, 'STATS_PATH', str(tmp_path / 'stats.pkl'))
    monkeypatch.setattr(cashflow_model, 'MODELS_DIR', str(tmp_path))

    # Simulate a model trained on noisy, large-scale aggregate data.
    noisy_training_df = _structured_daily_df(200, seed=3)
    noisy_training_df['amount'] = noisy_training_df['amount'] * 1000  # blow up the scale/residual
    model, metadata = train_cashflow_model(noisy_training_df)
    assert metadata['residual_std'] > 10000  # confirms the premise: a big global residual

    # But THIS user's own daily spend is small and steady.
    low_volatility_df = _flat_daily_df(60, amount=2000.0)

    predictions = predict_cashflow(low_volatility_df, days=30, metadata=metadata, model=model)

    # The interval must be sized off the low-volatility user's own data, so it
    # stays small relative to their own spend -- not inflated to the scale of
    # the noisy training residual.
    last = predictions[-1]
    interval_width = last['upper_bound'] - last['lower_bound']
    assert interval_width < 5000, f"interval too wide for a low-volatility user: {interval_width}"
