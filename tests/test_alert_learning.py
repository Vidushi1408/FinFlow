import numpy as np
import pandas as pd

from webapp.services.alert_learning import (
    FEATURES, MIN_CV_AUC, MIN_LABELS, build_features, fit_feedback_model, p_suspicious,
)

PROFILE = {"median_amount": 500.0, "known_merchants": {"SWIGGY", "METRO"}, "common_hours": {9, 10, 12, 19, 20, 21}}


def _tx(rows):
    return pd.DataFrame(rows, columns=["amount", "merchant_id", "transaction_ts", "fraud_probability"])


def test_features_describe_the_charge_relative_to_the_user():
    df = _tx([(500.0, "SWIGGY", "2026-01-01 20:00:00", 0.9), (50000.0, "UNKNOWN LTD", "2026-01-01 03:00:00", 0.99)])
    X = build_features(df, PROFILE)
    assert list(X.columns) == FEATURES
    assert X.loc[0, "new_merchant"] == 0 and X.loc[0, "odd_hour"] == 0
    assert X.loc[1, "new_merchant"] == 1 and X.loc[1, "odd_hour"] == 1
    assert X.loc[1, "amount_ratio"] > X.loc[0, "amount_ratio"]


def _synthetic(n_each, seed=0, separable=True):
    rng = np.random.default_rng(seed)
    mine = pd.DataFrame({"amount_ratio": rng.normal(0.3, 0.2, n_each), "new_merchant": 0.0, "odd_hour": 0.0,
                         "model_probability": rng.uniform(0.8, 0.9, n_each)})
    bad = pd.DataFrame({"amount_ratio": rng.normal(3.0 if separable else 0.3, 0.3 if separable else 0.2, n_each),
                        "new_merchant": 1.0 if separable else 0.0, "odd_hour": 1.0 if separable else 0.0,
                        "model_probability": rng.uniform(0.9, 0.99, n_each)})
    X = pd.concat([mine, bad], ignore_index=True)
    return X, np.array([0] * n_each + [1] * n_each)


def test_stays_off_without_enough_labels_or_both_classes():
    X, y = _synthetic(3)
    model, info = fit_feedback_model(X, y)
    assert model is None and not info["active"] and str(MIN_LABELS) in info["reason"]

    X, y = _synthetic(14)
    model, info = fit_feedback_model(X[y == 0], y[y == 0])
    assert model is None and "each kind" in info["reason"]


def test_turns_on_when_feedback_is_learnable_and_ranks_suspicious_higher():
    X, y = _synthetic(10)
    model, info = fit_feedback_model(X, y)
    assert info["active"] and info["cv_auc"] >= MIN_CV_AUC
    p = p_suspicious(model, X)
    assert p[y == 1].mean() > p[y == 0].mean() + 0.4


def test_stays_off_when_labels_are_noise():
    rng = np.random.default_rng(5)
    X, _ = _synthetic(12, separable=False)
    y = rng.permutation([0] * 12 + [1] * 12)
    model, info = fit_feedback_model(X, y)
    assert model is None and "AUC" in info["reason"]
