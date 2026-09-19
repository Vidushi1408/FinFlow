from flask import Blueprint, jsonify, request, g
from flask_login import login_required, current_user
from sqlalchemy import text
from datetime import datetime, date
from webapp.errors import error_response
from webapp.db_utils import user_account_ids

goals_bp = Blueprint('goals', __name__, url_prefix='/api/v1/goals')


def _recent_avg_monthly_savings(account_ids, months=3):
    """Average (income - spending) per month over the trailing N months, used
    to judge whether a goal's required saving rate is actually achievable."""
    if not account_ids:
        return 0.0

    rows = g.db.execute(
        text("""
            SELECT d.year, d.month,
                   SUM(CASE WHEN f.transaction_type = 'CREDIT' THEN f.amount ELSE -f.amount END) as net
            FROM fact_transactions f
            JOIN dim_date d ON f.date_id = d.date_id
            WHERE f.account_id = ANY(:aids) AND d.date >= (CURRENT_DATE - make_interval(months => :months))
            GROUP BY d.year, d.month
        """), {"aids": account_ids, "months": months}
    ).fetchall()

    if not rows:
        return 0.0
    return sum(float(r[2]) for r in rows) / len(rows)


@goals_bp.route('', methods=['GET'])
@login_required
def list_goals():
    rows = g.db.execute(
        text("SELECT goal_id, name, target_amount, deadline, created_at FROM goal WHERE user_id = :uid ORDER BY deadline"),
        {"uid": current_user.id}
    ).fetchall()

    account_ids = user_account_ids(current_user.id)
    avg_monthly_savings = _recent_avg_monthly_savings(account_ids)

    goals = []
    for r in rows:
        days_left = max((r[3] - date.today()).days, 0)
        months_left = max(days_left / 30.0, 1 / 30.0)
        required_monthly = float(r[2]) / months_left
        goals.append({
            "goal_id": str(r[0]),
            "name": r[1],
            "target_amount": float(r[2]),
            "deadline": str(r[3]),
            "days_left": days_left,
            "required_monthly_saving": round(required_monthly, 2),
            "recent_avg_monthly_savings": round(avg_monthly_savings, 2),
            "on_track": avg_monthly_savings >= required_monthly
        })

    return jsonify({"goals": goals})


@goals_bp.route('', methods=['POST'])
@login_required
def create_goal():
    req = request.get_json(silent=True) or {}
    name = (req.get('name') or '').strip()
    target_amount = req.get('target_amount')
    deadline = req.get('deadline')

    if not name:
        return error_response("name is required", 400)
    try:
        target_amount = float(target_amount)
    except (TypeError, ValueError):
        return error_response("target_amount must be a number", 400)
    if target_amount <= 0:
        return error_response("target_amount must be > 0", 400)
    try:
        deadline_date = datetime.strptime(deadline, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return error_response("deadline must be YYYY-MM-DD", 400)
    if deadline_date <= date.today():
        return error_response("deadline must be in the future", 400)

    g.db.execute(
        text("""
            INSERT INTO goal (user_id, name, target_amount, deadline)
            VALUES (:uid, :name, :target, :deadline)
        """),
        {"uid": current_user.id, "name": name, "target": target_amount, "deadline": deadline_date}
    )
    g.db.commit()
    return jsonify({"message": "Goal created"}), 201


@goals_bp.route('/<goal_id>', methods=['DELETE'])
@login_required
def delete_goal(goal_id):
    result = g.db.execute(
        text("DELETE FROM goal WHERE goal_id = :gid AND user_id = :uid"),
        {"gid": goal_id, "uid": current_user.id}
    )
    g.db.commit()
    if result.rowcount == 0:
        return error_response("Goal not found", 404)
    return jsonify({"message": "Goal deleted"})
