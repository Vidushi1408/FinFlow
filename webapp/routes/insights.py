from flask import Blueprint, jsonify, request, g
from flask_login import login_required, current_user
from sqlalchemy import text
import pandas as pd
from etl.transform.clean import summarize_recurring_series
from webapp.db_utils import user_account_ids
from webapp.errors import error_response

insights_bp = Blueprint('insights', __name__, url_prefix='/api/v1/insights')

@insights_bp.route('/subscriptions', methods=['GET'])
@login_required
def get_subscriptions():
    account_ids = user_account_ids(current_user.id)
    if not account_ids:
        return jsonify({"total_annual_cost": 0, "subscriptions": []})

    query = text("""
        SELECT transaction_id, account_id, merchant_id, amount, transaction_ts as date
        FROM fact_transactions
        WHERE account_id = ANY(:account_ids) AND transaction_type = 'DEBIT'
    """)
    df = pd.read_sql(query, g.db.connection(), params={"account_ids": account_ids})

    if df.empty:
        return jsonify({"total_annual_cost": 0, "subscriptions": []})

    df['date'] = pd.to_datetime(df['date'])
    summary = summarize_recurring_series(df)

    overrides = g.db.execute(
        text("SELECT account_id, merchant_id, status FROM subscription_override WHERE user_id = :uid"),
        {"uid": current_user.id}
    ).fetchall()
    override_map = {(r[0], r[1]): r[2] for r in overrides}

    total_annual = 0.0
    subs = []
    for _, row in summary.iterrows():
        status = override_map.get((row['account_id'], row['merchant_id']))
        if status == 'NOT_SUBSCRIPTION':
            continue

        annual_cost = float(row['annualized_cost'])
        total_annual += annual_cost
        subs.append({
            "account_id": row['account_id'],
            "merchant_id": row['merchant_id'],
            "cadence": row['cadence'],
            "typical_amount": float(row['typical_amount']),
            "next_expected_date": str(row['next_expected_date'].date()),
            "annualized_cost": annual_cost,
            "transaction_count": int(row['tx_count']),
            "status": status
        })

    subs.sort(key=lambda s: s['annualized_cost'], reverse=True)

    return jsonify({
        "total_annual_cost": total_annual,
        "subscriptions": subs
    })


@insights_bp.route('/subscriptions/override', methods=['POST'])
@login_required
def override_subscription():
    req = request.get_json(silent=True) or {}
    account_id = req.get('account_id')
    merchant_id = req.get('merchant_id')
    status = req.get('status')

    if status not in ('CANCEL_CANDIDATE', 'NOT_SUBSCRIPTION'):
        return error_response("status must be CANCEL_CANDIDATE or NOT_SUBSCRIPTION", 400)

    account_ids = user_account_ids(current_user.id)
    if account_id not in account_ids:
        return error_response("Unknown account", 404)

    g.db.execute(
        text("""
            INSERT INTO subscription_override (user_id, account_id, merchant_id, status)
            VALUES (:uid, :account_id, :merchant_id, :status)
            ON CONFLICT (user_id, account_id, merchant_id)
            DO UPDATE SET status = EXCLUDED.status, updated_at = CURRENT_TIMESTAMP
        """),
        {"uid": current_user.id, "account_id": account_id, "merchant_id": merchant_id, "status": status}
    )
    g.db.commit()

    return jsonify({"account_id": account_id, "merchant_id": merchant_id, "status": status})
