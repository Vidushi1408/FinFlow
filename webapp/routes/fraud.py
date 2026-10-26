from flask import Blueprint, jsonify, request, g
from flask_login import login_required, current_user
from sqlalchemy import text
import pandas as pd
from ml.fraud_model import predict_fraud
from webapp.db_utils import user_account_ids
from webapp.errors import error_response
from etl.transform.clean import merchant_key
from webapp.services.alert_learning import LOOKS_LIKE_YOURS_BELOW, build_features, fit_feedback_model, p_suspicious
from webapp.services.alert_threshold import effective_threshold

fraud_bp = Blueprint('fraud', __name__, url_prefix='/api/v1/fraud')

TRUSTED_MERCHANT_AMOUNT_TOLERANCE = 1.5
ROUTINE_MIN_PRIOR = 3          # same-merchant history needed to call a charge routine
ROUTINE_AMOUNT_TOLERANCE = 0.20  # within 20% of that merchant's usual amount
ALERT_WINDOW_ROWS = 200      # most recent debits an alert can come from
HISTORY_ROWS = 2000          # recent debits scored to learn this user's own normal level


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


def _learn_from_feedback(account_ids, profile):
    """(model_or_None, info) fitted from this user's own reviewed alerts. Never raises: any problem
    (no trained fraud model, no feedback) just means learning stays off."""
    info = {"labels": 0, "active": False, "cv_auc": None, "reason": "no feedback yet"}
    rows = g.db.execute(text("""
        SELECT f.transaction_id, f.merchant_id, f.amount, f.transaction_ts, f.transaction_type, ff.feedback
        FROM fraud_feedback ff JOIN fact_transactions f ON f.transaction_id = ff.transaction_id
        WHERE ff.user_id = :uid AND f.account_id = ANY(:aids)
    """), {"uid": current_user.id, "aids": account_ids}).fetchall()
    if not rows:
        return None, info
    labelled = pd.DataFrame(rows, columns=["transaction_id", "merchant_id", "amount", "transaction_ts", "transaction_type", "feedback"])
    labelled["amount"] = labelled["amount"].astype(float)
    labelled["transaction_ts"] = pd.to_datetime(labelled["transaction_ts"])
    try:
        scored = predict_fraud(labelled)
    except FileNotFoundError:
        info["reason"] = "fraud model not trained"
        return None, info
    labelled["fraud_probability"] = scored["fraud_probability"]
    X = build_features(labelled, profile)
    return fit_feedback_model(X, (labelled["feedback"] == "SUSPICIOUS").astype(int))


