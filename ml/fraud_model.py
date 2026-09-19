import pandas as pd
import numpy as np
from sklearn.ensemble import IsolationForest
import joblib
import os
from datetime import datetime, timezone

from logging_config import logger

MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'models')
MODEL_PATH = os.path.join(MODELS_DIR, 'fraud_model.pkl')
STATS_PATH = os.path.join(MODELS_DIR, 'fraud_stats.pkl')

FEATURE_COLUMNS = ['log_amount', 'transaction_hour', 'amount_deviation', 'merchant_freq']

def extract_features(df, is_training=False, stats=None, typical_merchant_freq=None):
    """
    Extract features for fraud detection.

    `typical_merchant_freq` is the fallback for a merchant never seen in
    training (a brand-new merchant on a freshly onboarded account, most
    commonly). It defaults to the median merchant_freq across all training
    merchants -- NOT 1. Filling with 1 would make "no history yet" look
    identical to "the single rarest merchant in the whole training set",
    which made ordinary first-time purchases at unfamiliar (but harmless)
    merchants read as maximally anomalous on that feature alone.
    """
    df_features = df.copy()

    # Feature 1: log(amount)
    df_features['log_amount'] = np.log1p(df_features['amount'])

    # Feature 2: transaction_hour
    # Use transaction_ts as it contains the actual time, unlike date_id or date
    ts_col = 'transaction_ts' if 'transaction_ts' in df_features.columns else 'date'
    df_features[ts_col] = pd.to_datetime(df_features[ts_col])
    df_features['transaction_hour'] = df_features[ts_col].dt.hour

    if is_training:
        merchant_avgs = df_features.groupby('merchant_id')['amount'].mean().rename('merchant_avg')
        merchant_freqs = df_features.groupby('merchant_id')['transaction_id'].count().rename('merchant_freq')

        # Save these for later
        stats = pd.concat([merchant_avgs, merchant_freqs], axis=1).reset_index()
        df_features = df_features.merge(stats, on='merchant_id', how='left')
    else:
        # Inference time: use provided stats
        if stats is not None:
            fallback_freq = typical_merchant_freq if typical_merchant_freq is not None else max(stats['merchant_freq'].median(), 1)
            df_features = df_features.merge(stats, on='merchant_id', how='left')
            # Fill missing merchants with global averages or a typical (not minimal) frequency
            df_features['merchant_avg'] = df_features['merchant_avg'].fillna(df_features['amount'])
            df_features['merchant_freq'] = df_features['merchant_freq'].fillna(fallback_freq)
        else:
            fallback_freq = typical_merchant_freq if typical_merchant_freq is not None else 1
            df_features['merchant_avg'] = df_features['amount']
            df_features['merchant_freq'] = fallback_freq

    # Feature 3: amount_deviation_from_merchant_avg
    df_features['amount_deviation'] = df_features['amount'] - df_features['merchant_avg']

    # Select final features
    return df_features, FEATURE_COLUMNS, stats

def train_fraud_model(df, contamination=0.03):
    """
    Train an Isolation Forest model to detect fraud, and calibrate a fixed
    decision threshold + score distribution from the training data so that
    inference never has to renormalize per request batch.
    """
    logger.info("Training fraud detection model")
    df_features, feature_cols, merchant_stats = extract_features(df, is_training=True)
    typical_merchant_freq = float(max(merchant_stats['merchant_freq'].median(), 1))

    X = df_features[feature_cols].fillna(0)

    # Initialize and train Isolation Forest
    # Contamination is expected percentage of outliers
    model = IsolationForest(n_estimators=100, contamination=contamination, random_state=42)
    model.fit(X)

    # decision_function: lower score = more anomalous. Calibrate the alert
    # threshold as the contamination-th percentile of the TRAINING scores, so
    # inference reuses this fixed cutoff instead of the min/max of whatever
    # batch happens to be scored.
    scores = model.decision_function(X)
    threshold = float(np.percentile(scores, contamination * 100))
    score_mean = float(scores.mean())
    score_std = float(scores.std()) or 1.0

    metadata = {
        'merchant_stats': merchant_stats,
        'threshold': threshold,
        'score_mean': score_mean,
        'score_std': score_std,
        'feature_columns': feature_cols,
        'contamination': contamination,
        'typical_merchant_freq': typical_merchant_freq,
        'training_date': datetime.now(timezone.utc).isoformat(),
        'training_rows': int(len(df)),
        'metrics': None
    }

    # Save model and stats
    os.makedirs(MODELS_DIR, exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    joblib.dump(metadata, STATS_PATH)

    logger.info(f"Model saved to {MODEL_PATH} (threshold={threshold:.4f})")
    return model, metadata

def save_fraud_metrics(metrics):
    """Attach offline evaluation metrics (computed against the is_fraud label) to the saved model metadata."""
    metadata = joblib.load(STATS_PATH)
    metadata['metrics'] = metrics
    joblib.dump(metadata, STATS_PATH)

def load_fraud_metadata():
    if not os.path.exists(STATS_PATH):
        raise FileNotFoundError("Model stats not found. Train the model first.")
    return joblib.load(STATS_PATH)

def predict_fraud(df, model=None, metadata=None):
    """
    Score transactions for fraud. Returns a DataFrame with:
      - fraud_score: raw IsolationForest decision_function output (lower = more anomalous)
      - fraud_probability: a [0, 1] value calibrated from the TRAINING score
        distribution (fixed mean/std), never from the scored batch itself
      - is_flagged: whether the raw score is at or below the threshold saved
        at training time (the actual alerting decision)
    """
    if model is None or metadata is None:
        if not os.path.exists(MODEL_PATH) or not os.path.exists(STATS_PATH):
            raise FileNotFoundError("Model or stats not found. Train the model first.")
        model = joblib.load(MODEL_PATH)
        metadata = joblib.load(STATS_PATH)

    merchant_stats = metadata['merchant_stats']
    threshold = metadata['threshold']
    score_mean = metadata['score_mean']
    score_std = metadata['score_std']
    feature_cols = metadata.get('feature_columns', FEATURE_COLUMNS)
    typical_merchant_freq = metadata.get('typical_merchant_freq')

    df_features, _, _ = extract_features(df, is_training=False, stats=merchant_stats, typical_merchant_freq=typical_merchant_freq)
    X = df_features[feature_cols].fillna(0)

    scores = model.decision_function(X)

    # Fixed sigmoid calibration around the training score distribution: a score
    # `score_std` below the training mean maps to ~0.73, not "worst-in-batch".
    z = (score_mean - scores) / score_std
    probability = 1.0 / (1.0 + np.exp(-z))

    return pd.DataFrame({
        'fraud_score': scores,
        'fraud_probability': probability,
        'is_flagged': scores <= threshold
    }, index=df.index)
