"""Learn from "This was me" / "Report as suspicious" to re-rank a user's own alerts.

The fraud model is unsupervised, so labels can't go into it. Instead, a tiny per-user logistic regression
is fitted on that user's reviewed alerts, using four features that describe how a charge relates to their
own normal behaviour. It is deliberately modest:

  * it only RE-RANKS alerts (likely-suspicious first) and tags ones that resemble charges the user
    confirmed as theirs; it never hides or removes an alert;
  * it switches itself on only with enough labels of BOTH kinds AND a cross-validated AUC above a bar,
    so a handful of clicks can't make the ordering worse than the default;
  * everything is per user, fitted on the fly from their own feedback rows (nothing stored, nothing shared).
"""
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedKFold, cross_val_predict

MIN_LABELS = 12
MIN_PER_CLASS = 3
MIN_CV_AUC = 0.65
LOOKS_LIKE_YOURS_BELOW = 0.25     # p(suspicious) under this => "resembles charges you confirmed"

FEATURES = ["amount_ratio", "new_merchant", "odd_hour", "model_probability"]


def build_features(df, profile):
    """One row per transaction: how unusual it is for THIS user (needs amount, merchant_id, transaction_ts,
    fraud_probability). `profile` is fraud._build_user_profile's dict."""
    median = profile["median_amount"] or 1.0
    hours = pd.to_datetime(df["transaction_ts"]).dt.hour
    common = profile["common_hours"]
    return pd.DataFrame({
        "amount_ratio": np.log1p(df["amount"].astype(float) / median),
        "new_merchant": (~df["merchant_id"].isin(profile["known_merchants"])).astype(float),
        "odd_hour": (~hours.isin(common)).astype(float) if common else 0.0,
        "model_probability": df["fraud_probability"].astype(float),
    }, index=df.index)[FEATURES]


def fit_feedback_model(X, y):
    """(model, info). model is None until there are enough labels and the cross-validated AUC clears the bar;
    info says why, for display and tests. y: 1 = reported suspicious, 0 = confirmed as theirs."""
    y = np.asarray(y, dtype=int)
    info = {"labels": int(len(y)), "suspicious": int(y.sum()), "confirmed_me": int((y == 0).sum()),
            "active": False, "cv_auc": None, "reason": None}
    if len(y) < MIN_LABELS:
        info["reason"] = f"needs {MIN_LABELS} reviewed alerts (has {len(y)})"
        return None, info
    if min(info["suspicious"], info["confirmed_me"]) < MIN_PER_CLASS:
        info["reason"] = f"needs at least {MIN_PER_CLASS} of each kind of feedback"
        return None, info

    model = LogisticRegression(C=1.0, class_weight="balanced", max_iter=500)
    folds = StratifiedKFold(n_splits=min(3, info["suspicious"], info["confirmed_me"]), shuffle=True, random_state=0)
    held_out = cross_val_predict(model, X, y, cv=folds, method="predict_proba")[:, 1]
    info["cv_auc"] = round(float(roc_auc_score(y, held_out)), 3)
    if info["cv_auc"] < MIN_CV_AUC:
        info["reason"] = f"feedback doesn't predict itself well enough yet (cross-validated AUC {info['cv_auc']} < {MIN_CV_AUC})"
        return None, info
    model.fit(X, y)
    info["active"] = True
    return model, info


def p_suspicious(model, X):
    return model.predict_proba(X)[:, 1]
