import csv
import io

from flask import Blueprint, Response, jsonify, request, g
from flask_login import login_required, current_user
from sqlalchemy import text
from webapp.db_utils import user_account_ids
from webapp.errors import error_response
from etl.transform.clean import merchant_key
from webapp.services.pdf_utils import pdf_inr

transactions_bp = Blueprint('transactions', __name__, url_prefix='/api/v1/transactions')


def _build_filters(account_ids):
    """WHERE clause + bound params for the ledger's filters, shared by the list and export routes.
    Returns (where_clause, params, error_response); error_response is None when the filters are valid."""
    search = request.args.get('search', default='', type=str).strip()
    category = request.args.get('category', default=None, type=str)
    account_id = request.args.get('account_id', default=None, type=str)
    tx_type = request.args.get('type', default=None, type=str)
    date_from = request.args.get('date_from', default=None, type=str)
    date_to = request.args.get('date_to', default=None, type=str)
    amount_min = request.args.get('amount_min', default=None, type=float)
    amount_max = request.args.get('amount_max', default=None, type=float)

    if tx_type and tx_type not in ('DEBIT', 'CREDIT'):
        return None, None, error_response("type must be DEBIT or CREDIT", 400)

    filters = ["f.account_id = ANY(:account_ids)"]
    params = {"account_ids": account_ids}

    if search:
        filters.append("f.merchant_id ILIKE :search")
        params["search"] = f"%{search}%"
    if category:
        filters.append("f.category_id = :category")
        params["category"] = category
    if account_id:
        if account_id not in account_ids:
            return None, None, error_response("Unknown account", 400)
        filters.append("f.account_id = :account_id")
        params["account_id"] = account_id
    if tx_type:
        filters.append("f.transaction_type = :tx_type")
        params["tx_type"] = tx_type
    if date_from:
        filters.append("f.transaction_ts >= :date_from")
        params["date_from"] = date_from
    if date_to:
        filters.append("f.transaction_ts <= :date_to")
        params["date_to"] = date_to
    if amount_min is not None:
        filters.append("f.amount >= :amount_min")
        params["amount_min"] = amount_min
    if amount_max is not None:
        filters.append("f.amount <= :amount_max")
        params["amount_max"] = amount_max

    # `filters` only ever holds fixed string literals appended above (never anything built from
    # `search`/`category`/etc directly) -- every actual value flows through `params` as a bound
    # :placeholder. See tests/test_integration_routes.py for injection-attempt tests against this.
    return " AND ".join(filters), params, None


@transactions_bp.route('', methods=['GET'])
@login_required
def list_transactions():
    account_ids = user_account_ids(current_user.id)
    if not account_ids:
        return jsonify({"total": 0, "page": 1, "page_size": 0, "transactions": []})

    page = max(request.args.get('page', default=1, type=int), 1)
    page_size = min(max(request.args.get('page_size', default=25, type=int), 1), 200)

    where_clause, params, err = _build_filters(account_ids)
    if err:
        return err

    count_sql = f"SELECT COUNT(*) FROM fact_transactions f WHERE {where_clause}"  # nosec B608 - see comment above
    total = g.db.execute(text(count_sql), params).scalar()

    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size

    select_sql = f"""
        SELECT f.transaction_id, f.account_id, f.merchant_id, f.category_id,
               f.amount, f.transaction_type, f.transaction_ts, f.is_fraud, f.is_recurring, f.fraud_score
        FROM fact_transactions f
        WHERE {where_clause}
        ORDER BY f.transaction_ts DESC
        LIMIT :limit OFFSET :offset
    """  # nosec B608 - see comment above
    rows = g.db.execute(text(select_sql), params).fetchall()

    transactions = [{
        "transaction_id": r[0],
        "account_id": r[1],
        "merchant_id": r[2],
        "category_id": r[3],
        "amount": float(r[4]),
        "transaction_type": r[5],
        "transaction_ts": str(r[6]),
        "is_fraud": bool(r[7]),
        "is_recurring": bool(r[8]),
        "fraud_score": float(r[9]) if r[9] is not None else None,
        # Shown as "flagged": scored at or above this user's alert sensitivity (the same rule the
        # Alerts page uses), or carrying a ground-truth fraud label (generated data).
        "is_flagged": bool(r[7]) or (r[9] is not None and float(r[9]) >= current_user.alert_sensitivity)
    } for r in rows]

    return jsonify({
        "total": total,
        "page": page,
        "page_size": page_size,
        "transactions": transactions
    })


