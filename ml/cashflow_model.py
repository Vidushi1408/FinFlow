import pandas as pd
import numpy as np
from sklearn.linear_model import LinearRegression
import joblib
import os
from datetime import datetime, timezone

from logging_config import logger

MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'models')
MODEL_PATH = os.path.join(MODELS_DIR, 'cashflow_model.pkl')
STATS_PATH = os.path.join(MODELS_DIR, 'cashflow_stats.pkl')

FEATURE_COLUMNS = ['rolling_7d', 'rolling_14d', 'day_of_week', 'is_payday', 'is_weekend']

# Days of the month that commonly carry paydays/rent (see data_generator's
# PERSONAS income_days) -- a spending model benefits from knowing "today is
# likely payday" since spending patterns shift around it.
PAYDAY_DAYS = {1, 28, 29, 30, 31}


def _build_daily_features(df):
    """
    Daily total spend + engineered features, WITHOUT a target column or any
    row dropped -- used both to build the training frame (below) and to seed
    live forecasting with the most recent day's features.
    """
    df_daily = df.groupby('date_id')['amount'].sum().reset_index()
    df_daily['date'] = pd.to_datetime(df_daily['date_id'].astype(str), format='%Y%m%d')
    df_daily = df_daily.sort_values('date').set_index('date')

    full_date_range = pd.date_range(start=df_daily.index.min(), end=df_daily.index.max())
    df_daily = df_daily.reindex(full_date_range, fill_value=0)

    df_daily['rolling_7d'] = df_daily['amount'].rolling(window=7, min_periods=1).mean()
    df_daily['rolling_14d'] = df_daily['amount'].rolling(window=14, min_periods=1).mean()
    df_daily['day_of_week'] = df_daily.index.dayofweek
    df_daily['is_payday'] = df_daily.index.day.isin(PAYDAY_DAYS).astype(int)
    df_daily['is_weekend'] = (df_daily.index.dayofweek >= 5).astype(int)

    return df_daily


def prepare_cashflow_data(df):
    """Training frame: daily features plus next-day target, NaN target rows dropped."""
    df_daily = _build_daily_features(df)
    df_daily['target_next_day'] = df_daily['amount'].shift(-1)
    df_daily = df_daily.dropna()
    return df_daily, FEATURE_COLUMNS


def _baseline_same_period_last_month(df_daily, target_dates):
    """Predict each target day as whatever was actually spent 30 days earlier."""
    lookup = df_daily['amount']
    preds = []
    for d in target_dates:
        ref_date = d - pd.Timedelta(days=30)
        preds.append(lookup.get(ref_date, np.nan))
    return np.array(preds)


def _baseline_trailing_30d_avg(df_daily, feature_dates):
    """Predict each target day as the trailing-30-day average spend as of the feature day."""
    trailing_avg = df_daily['amount'].rolling(window=30, min_periods=1).mean()
    return trailing_avg.reindex(feature_dates).to_numpy()


def backtest_cashflow(df_daily, feature_cols, horizon=90):
    """
    Rolling-origin-style backtest: fit once on data strictly before the test
    window, then evaluate one-step-ahead predictions across the test window
    (each prediction uses only features observable on its own feature day,
    which is standard for next-day forecasting and introduces no leakage).
    Compares the model against two naive baselines and reports MAE for each.
    """
    horizon = max(min(horizon, len(df_daily) - 14), 7)  # need some minimum history to train on
    train_df = df_daily.iloc[:-horizon]
    test_df = df_daily.iloc[-horizon:]

    if len(train_df) < 7:
        raise ValueError("Not enough history to backtest the cash flow model.")

    model = LinearRegression()
    model.fit(train_df[feature_cols], train_df['target_next_day'])

    model_pred = model.predict(test_df[feature_cols])
    actual = test_df['target_next_day'].to_numpy()

    target_dates = test_df.index + pd.Timedelta(days=1)
    baseline_last_month_pred = _baseline_same_period_last_month(df_daily, target_dates)
    baseline_trailing_pred = _baseline_trailing_30d_avg(df_daily, test_df.index)

    def mae(pred):
        pred = np.asarray(pred, dtype=float)
        valid = ~np.isnan(pred)
        if valid.sum() == 0:
            return np.inf
        return float(np.mean(np.abs(pred[valid] - actual[valid])))

    metrics = {
        'model_mae': mae(model_pred),
        'baseline_last_month_mae': mae(baseline_last_month_pred),
        'baseline_trailing_avg_mae': mae(baseline_trailing_pred),
        'backtest_days': int(horizon)
    }

    strategy = min(
        ['model', 'same_period_last_month', 'trailing_30d_avg'],
        key=lambda s: {
            'model': metrics['model_mae'],
            'same_period_last_month': metrics['baseline_last_month_mae'],
            'trailing_30d_avg': metrics['baseline_trailing_avg_mae']
        }[s]
    )

    # Residuals of the SELECTED strategy, used to size the prediction interval.
    selected_pred = {
        'model': model_pred,
        'same_period_last_month': baseline_last_month_pred,
        'trailing_30d_avg': baseline_trailing_pred
    }[strategy]
    residuals = actual - np.nan_to_num(selected_pred, nan=np.nanmean(actual))
    residual_std = float(np.std(residuals)) if len(residuals) > 1 else float(np.std(actual)) or 1.0

    return model, strategy, metrics, residual_std


