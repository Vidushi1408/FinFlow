from flask import Blueprint, jsonify, request, g
from flask_login import login_required, current_user
from sqlalchemy import text
from webapp.db_utils import user_account_ids
from webapp.errors import error_response

whatif_bp = Blueprint('whatif', __name__, url_prefix='/api/v1/what-if')

@whatif_bp.route('/budget', methods=['POST'])
@login_required
def simulate_budget():
    req = request.get_json()
    add_savings = float(req.get('add_savings', 0))
    reduce_categories = req.get('reduce_categories', {})

    account_ids = user_account_ids(current_user.id)
    monthly_savings = add_savings

    if reduce_categories:
        cats = list(reduce_categories.keys())

        # Whitelist: only category_ids that actually exist in dim_category are
        # allowed through to the query. Anything else is rejected outright.
        valid_rows = g.db.execute(
            text("SELECT category_id FROM dim_category WHERE category_id = ANY(:cats)"),
            {"cats": cats}
        ).fetchall()
        valid_cats = {row[0] for row in valid_rows}
        invalid_cats = [c for c in cats if c not in valid_cats]
        if invalid_cats:
            return error_response(f"Unknown category key(s): {invalid_cats}", 400)

        if account_ids:
            query = text("""
                SELECT category_id, AVG(amount)
                FROM fact_transactions
                WHERE category_id = ANY(:cats) AND account_id = ANY(:account_ids)
                GROUP BY category_id
            """)
            results = g.db.execute(query, {"cats": cats, "account_ids": account_ids}).fetchall()

            for row in results:
                cat_id = row[0]
                avg_spend = float(row[1])
                reduction_factor = float(reduce_categories.get(cat_id, 0))
                monthly_savings += avg_spend * reduction_factor

    # Latest closing balance across this user's own accounts only.
    if account_ids:
        balance_query = text("""
            SELECT COALESCE(SUM(latest.closing_balance), 0)
            FROM (
                SELECT DISTINCT ON (account_id) account_id, closing_balance
                FROM fact_account_balance
                WHERE account_id = ANY(:account_ids)
                ORDER BY account_id, date_id DESC
            ) latest
        """)
        current_balance = float(g.db.execute(balance_query, {"account_ids": account_ids}).scalar() or 0)
    else:
        current_balance = 0.0

    projected = current_balance + (monthly_savings * 6)
    
    return jsonify({
        "monthly_savings_impact": float(monthly_savings),
        "projected_balance_6_months": float(projected)
    })

@whatif_bp.route('/investments', methods=['POST'])
@login_required
def simulate_investments():
    req = request.get_json()
    invest_amount = float(req.get('invest_amount', 0))
    allocation = req.get('allocation', {'stocks': 0.6, 'bonds': 0.4})
    
    stock_return = 0.07
    bond_return = 0.03
    
    blended_rate = (float(allocation.get('stocks', 0)) * stock_return) + (float(allocation.get('bonds', 0)) * bond_return)
    
    p = invest_amount
    r = blended_rate
    
    return jsonify({
        "projected_1_year": p * (1 + r)**1,
        "projected_5_years": p * (1 + r)**5,
        "projected_10_years": p * (1 + r)**10
    })