@transactions_bp.route('/unknown-payees', methods=['GET'])
@login_required
def unknown_payees():
    """The payees behind the most still-generic 'UPI' spending, biggest first, each with one sample
    transaction id so the UI can categorize the whole payee in a single step (PATCH with create_rule)."""
    account_ids = user_account_ids(current_user.id)
    if not account_ids:
        return jsonify({"payees": []})
    rows = g.db.execute(
        text("""
            SELECT merchant_id, SUM(amount), COUNT(*), (ARRAY_AGG(transaction_id ORDER BY transaction_ts DESC))[1]
            FROM fact_transactions
            WHERE account_id = ANY(:aids) AND category_id = 'UPI' AND transaction_type = 'DEBIT'
            GROUP BY merchant_id ORDER BY SUM(amount) DESC LIMIT 5
        """), {"aids": account_ids}
    ).fetchall()
    return jsonify({"payees": [
        {"merchant_id": r[0], "total_spent": float(r[1]), "transaction_count": int(r[2]), "sample_transaction_id": r[3]}
        for r in rows
    ]})


@transactions_bp.route('/<transaction_id>/category', methods=['PATCH'])
@login_required
def update_transaction_category(transaction_id):
    req = request.get_json(silent=True) or {}
    category_id = req.get('category_id')
    create_rule = bool(req.get('create_rule', False))

    if not category_id:
        return error_response("category_id is required", 400)

    valid_category = g.db.execute(
        text("SELECT 1 FROM dim_category WHERE category_id = :cat"), {"cat": category_id}
    ).fetchone()
    if not valid_category:
        return error_response(f"Unknown category: {category_id}", 400)

    account_ids = user_account_ids(current_user.id)
    tx = g.db.execute(
        text("SELECT merchant_id FROM fact_transactions WHERE transaction_id = :tid AND account_id = ANY(:account_ids)"),
        {"tid": transaction_id, "account_ids": account_ids}
    ).fetchone()
    if not tx:
        return error_response("Transaction not found", 404)

    g.db.execute(
        text("UPDATE fact_transactions SET category_id = :cat WHERE transaction_id = :tid"),
        {"cat": category_id, "tid": transaction_id}
    )

    similar_updated = 0
    if create_rule:
        # The rule is keyed on the merchant's identity (the payee for a UPI payment, the
        # description minus reference numbers otherwise), not the raw text, so it covers
        # every payment to them -- not just this one narration with its unique ref number.
        key = merchant_key(tx[0])
        g.db.execute(
            text("""
                INSERT INTO category_rule (user_id, merchant_id, category_id)
                VALUES (:uid, :merchant_id, :cat)
                ON CONFLICT (user_id, merchant_id)
                DO UPDATE SET category_id = EXCLUDED.category_id
            """),
            {"uid": current_user.id, "merchant_id": key, "cat": category_id}
        )

        distinct_merchants = g.db.execute(
            text("SELECT DISTINCT merchant_id FROM fact_transactions WHERE account_id = ANY(:account_ids)"),
            {"account_ids": account_ids}
        ).fetchall()
        matching = [r[0] for r in distinct_merchants if merchant_key(r[0]) == key]
        if matching:
            result = g.db.execute(
                text("UPDATE fact_transactions SET category_id = :cat WHERE account_id = ANY(:account_ids) AND merchant_id = ANY(:matching)"),
                {"cat": category_id, "account_ids": account_ids, "matching": matching}
            )
            similar_updated = result.rowcount or 0

    g.db.commit()
    return jsonify({
        "transaction_id": transaction_id, "category_id": category_id,
        "rule_created": create_rule, "transactions_updated": similar_updated if create_rule else 1
    })