def train_cashflow_model(df):
    """
    Train the cash flow model AND decide, via backtest, whether it actually
    beats naive baselines -- same period last month, and a trailing 30-day
    average. Whichever wins the backtest is what live forecasts use; this is
    recorded honestly in the saved metadata rather than always trusting the
    regression.
    """
    logger.info("Training cashflow prediction model")
    df_daily, feature_cols = prepare_cashflow_data(df)

    _, strategy, metrics, residual_std = backtest_cashflow(df_daily, feature_cols)

    # Refit the regression on the FULL history for production use, regardless of
    # which strategy won the backtest -- so it's available if the deployment
    # later wants it, and so "Model Info" can show real, current metrics for it.
    model = LinearRegression()
    model.fit(df_daily[feature_cols], df_daily['target_next_day'])

    metadata = {
        'feature_columns': feature_cols,
        'selected_strategy': strategy,
        'metrics': metrics,
        'residual_std': residual_std,
        'training_date': datetime.now(timezone.utc).isoformat(),
        'training_rows': int(len(df_daily))
    }

    os.makedirs(MODELS_DIR, exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    joblib.dump(metadata, STATS_PATH)

    logger.info(f"Model saved to {MODEL_PATH}")
    logger.info(
        f"Backtest MAE (last {metrics['backtest_days']} days) -- "
        f"model: {metrics['model_mae']:.2f}, "
        f"same period last month: {metrics['baseline_last_month_mae']:.2f}, "
        f"trailing 30d avg: {metrics['baseline_trailing_avg_mae']:.2f}. "
        f"Using: {strategy}"
        + ("" if strategy == 'model' else " (the regression did not beat this baseline)")
    )
    return model, metadata


def load_cashflow_metadata():
    if not os.path.exists(STATS_PATH):
        raise FileNotFoundError("Model stats not found. Train the model first.")
    return joblib.load(STATS_PATH)


def predict_cashflow(df, days=30, model=None, metadata=None):
    """
    Predict daily spending for the next N days using whichever strategy won
    the training-time backtest (the regression, or a naive baseline if that
    scored better) and attach a widening prediction interval derived from
    backtest residuals.
    """
    if model is None or metadata is None:
        if not os.path.exists(MODEL_PATH) or not os.path.exists(STATS_PATH):
            raise FileNotFoundError("Model not found. Train the model first.")
        model = joblib.load(MODEL_PATH)
        metadata = joblib.load(STATS_PATH)

    feature_cols = metadata.get('feature_columns', FEATURE_COLUMNS)
    strategy = metadata.get('selected_strategy', 'model')

    df_daily = _build_daily_features(df)
    current_date = df_daily.index[-1]
    history = df_daily['amount'].tolist()

    # Size the interval from THIS user's own recent day-to-day volatility, not
    # the training-time backtest residual: that residual was computed on
    # aggregate daily totals across every account in the training data, which
    # is far noisier than any single user's own spending. Using it directly
    # here produced prediction bands so wide (roughly the model's own
    # magnitude) that the forecast chart was unreadable. Falls back to the
    # global residual only when there's too little of this user's own
    # history to estimate their volatility from.
    recent_amounts = df_daily['amount'].tail(60)
    if len(recent_amounts) >= 7:
        interval_std = float(recent_amounts.std(ddof=0))
    else:
        interval_std = metadata.get('residual_std', 0.0)

    predictions = []

    if strategy == 'trailing_30d_avg':
        base_value = max(np.mean(history[-30:]), 0.0)
        for i in range(days):
            current_date += pd.Timedelta(days=1)
            predictions.append((current_date, base_value))

    elif strategy == 'same_period_last_month':
        tail_30 = history[-30:] if len(history) >= 30 else history
        for i in range(days):
            current_date += pd.Timedelta(days=1)
            pred_amount = max(tail_30[i % len(tail_30)], 0.0)
            predictions.append((current_date, pred_amount))

    else:  # 'model'
        for i in range(days):
            current_date += pd.Timedelta(days=1)
            roll_7 = np.mean(history[-7:])
            roll_14 = np.mean(history[-14:])
            X_pred = pd.DataFrame([{
                'rolling_7d': roll_7,
                'rolling_14d': roll_14,
                'day_of_week': current_date.dayofweek,
                'is_payday': int(current_date.day in PAYDAY_DAYS),
                'is_weekend': int(current_date.dayofweek >= 5)
            }])[feature_cols]
            pred_amount = max(0.0, model.predict(X_pred)[0])
            predictions.append((current_date, pred_amount))
            history.append(pred_amount)

    results = []
    for i, (date, pred_amount) in enumerate(predictions):
        # Uncertainty widens with horizon (sqrt-of-time is a standard, if
        # approximate, way to grow a naive interval over a multi-day forecast).
        interval = interval_std * 1.28 * np.sqrt(i + 1)  # ~80% band
        results.append({
            'date': date.strftime('%Y-%m-%d'),
            'predicted_amount': pred_amount,
            'lower_bound': max(0.0, pred_amount - interval),
            'upper_bound': pred_amount + interval
        })

    return results
