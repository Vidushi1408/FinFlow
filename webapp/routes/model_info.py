from flask import Blueprint, jsonify
from flask_login import login_required
from ml.fraud_model import load_fraud_metadata
from ml.cashflow_model import load_cashflow_metadata

model_info_bp = Blueprint('model_info', __name__, url_prefix='/api/v1/models')

STRATEGY_LABELS = {
    'model': 'Linear regression (day-of-week + payday + rolling averages)',
    'same_period_last_month': 'Naive baseline: same period last month',
    'trailing_30d_avg': 'Naive baseline: trailing 30-day average'
}


@model_info_bp.route('/info', methods=['GET'])
@login_required
def get_model_info():
    info = {"fraud_model": None, "cashflow_model": None}

    try:
        fraud_meta = load_fraud_metadata()
        metrics = fraud_meta.get('metrics') or {}
        info["fraud_model"] = {
            "algorithm": "Isolation Forest",
            "training_date": fraud_meta.get('training_date'),
            "training_rows": fraud_meta.get('training_rows'),
            "feature_columns": fraud_meta.get('feature_columns'),
            "contamination": fraud_meta.get('contamination'),
            "metrics": metrics,
            "plain_language": (
                f"On a held-out test set of synthetic data with known fraud, about "
                f"{round((metrics.get('precision') or 0) * 10, 1)} of every 10 flagged transactions were genuinely fraudulent, "
                f"and it caught {round((metrics.get('recall') or 0) * 100)}% of the fraud in that set. "
                f"On your real accounts, treat a flag as \"this looks unusual for you\" rather than proof of fraud."
                if metrics else "Not yet evaluated -- retrain to get holdout metrics."
            )
        }
    except FileNotFoundError:
        pass

    try:
        cf_meta = load_cashflow_metadata()
        metrics = cf_meta.get('metrics') or {}
        strategy = cf_meta.get('selected_strategy', 'model')
        beat_baseline = strategy == 'model'
        info["cashflow_model"] = {
            "algorithm": STRATEGY_LABELS.get(strategy, strategy),
            "training_date": cf_meta.get('training_date'),
            "training_rows": cf_meta.get('training_rows'),
            "selected_strategy": strategy,
            "metrics": metrics,
            "plain_language": (
                (
                    f"The regression model beat both naive baselines in backtesting "
                    f"(MAE {metrics.get('model_mae', 0):.0f} vs "
                    f"{metrics.get('baseline_last_month_mae', 0):.0f} and "
                    f"{metrics.get('baseline_trailing_avg_mae', 0):.0f}), so it's what powers your forecast."
                    if beat_baseline else
                    f"The regression model did NOT beat a naive baseline in backtesting, so forecasts "
                    f"currently use {STRATEGY_LABELS.get(strategy, strategy).lower()} instead -- "
                    f"simpler, and more accurate on your data."
                )
            )
        }
    except FileNotFoundError:
        pass

    return jsonify(info)
