from flask import Blueprint, jsonify, request, g
from flask_login import login_required, current_user
from sqlalchemy import text
import pandas as pd
from ml.fraud_model import predict_fraud
from webapp.db_utils import user_account_ids
from webapp.errors import error_response

fraud_bp = Blueprint('fraud', __name__, url_prefix='/api/v1/fraud')


def _build_user_profile(account_ids):
    """Median amount, known merchants, and common hours -- used to explain WHY
    a transaction was flagged, in terms of the user's own normal behaviour."""
    if not account_ids:
        return {"median_amount": 0.0, "known_merchants": set(), "common_hours": set()}

    query = text("""
        SELECT amount, merchant_id, EXTRACT(HOUR FROM transaction_ts) as hour
        FROM fact_transactions
        WHERE account_id = ANY(:account_ids) AND transaction_type = 'DEBIT'
    """)
    df = pd.read_sql(query, g.db.connection(), params={"account_ids": account_ids})

    if df.empty:
        return {"median_amount": 0.0, "known_merchants": set(), "common_hours": set()}

    merchant_counts = df['merchant_id'].value_counts()
    known_merchants = set(merchant_counts[merchant_counts >= 2].index)

    hour_counts = df['hour'].value_counts(normalize=True).sort_values(ascending=False)
    common_hours = set(hour_counts[hour_counts.cumsum() <= 0.85].index) | {hour_counts.index[0]}

    return {
        "median_amount": float(df['amount'].median()),
        "known_merchants": known_merchants,
        "common_hours": common_hours
    }


def _explain(row, profile):
    reasons = []
    if profile["median_amount"] > 0:
        ratio = row['amount'] / profile["median_amount"]
        if ratio >= 3:
            reasons.append(f"{ratio:.1f}× your median transaction")

    if row['merchant_id'] not in profile["known_merchants"]:
        reasons.append("new merchant")

    hour = pd.to_datetime(row['transaction_ts']).hour
    if profile["common_hours"] and hour not in profile["common_hours"]:
        reasons.append(f"{hour:02d}:00, outside your usual hours")

    if not reasons:
        reasons.append("unusual pattern compared with your normal activity")
    return reasons


@fraud_bp.route('/alerts', methods=['GET'])
@login_required
def get_fraud_alerts():
    threshold = request.args.get('threshold', default=current_user.alert_sensitivity, type=float)
    if threshold < 0 or threshold > 1:
        return error_response("Threshold must be between 0 and 1.", 400)

    account_ids = user_account_ids(current_user.id)
    if not account_ids:
        return jsonify({"threshold": threshold, "alerts": []})

    query = text("""
        SELECT f.transaction_id, f.account_id, f.merchant_id, f.amount, f.transaction_type, d.date, f.transaction_ts
        FROM fact_transactions f
        JOIN dim_date d ON f.date_id = d.date_id
        WHERE f.transaction_type = 'DEBIT' AND f.account_id = ANY(:account_ids)
        ORDER BY f.transaction_ts DESC
        LIMIT 200
    """)
    df = pd.read_sql(query, g.db.connection(), params={"account_ids": account_ids})

    if df.empty:
        return jsonify({"threshold": threshold, "alerts": []})

    try:
        scored = predict_fraud(df)
        df['fraud_probability'] = scored['fraud_probability']
        df['is_flagged'] = scored['is_flagged']
    except FileNotFoundError:
        df['fraud_probability'] = 0.0
        df['is_flagged'] = False

    flagged = df[df['fraud_probability'] >= threshold]

    # Feedback already given by this user, so alerts they've dismissed/confirmed aren't re-shown as pending.
    feedback_rows = g.db.execute(
        text("SELECT transaction_id, feedback FROM fraud_feedback WHERE user_id = :uid"),
        {"uid": current_user.id}
    ).fetchall()
    feedback_map = {r[0]: r[1] for r in feedback_rows}

    profile = _build_user_profile(account_ids)

    alerts = []
    for _, row in flagged.iterrows():
        alerts.append({
            "transaction_id": row['transaction_id'],
            "account_id": row['account_id'],
            "merchant_id": row['merchant_id'],
            "amount": float(row['amount']),
            "date": str(row['date']),
            "fraud_probability": float(row['fraud_probability']),
            "is_flagged": bool(row['is_flagged']),
            "reasons": _explain(row, profile),
            "feedback": feedback_map.get(row['transaction_id'])
        })

    return jsonify({
        "threshold": threshold,
        "alerts": alerts
    })


@fraud_bp.route('/feedback', methods=['POST'])
@login_required
def submit_fraud_feedback():
    req = request.get_json(silent=True) or {}
    transaction_id = req.get('transaction_id')
    feedback = req.get('feedback')

    if feedback not in ('CONFIRMED_ME', 'SUSPICIOUS'):
        return error_response("feedback must be CONFIRMED_ME or SUSPICIOUS", 400)
    if not transaction_id:
        return error_response("transaction_id is required", 400)

    account_ids = user_account_ids(current_user.id)
    owns_tx = g.db.execute(
        text("SELECT 1 FROM fact_transactions WHERE transaction_id = :tid AND account_id = ANY(:account_ids)"),
        {"tid": transaction_id, "account_ids": account_ids}
    ).fetchone()
    if not owns_tx:
        return error_response("Transaction not found", 404)

    g.db.execute(
        text("""
            INSERT INTO fraud_feedback (transaction_id, user_id, feedback)
            VALUES (:tid, :uid, :feedback)
            ON CONFLICT (transaction_id, user_id) DO UPDATE SET feedback = EXCLUDED.feedback, created_at = CURRENT_TIMESTAMP
        """),
        {"tid": transaction_id, "uid": current_user.id, "feedback": feedback}
    )
    g.db.commit()

    return jsonify({"transaction_id": transaction_id, "feedback": feedback})
