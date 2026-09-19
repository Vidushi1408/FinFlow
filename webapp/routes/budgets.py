from flask import Blueprint, jsonify, request, g
from flask_login import login_required, current_user
from sqlalchemy import text
from datetime import date
from webapp.db_utils import user_account_ids
from webapp.errors import error_response

budgets_bp = Blueprint('budgets', __name__, url_prefix='/api/v1/budgets')


@budgets_bp.route('', methods=['GET'])
@login_required
def list_budgets():
    """Per-category monthly limits plus this-month actual and a naive projected month-end spend."""
    account_ids = user_account_ids(current_user.id)
    today = date.today()

    limits = g.db.execute(
        text("SELECT category_id, monthly_limit FROM user_category_budget WHERE user_id = :uid"),
        {"uid": current_user.id}
    ).fetchall()
    limit_map = {r[0]: float(r[1]) for r in limits}

    actuals = {}
    if account_ids:
        rows = g.db.execute(
            text("""
                SELECT f.category_id, SUM(f.amount)
                FROM fact_transactions f
                JOIN dim_date d ON f.date_id = d.date_id
                WHERE f.account_id = ANY(:account_ids) AND f.transaction_type = 'DEBIT'
                  AND d.year = :year AND d.month = :month
                GROUP BY f.category_id
            """),
            {"account_ids": account_ids, "year": today.year, "month": today.month}
        ).fetchall()
        actuals = {r[0]: float(r[1]) for r in rows}

    categories = g.db.execute(text("SELECT category_id, category_name FROM dim_category")).fetchall()

    days_elapsed = max(today.day, 1)
    days_in_month = 28 if today.month == 2 else 30 if today.month in (4, 6, 9, 11) else 31

    budgets = []
    for cat_id, cat_name in categories:
        spent = actuals.get(cat_id, 0.0)
        limit = limit_map.get(cat_id)
        projected = round(spent / days_elapsed * days_in_month, 2)

        status = None
        if limit is not None and limit > 0:
            ratio = spent / limit
            status = 'red' if ratio >= 1.0 else 'amber' if ratio >= 0.8 else 'green'

        budgets.append({
            "category_id": cat_id,
            "category_name": cat_name,
            "monthly_limit": limit,
            "spent_this_month": round(spent, 2),
            "projected_month_end": projected,
            "status": status
        })

    return jsonify({"month": today.strftime("%Y-%m"), "budgets": budgets})


@budgets_bp.route('', methods=['POST'])
@login_required
def set_budget():
    req = request.get_json(silent=True) or {}
    category_id = req.get('category_id')
    monthly_limit = req.get('monthly_limit')

    if not category_id or monthly_limit is None:
        return error_response("category_id and monthly_limit are required", 400)

    try:
        monthly_limit = float(monthly_limit)
    except (TypeError, ValueError):
        return error_response("monthly_limit must be a number", 400)

    if monthly_limit < 0:
        return error_response("monthly_limit must be >= 0", 400)

    valid = g.db.execute(
        text("SELECT 1 FROM dim_category WHERE category_id = :cat"), {"cat": category_id}
    ).fetchone()
    if not valid:
        return error_response(f"Unknown category: {category_id}", 400)

    g.db.execute(
        text("""
            INSERT INTO user_category_budget (user_id, category_id, monthly_limit)
            VALUES (:uid, :cat, :limit)
            ON CONFLICT (user_id, category_id)
            DO UPDATE SET monthly_limit = EXCLUDED.monthly_limit, updated_at = CURRENT_TIMESTAMP
        """),
        {"uid": current_user.id, "cat": category_id, "limit": monthly_limit}
    )
    g.db.commit()

    return jsonify({"category_id": category_id, "monthly_limit": monthly_limit})
