from flask import Blueprint, jsonify, g
from flask_login import login_required, current_user
from sqlalchemy import text
from datetime import date
import pandas as pd
from etl.transform.clean import summarize_recurring_series
from ml.fraud_model import predict_fraud
from webapp.db_utils import user_account_ids

dashboard_bp = Blueprint('dashboard', __name__, url_prefix='/api/v1/dashboard')


def _clamp(x, low=0.0, high=1.0):
    return max(low, min(high, x))


def _financial_health_score(income, spending, avg_daily_spend, balance, budgets, annual_sub_cost):
    """
    Composite 0-100 score from four components, each already a 0-1 fraction:
      - savings_rate (35%): (income - spending) / income this month
      - budget_adherence (25%): how far spending stayed under each set budget
      - subscription_load (20%): inverse of subscriptions as a share of income
      - emergency_buffer (20%): days the current balance would cover at the
        recent average daily spend rate, capped at 90 days for a full score
    Missing inputs (no income yet, no budgets set) fall back to a neutral 0.6
    rather than 0, so a new account isn't punished for lacking history.
    """
    savings_component = _clamp((income - spending) / income) if income > 0 else 0.6

    if budgets:
        ratios = [min(b['spent_this_month'] / b['monthly_limit'], 1.5) for b in budgets if b['monthly_limit']]
        budget_component = _clamp(1 - (sum(ratios) / len(ratios)) / 1.5) if ratios else 0.6
    else:
        budget_component = 0.6

    monthly_income = income if income > 0 else spending  # fall back so this never divides by zero
    sub_load = (annual_sub_cost / 12) / monthly_income if monthly_income > 0 else 0
    subscription_component = _clamp(1 - sub_load / 0.30)

    buffer_days = (balance / avg_daily_spend) if avg_daily_spend > 0 else 90
    buffer_component = _clamp(buffer_days / 90)

    score = 100 * (
        0.35 * savings_component +
        0.25 * budget_component +
        0.20 * subscription_component +
        0.20 * buffer_component
    )

    return {
        "score": round(score, 1),
        "components": {
            "savings_rate": round(savings_component * 100, 1),
            "budget_adherence": round(budget_component * 100, 1),
            "subscription_load": round(subscription_component * 100, 1),
            "emergency_buffer_days": round(buffer_days, 1)
        }
    }


