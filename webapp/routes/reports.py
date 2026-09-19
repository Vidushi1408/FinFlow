import io
import csv
from datetime import date, datetime

from flask import Blueprint, jsonify, request, g, Response
from flask_login import login_required, current_user
from sqlalchemy import text

from webapp.db_utils import user_account_ids
from webapp.errors import error_response
from etl.transform.clean import summarize_recurring_series
from ml.fraud_model import predict_fraud
import pandas as pd

reports_bp = Blueprint('reports', __name__, url_prefix='/api/v1/reports')


def _parse_month(month_str):
    try:
        year, m = month_str.split('-')
        return int(year), int(m)
    except (ValueError, AttributeError):
        return None, None


def _prev_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _build_monthly_report(user_id, year, month):
    account_ids = user_account_ids(user_id)
    if not account_ids:
        return None

    def month_totals(y, m):
        row = g.db.execute(
            text("""
                SELECT f.category_id, SUM(f.amount)
                FROM fact_transactions f
                JOIN dim_date d ON f.date_id = d.date_id
                WHERE f.account_id = ANY(:aids) AND f.transaction_type = 'DEBIT'
                  AND d.year = :year AND d.month = :month
                GROUP BY f.category_id ORDER BY SUM(f.amount) DESC
            """), {"aids": account_ids, "year": y, "month": m}
        ).fetchall()
        return [(r[0], float(r[1])) for r in row]

    this_month = month_totals(year, month)
    prev_year, prev_month = _prev_month(year, month)
    last_month = month_totals(prev_year, prev_month)

    total_this = sum(a for _, a in this_month)
    total_last = sum(a for _, a in last_month)
    pct_change = ((total_this - total_last) / total_last * 100) if total_last > 0 else None

    biggest_category = this_month[0] if this_month else None

    # Subscriptions active this period
    tx_df = pd.read_sql(
        text("""
            SELECT transaction_id, account_id, merchant_id, amount, transaction_ts as date
            FROM fact_transactions
            WHERE account_id = ANY(:aids) AND transaction_type = 'DEBIT'
        """), g.db.connection(), params={"aids": account_ids}
    )
    subscription_total = 0.0
    subscription_count = 0
    if not tx_df.empty:
        tx_df['date'] = pd.to_datetime(tx_df['date'])
        recurring = summarize_recurring_series(tx_df)
        subscription_count = len(recurring)
        subscription_total = float(recurring['annualized_cost'].sum()) if not recurring.empty else 0.0

    # Alerts this period
    alert_count = 0
    try:
        recent_df = pd.read_sql(
            text("""
                SELECT transaction_id, merchant_id, amount, transaction_type, transaction_ts
                FROM fact_transactions f
                JOIN dim_date d ON f.date_id = d.date_id
                WHERE f.account_id = ANY(:aids) AND f.transaction_type = 'DEBIT'
                  AND d.year = :year AND d.month = :month
            """), g.db.connection(), params={"aids": account_ids, "year": year, "month": month}
        )
        if not recent_df.empty:
            scored = predict_fraud(recent_df)
            alert_count = int(scored['is_flagged'].sum())
    except FileNotFoundError:
        pass

    # Goal progress
    goal_rows = g.db.execute(
        text("SELECT name, target_amount, deadline FROM goal WHERE user_id = :uid"),
        {"uid": user_id}
    ).fetchall()
    goals = []
    for name, target, deadline in goal_rows:
        days_left = max((deadline - date.today()).days, 0)
        goals.append({
            "name": name,
            "target_amount": float(target),
            "deadline": str(deadline),
            "days_left": days_left
        })

    return {
        "month": f"{year}-{month:02d}",
        "total_spent": round(total_this, 2),
        "total_spent_last_month": round(total_last, 2),
        "pct_change_vs_last_month": round(pct_change, 1) if pct_change is not None else None,
        "biggest_category": {"category_id": biggest_category[0], "amount": biggest_category[1]} if biggest_category else None,
        "categories": [{"category_id": c, "amount": a} for c, a in this_month],
        "subscription_count": subscription_count,
        "subscription_annual_cost": round(subscription_total, 2),
        "alert_count": alert_count,
        "goals": goals
    }


