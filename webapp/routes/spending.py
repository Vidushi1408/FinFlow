from flask import Blueprint, jsonify, request, g
from flask_login import login_required, current_user
from sqlalchemy import text
import pandas as pd
from webapp.db_utils import user_account_ids
from webapp.errors import error_response

spending_bp = Blueprint('spending', __name__, url_prefix='/api/v1/spending')

@spending_bp.route('/summary', methods=['GET'])
@login_required
def get_spending_summary():
    month = request.args.get('month')
    if not month:
        return error_response("Month parameter is required (YYYY-MM)", 400)

    try:
        year, m = month.split('-')
        if len(year) != 4 or len(m) != 2:
            raise ValueError
    except ValueError:
        return error_response("Invalid month format. Use YYYY-MM", 400)

    account_ids = user_account_ids(current_user.id)
    if not account_ids:
        return jsonify({"month": month, "total_spent": 0, "transaction_count": 0, "categories": []})

    query = text("""
        SELECT c.category_id, SUM(f.amount) as total_spent, COUNT(f.transaction_id) as tx_count
        FROM fact_transactions f
        JOIN dim_category c ON f.category_id = c.category_id
        JOIN dim_date d ON f.date_id = d.date_id
        WHERE d.year = :year AND d.month = :month AND f.transaction_type = 'DEBIT'
          AND f.account_id = ANY(:account_ids)
        GROUP BY c.category_id
    """)
    results = g.db.execute(query, {"year": int(year), "month": int(m), "account_ids": account_ids}).fetchall()
    
    categories = []
    total_spent = 0
    total_count = 0
    
    for row in results:
        cat_id = row[0]
        spent = float(row[1])
        count = row[2]
        
        categories.append({
            "category_id": cat_id,
            "total_spent": spent,
            "transaction_count": count
        })
        total_spent += spent
        total_count += count
        
    return jsonify({
        "month": month,
        "total_spent": total_spent,
        "transaction_count": total_count,
        "categories": categories
    })

@spending_bp.route('/trends', methods=['GET'])
@login_required
def get_spending_trends():
    days = request.args.get('days', default=90, type=int)

    if days <= 0 or days > 365:
        return error_response("days must be between 1 and 365", 400)

    account_ids = user_account_ids(current_user.id)
    if not account_ids:
        return jsonify({"days": days, "trends": []})

    query_days = days + 7
    query = text("""
        SELECT d.date, SUM(f.amount) as daily_total
        FROM fact_transactions f
        JOIN dim_date d ON f.date_id = d.date_id
        WHERE f.transaction_type = 'DEBIT' AND f.account_id = ANY(:account_ids)
        GROUP BY d.date
        ORDER BY d.date DESC
        LIMIT :limit
    """)
    results = g.db.execute(query, {"limit": query_days, "account_ids": account_ids}).fetchall()
    
    if not results:
        return jsonify({"days": days, "trends": []})
        
    df = pd.DataFrame(results, columns=['date', 'amount'])
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').set_index('date')
    
    full_date_range = pd.date_range(start=df.index.min(), end=df.index.max())
    df = df.reindex(full_date_range, fill_value=0)
    
    df['rolling_7d'] = df['amount'].rolling(window=7, min_periods=1).mean()
    
    df = df.tail(days)
    
    trends = []
    for date, row in df.iterrows():
        trends.append({
            "date": str(date.date()),
            "amount": float(row['amount']),
            "rolling_7d": float(row['rolling_7d']) if not pd.isna(row['rolling_7d']) else None
        })
        
    return jsonify({
        "days": days,
        "trends": trends
    })