@dashboard_bp.route('/summary', methods=['GET'])
@login_required
def dashboard_summary():
    account_ids = user_account_ids(current_user.id)
    today = date.today()

    empty = {
        "current_balance": 0, "financial_health": {"score": 0, "components": {}},
        "this_month": {"income": 0, "spending": 0}, "spending_by_category": [],
        "trend_6_months": [], "latest_alerts": [], "subscriptions_due_soon": []
    }
    if not account_ids:
        return jsonify(empty)

    balance = float(g.db.execute(
        text("""
            SELECT COALESCE(SUM(latest.closing_balance), 0)
            FROM (
                SELECT DISTINCT ON (account_id) account_id, closing_balance
                FROM fact_account_balance WHERE account_id = ANY(:aids)
                ORDER BY account_id, date_id DESC
            ) latest
        """), {"aids": account_ids}
    ).scalar() or 0)

    month_row = g.db.execute(
        text("""
            SELECT
                COALESCE(SUM(CASE WHEN f.transaction_type = 'CREDIT' THEN f.amount ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN f.transaction_type = 'DEBIT' THEN f.amount ELSE 0 END), 0)
            FROM fact_transactions f
            JOIN dim_date d ON f.date_id = d.date_id
            WHERE f.account_id = ANY(:aids) AND d.year = :year AND d.month = :month
        """), {"aids": account_ids, "year": today.year, "month": today.month}
    ).fetchone()
    income, spending = float(month_row[0]), float(month_row[1])

    avg_daily_spend = float(g.db.execute(
        text("""
            SELECT COALESCE(SUM(amount), 0) / 30.0
            FROM fact_transactions
            WHERE account_id = ANY(:aids) AND transaction_type = 'DEBIT'
              AND transaction_ts >= CURRENT_DATE - INTERVAL '30 days'
        """), {"aids": account_ids}
    ).scalar() or 0)

    category_rows = g.db.execute(
        text("""
            SELECT f.category_id, SUM(f.amount)
            FROM fact_transactions f
            JOIN dim_date d ON f.date_id = d.date_id
            WHERE f.account_id = ANY(:aids) AND f.transaction_type = 'DEBIT'
              AND d.year = :year AND d.month = :month
            GROUP BY f.category_id ORDER BY SUM(f.amount) DESC
        """), {"aids": account_ids, "year": today.year, "month": today.month}
    ).fetchall()
    spending_by_category = [{"category_id": r[0], "amount": float(r[1])} for r in category_rows]

    trend_rows = g.db.execute(
        text("""
            SELECT d.year, d.month,
                   SUM(CASE WHEN f.transaction_type = 'DEBIT' THEN f.amount ELSE 0 END) as spend,
                   SUM(CASE WHEN f.transaction_type = 'CREDIT' THEN f.amount ELSE 0 END) as income
            FROM fact_transactions f
            JOIN dim_date d ON f.date_id = d.date_id
            WHERE f.account_id = ANY(:aids)
              AND d.date >= (CURRENT_DATE - INTERVAL '6 months')
            GROUP BY d.year, d.month ORDER BY d.year, d.month
        """), {"aids": account_ids}
    ).fetchall()
    trend_6_months = [{"month": f"{r[0]}-{r[1]:02d}", "spending": float(r[2]), "income": float(r[3])} for r in trend_rows]

    budget_limits = g.db.execute(
        text("SELECT category_id, monthly_limit FROM user_category_budget WHERE user_id = :uid"),
        {"uid": current_user.id}
    ).fetchall()
    limit_map = {r[0]: float(r[1]) for r in budget_limits}
    spent_map = {c['category_id']: c['amount'] for c in spending_by_category}
    budgets = [
        {"category_id": cid, "monthly_limit": limit, "spent_this_month": spent_map.get(cid, 0.0)}
        for cid, limit in limit_map.items()
    ]

    tx_df = pd.read_sql(
        text("""
            SELECT transaction_id, account_id, merchant_id, amount, transaction_ts as date
            FROM fact_transactions
            WHERE account_id = ANY(:aids) AND transaction_type = 'DEBIT'
        """), g.db.connection(), params={"aids": account_ids}
    )
    annual_sub_cost = 0.0
    subscriptions_due_soon = []
    if not tx_df.empty:
        tx_df['date'] = pd.to_datetime(tx_df['date'])
        recurring = summarize_recurring_series(tx_df)
        annual_sub_cost = float(recurring['annualized_cost'].sum()) if not recurring.empty else 0.0
        if not recurring.empty:
            upcoming = recurring[
                recurring['next_expected_date'] <= pd.Timestamp.now() + pd.Timedelta(days=7)
            ].sort_values('next_expected_date')
            subscriptions_due_soon = [{
                "merchant_id": r['merchant_id'],
                "typical_amount": float(r['typical_amount']),
                "next_expected_date": str(r['next_expected_date'].date())
            } for _, r in upcoming.iterrows()]

    latest_alerts = []
    try:
        recent_df = pd.read_sql(
            text("""
                SELECT transaction_id, merchant_id, amount, transaction_type, transaction_ts
                FROM fact_transactions
                WHERE account_id = ANY(:aids) AND transaction_type = 'DEBIT'
                ORDER BY transaction_ts DESC LIMIT 200
            """), g.db.connection(), params={"aids": account_ids}
        )
        if not recent_df.empty:
            scored = predict_fraud(recent_df)
            recent_df['fraud_probability'] = scored['fraud_probability']
            recent_df['is_flagged'] = scored['is_flagged']
            top = recent_df[recent_df['is_flagged']].sort_values('fraud_probability', ascending=False).head(3)
            latest_alerts = [{
                "transaction_id": r['transaction_id'], "merchant_id": r['merchant_id'],
                "amount": float(r['amount']), "date": str(r['transaction_ts']),
                "fraud_probability": float(r['fraud_probability'])
            } for _, r in top.iterrows()]
    except FileNotFoundError:
        pass

    health = _financial_health_score(income, spending, avg_daily_spend, balance, budgets, annual_sub_cost)

    return jsonify({
        "current_balance": balance,
        "financial_health": health,
        "this_month": {"income": income, "spending": spending},
        "spending_by_category": spending_by_category,
        "trend_6_months": trend_6_months,
        "latest_alerts": latest_alerts,
        "subscriptions_due_soon": subscriptions_due_soon
    })