@reports_bp.route('/monthly', methods=['GET'])
@login_required
def monthly_report():
    month_str = request.args.get('month', datetime.now().strftime('%Y-%m'))
    year, month = _parse_month(month_str)
    if year is None:
        return error_response("month must be YYYY-MM", 400)

    report = _build_monthly_report(current_user.id, year, month)
    if report is None:
        return jsonify({"month": month_str, "total_spent": 0, "categories": [], "goals": [], "message": "No accounts yet."})

    return jsonify(report)


@reports_bp.route('/monthly/export.csv', methods=['GET'])
@login_required
def export_csv():
    month_str = request.args.get('month', datetime.now().strftime('%Y-%m'))
    year, month = _parse_month(month_str)
    if year is None:
        return error_response("month must be YYYY-MM", 400)

    account_ids = user_account_ids(current_user.id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["date", "merchant", "category", "type", "amount"])

    if account_ids:
        rows = g.db.execute(
            text("""
                SELECT f.transaction_ts, f.merchant_id, f.category_id, f.transaction_type, f.amount
                FROM fact_transactions f
                JOIN dim_date d ON f.date_id = d.date_id
                WHERE f.account_id = ANY(:aids) AND d.year = :year AND d.month = :month
                ORDER BY f.transaction_ts
            """), {"aids": account_ids, "year": year, "month": month}
        ).fetchall()
        for r in rows:
            writer.writerow([r[0], r[1], r[2], r[3], float(r[4])])

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=finflow-report-{month_str}.csv'}
    )


@reports_bp.route('/monthly/export.pdf', methods=['GET'])
@login_required
def export_pdf():
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet

    month_str = request.args.get('month', datetime.now().strftime('%Y-%m'))
    year, month = _parse_month(month_str)
    if year is None:
        return error_response("month must be YYYY-MM", 400)

    report = _build_monthly_report(current_user.id, year, month) or {
        "month": month_str, "total_spent": 0, "total_spent_last_month": 0,
        "pct_change_vs_last_month": None, "biggest_category": None,
        "categories": [], "subscription_count": 0, "subscription_annual_cost": 0,
        "alert_count": 0, "goals": []
    }

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = [Paragraph(f"FinFlow Monthly Report &mdash; {report['month']}", styles['Title']), Spacer(1, 12)]

    change_text = (
        f"{'up' if report['pct_change_vs_last_month'] and report['pct_change_vs_last_month'] > 0 else 'down'} "
        f"{abs(report['pct_change_vs_last_month'])}% vs last month"
        if report['pct_change_vs_last_month'] is not None else "no data for last month"
    )
    sub_count = report['subscription_count']
    alert_count = report['alert_count']
    summary_text = (
        f"You spent ₹{report['total_spent']:,.2f} this month ({change_text}). "
        + (f"Your biggest category was {report['biggest_category']['category_id']} "
           f"at ₹{report['biggest_category']['amount']:,.2f}. " if report['biggest_category'] else "")
        + f"You have {sub_count} active subscription{'' if sub_count == 1 else 's'} costing "
        f"₹{report['subscription_annual_cost']:,.2f}/year, and {alert_count} alert{'' if alert_count == 1 else 's'} this month."
    )
    elements.append(Paragraph(summary_text, styles['BodyText']))
    elements.append(Spacer(1, 16))

    if report['categories']:
        data = [["Category", "Amount"]] + [[c['category_id'], f"₹{c['amount']:,.2f}"] for c in report['categories']]
        table = Table(data, hAlign='LEFT')
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3b82f6')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ]))
        elements.append(table)
        elements.append(Spacer(1, 16))

    if report['goals']:
        elements.append(Paragraph("Savings Goals", styles['Heading2']))
        goal_data = [["Goal", "Target", "Deadline", "Days left"]] + [
            [g['name'], f"₹{g['target_amount']:,.2f}", g['deadline'], str(g['days_left'])] for g in report['goals']
        ]
        goal_table = Table(goal_data, hAlign='LEFT')
        goal_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#8b5cf6')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ]))
        elements.append(goal_table)

    doc.build(elements)
    buffer.seek(0)

    return Response(
        buffer.read(),
        mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename=finflow-report-{month_str}.pdf'}
    )