def _csv_safe(value):
    """Stop spreadsheet apps from running a merchant name like '=HYPERLINK(...)' as a formula."""
    value = "" if value is None else str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


@transactions_bp.route('/export.csv', methods=['GET'])
@login_required
def export_transactions_csv():
    """The whole filtered ledger (not just one page) as CSV, scoped to the caller's own accounts."""
    account_ids = user_account_ids(current_user.id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["date", "account", "merchant", "category", "type", "amount", "recurring"])

    if account_ids:
        where_clause, params, err = _build_filters(account_ids)
        if err:
            return err
        export_sql = f"""
            SELECT f.transaction_ts, f.account_id, f.merchant_id, f.category_id,
                   f.transaction_type, f.amount, f.is_recurring
            FROM fact_transactions f
            WHERE {where_clause}
            ORDER BY f.transaction_ts DESC
        """  # nosec B608 - where_clause is built only from fixed literals, see _build_filters
        for r in g.db.execute(text(export_sql), params):
            writer.writerow([r[0], _csv_safe(r[1]), _csv_safe(r[2]), _csv_safe(r[3]), r[4], float(r[5]), bool(r[6])])

    return Response(
        output.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=finflow-transactions.csv'}
    )


PDF_MAX_ROWS = 5000


def _pdf_text(value, limit=38):
    """Plain, base-font-safe text for a PDF cell: characters the built-in fonts can't draw become '?'
    instead of black squares, and long merchant names are cut."""
    text_value = "" if value is None else str(value)
    return text_value.encode("latin-1", "replace").decode("latin-1")[:limit]


@transactions_bp.route('/export.pdf', methods=['GET'])
@login_required
def export_transactions_pdf():
    """The filtered ledger as a PDF (newest first, capped at PDF_MAX_ROWS rows; use the CSV for more)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    account_ids = user_account_ids(current_user.id)
    rows, total = [], 0
    if account_ids:
        where_clause, params, err = _build_filters(account_ids)
        if err:
            return err
        total = g.db.execute(text(f"SELECT COUNT(*) FROM fact_transactions f WHERE {where_clause}"), params).scalar()  # nosec B608 - see _build_filters
        params["limit"] = PDF_MAX_ROWS
        rows = g.db.execute(text(f"""
            SELECT f.transaction_ts, f.merchant_id, f.category_id, f.transaction_type, f.amount
            FROM fact_transactions f WHERE {where_clause}
            ORDER BY f.transaction_ts DESC LIMIT :limit
        """), params).fetchall()  # nosec B608 - see _build_filters

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, title="FinFlow transactions")
    styles = getSampleStyleSheet()
    shown = len(rows)
    note = f"{shown} of {total} transactions" + (f" (capped at {PDF_MAX_ROWS}; export the CSV for the rest)" if total > shown else "")
    elements = [Paragraph("FinFlow Transactions", styles['Title']), Paragraph(_pdf_text(note, 120), styles['BodyText']), Spacer(1, 10)]
    if rows:
        data = [["Date", "Merchant", "Category", "Type", "Amount"]] + [
            [str(r[0])[:10], _pdf_text(r[1]), _pdf_text(r[2], 18), r[3], pdf_inr(r[4])] for r in rows
        ]
        table = Table(data, repeatRows=1, hAlign='LEFT', colWidths=[62, 190, 90, 42, 80])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3b82f6')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('ALIGN', (-1, 0), (-1, -1), 'RIGHT'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f3f4f6')]),
            ('GRID', (0, 0), (-1, -1), 0.25, colors.grey),
        ]))
        elements.append(table)
    else:
        elements.append(Paragraph("No transactions match these filters.", styles['BodyText']))
    doc.build(elements)

    return Response(
        buffer.getvalue(), mimetype='application/pdf',
        headers={'Content-Disposition': 'attachment; filename=finflow-transactions.pdf'}
    )
