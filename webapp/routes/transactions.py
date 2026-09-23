from flask import Blueprint, jsonify, request, g
from flask_login import login_required, current_user
from sqlalchemy import text
from webapp.db_utils import user_account_ids
from webapp.errors import error_response
from etl.transform.clean import merchant_key

transactions_bp = Blueprint('transactions', __name__, url_prefix='/api/v1/transactions')


@transactions_bp.route('', methods=['GET'])
@login_required
def list_transactions():
    account_ids = user_account_ids(current_user.id)
    if not account_ids:
        return jsonify({"total": 0, "page": 1, "page_size": 0, "transactions": []})

    page = max(request.args.get('page', default=1, type=int), 1)
    page_size = min(max(request.args.get('page_size', default=25, type=int), 1), 200)
    search = request.args.get('search', default='', type=str).strip()
    category = request.args.get('category', default=None, type=str)
    account_id = request.args.get('account_id', default=None, type=str)
    tx_type = request.args.get('type', default=None, type=str)
    date_from = request.args.get('date_from', default=None, type=str)
    date_to = request.args.get('date_to', default=None, type=str)
    amount_min = request.args.get('amount_min', default=None, type=float)
    amount_max = request.args.get('amount_max', default=None, type=float)

    if tx_type and tx_type not in ('DEBIT', 'CREDIT'):
        return error_response("type must be DEBIT or CREDIT", 400)

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
            return error_response("Unknown account", 400)
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
    where_clause = " AND ".join(filters)

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