@fraud_bp.route('/alerts', methods=['GET'])
@login_required
def get_fraud_alerts():
    threshold = request.args.get('threshold', default=current_user.alert_sensitivity, type=float)
    if threshold < 0 or threshold > 1:
        return error_response("Threshold must be between 0 and 1.", 400)

    account_ids = user_account_ids(current_user.id)
    if not account_ids:
        return jsonify({"threshold": threshold, "threshold_source": "global", "alerts": []})

    query = text("""
        SELECT f.transaction_id, f.account_id, f.merchant_id, f.amount, f.transaction_type, d.date, f.transaction_ts
        FROM fact_transactions f
        JOIN dim_date d ON f.date_id = d.date_id
        WHERE f.transaction_type = 'DEBIT' AND f.account_id = ANY(:account_ids)
        ORDER BY f.transaction_ts DESC
        LIMIT :history_rows
    """)
    df = pd.read_sql(query, g.db.connection(), params={"account_ids": account_ids, "history_rows": HISTORY_ROWS})

    if df.empty:
        return jsonify({"threshold": threshold, "threshold_source": "global", "alerts": []})

    try:
        scored = predict_fraud(df)
        df['fraud_probability'] = scored['fraud_probability']
        df['is_flagged'] = scored['is_flagged']
    except FileNotFoundError:
        df['fraud_probability'] = 0.0
        df['is_flagged'] = False

    # Personalise: the slider/sensitivity is the floor; once there is enough history the cutoff rises to
    # just above this user's own normal level (never below the floor). `personalize=0` returns raw results.
    threshold_source = "global"
    if request.args.get('personalize', default='1') != '0':
        threshold, threshold_source = effective_threshold(df['fraud_probability'], threshold)

    flagged = df.head(ALERT_WINDOW_ROWS)
    flagged = flagged[flagged['fraud_probability'] >= threshold]

    # Feedback already given by this user, so alerts they've dismissed/confirmed aren't re-shown as pending.
    feedback_rows = g.db.execute(
        text("SELECT transaction_id, feedback FROM fraud_feedback WHERE user_id = :uid"),
        {"uid": current_user.id}
    ).fetchall()
    feedback_map = {r[0]: r[1] for r in feedback_rows}

    profile = _build_user_profile(account_ids)

    # "This was me" teaches the alert list: later charges from a merchant the user has confirmed are
    # not re-flagged unless the amount is well above anything they confirmed there (a 10x charge at a
    # trusted merchant is still worth a look).
    trusted = {
        r[0]: float(r[1]) for r in g.db.execute(text("""
            SELECT f.merchant_id, MAX(f.amount) FROM fraud_feedback ff
            JOIN fact_transactions f ON f.transaction_id = ff.transaction_id
            WHERE ff.user_id = :uid AND ff.feedback = 'CONFIRMED_ME' AND f.account_id = ANY(:aids)
            GROUP BY f.merchant_id
        """), {"uid": current_user.id, "aids": account_ids}).fetchall()
    }

    # Re-rank by what this user's own feedback says looks suspicious vs. like them. Ordering and a hint only:
    # no alert is ever removed by this. `personalize=0` turns it off.
    learning = {"labels": len(feedback_rows), "active": False, "cv_auc": None, "reason": "personalisation off"}
    p_map = {}
    if request.args.get('personalize', default='1') != '0':
        learned, learning = _learn_from_feedback(account_ids, profile)
        if learned is not None and not flagged.empty:
            p_map = dict(zip(flagged['transaction_id'], p_suspicious(learned, build_features(flagged, profile))))

    # A charge that repeats a long-standing pattern (the 5th identical rent payment, the usual Swiggy
    # order) is expected, however large it looks next to the user's median transaction. Unusual means
    # unusual for THAT merchant too, so routine repeats are left out and counted.
    df['_merchant_key'] = df['merchant_id'].map(merchant_key)

    def is_routine(row):
        history = df[(df['_merchant_key'] == merchant_key(row['merchant_id'])) & (df['transaction_id'] != row['transaction_id'])]
        if len(history) < ROUTINE_MIN_PRIOR:
            return False
        usual = float(history['amount'].median())
        return usual > 0 and abs(float(row['amount']) - usual) <= ROUTINE_AMOUNT_TOLERANCE * usual

    alerts = []
    suppressed = 0
    suppressed_routine = 0
    for _, row in flagged.iterrows():
        if feedback_map.get(row['transaction_id']) != 'SUSPICIOUS' and is_routine(row):
            suppressed_routine += 1
            continue
        trusted_max = trusted.get(row['merchant_id'])
        if (trusted_max is not None and float(row['amount']) <= trusted_max * TRUSTED_MERCHANT_AMOUNT_TOLERANCE
                and feedback_map.get(row['transaction_id']) != 'SUSPICIOUS'):
            suppressed += 1
            continue
        alerts.append({
            "transaction_id": row['transaction_id'],
            "account_id": row['account_id'],
            "merchant_id": row['merchant_id'],
            "amount": float(row['amount']),
            "date": str(row['date']),
            "fraud_probability": float(row['fraud_probability']),
            "is_flagged": bool(row['is_flagged']),
            "reasons": _explain(row, profile),
            "feedback": feedback_map.get(row['transaction_id']),
            "p_suspicious": round(float(p_map[row['transaction_id']]), 3) if row['transaction_id'] in p_map else None,
            "resembles_yours": bool(row['transaction_id'] in p_map and p_map[row['transaction_id']] < LOOKS_LIKE_YOURS_BELOW),
        })
    if p_map:
        alerts.sort(key=lambda a: (-(a["p_suspicious"] or 0), -a["fraud_probability"]))

    return jsonify({
        "threshold": threshold,
        "threshold_source": threshold_source,
        "alerts": alerts,
        "learning": learning,
        "suppressed_trusted_merchant_count": suppressed,
        "suppressed_routine_count": suppressed_routine
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
