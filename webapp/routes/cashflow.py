from flask import Blueprint, jsonify, g
from flask_login import login_required, current_user
from sqlalchemy import text
import pandas as pd
from ml.cashflow_model import predict_cashflow, load_cashflow_metadata
from webapp.db_utils import user_account_ids

cashflow_bp = Blueprint('cashflow', __name__, url_prefix='/api/v1/cash-flow')

@cashflow_bp.route('/forecast', methods=['GET'])
@login_required
def get_cashflow_forecast():
    account_ids = user_account_ids(current_user.id)
    if not account_ids:
        return jsonify({"forecast_30_days_total": 0, "daily_forecasts": []})

    query = text("""
        SELECT d.date_id, SUM(f.amount) as amount
        FROM fact_transactions f
        JOIN dim_date d ON f.date_id = d.date_id
        WHERE f.transaction_type = 'DEBIT' AND f.account_id = ANY(:account_ids)
        GROUP BY d.date_id
        ORDER BY d.date_id
    """)
    df = pd.read_sql(query, g.db.connection(), params={"account_ids": account_ids})
    
    if df.empty:
        return jsonify({
            "forecast_30_days_total": 0,
            "daily_forecasts": []
        })
        
    try:
        predictions = predict_cashflow(df, days=30)
        metadata = load_cashflow_metadata()
    except FileNotFoundError:
        return jsonify({
            "forecast_30_days_total": 0,
            "daily_forecasts": []
        })

    total_forecast = sum(p['predicted_amount'] for p in predictions)

    daily = []
    for p in predictions:
        daily.append({
            "date": str(p['date']),
            "predicted_amount": float(p['predicted_amount']),
            "lower_bound": float(p['lower_bound']),
            "upper_bound": float(p['upper_bound'])
        })

    return jsonify({
        "forecast_30_days_total": float(total_forecast),
        "daily_forecasts": daily,
        "strategy": metadata.get('selected_strategy', 'model'),
        "backtest_mae": metadata.get('metrics', {}).get(
            {'model': 'model_mae', 'same_period_last_month': 'baseline_last_month_mae',
             'trailing_30d_avg': 'baseline_trailing_avg_mae'}.get(metadata.get('selected_strategy'), 'model_mae')
        )
    })
