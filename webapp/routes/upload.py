import json
import uuid

import pandas as pd
from flask import Blueprint, jsonify, request, g
from flask_login import login_required, current_user
from sqlalchemy import text

from etl.extract.csv_mapping import FIELDS, MappingError, apply_mapping, estimate_opening_balance, read_csv_bytes, suggest_mapping
from etl.transform.clean import categorize_transactions, compute_idempotency_key, merchant_key, process_pipeline
from ml.fraud_model import predict_fraud
from etl.load.db import load_data
from logging_config import logger
from webapp.errors import error_response
from webapp.services import statement_account

upload_bp = Blueprint('upload', __name__, url_prefix='/api/v1/transactions')

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

def _read_uploaded_csv():
    """Validate the multipart upload. Returns (raw_df, None) or (None, error_response)."""
    if 'file' not in request.files:
        return None, error_response("No file part in the request", 400)

    file = request.files['file']
    if file.filename == '':
        return None, error_response("No file selected", 400)
    if not file.filename.lower().endswith('.csv'):
        return None, error_response("Only CSV files are supported.", 400)

    contents = file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        return None, error_response(f"File too large. Maximum size is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.", 400)

    try:
        return read_csv_bytes(contents), None
    except MappingError as e:
        return None, error_response(str(e), 400)


def _requested_mapping(raw_df):
    """The user's reviewed mapping if the client sent one, else our suggestion."""
    suggested = suggest_mapping(raw_df.columns)
    supplied = request.form.get('mapping')
    if not supplied:
        return suggested, suggested, None
    try:
        parsed = json.loads(supplied)
        if not isinstance(parsed, dict) or not set(parsed) <= set(FIELDS):
            raise ValueError
        if not all(v is None or isinstance(v, str) for v in parsed.values()):
            raise ValueError
    except ValueError:
        return suggested, suggested, error_response("mapping must be a JSON object of field -> column name.", 400)
    return suggested, {field: parsed.get(field) for field in FIELDS}, None


def _apply_user_rules(df, user_id):
    """Apply this user's "always categorize X as Y" rules over the default categorization."""
    rules = g.db.execute(
        text("SELECT merchant_id, category_id FROM category_rule WHERE user_id = :uid"), {"uid": user_id}
    ).fetchall()
    if rules:
        # Rules are stored by merchant identity (see merchant_key), so key both sides the same way.
        rule_map = {merchant_key(r[0]): r[1] for r in rules}
        df['category_id'] = df.apply(lambda row: rule_map.get(merchant_key(row['merchant_id']), row['category_id']), axis=1)
    return df


def _requested_opening_balance():
    """(value, error_response): the opening balance the user typed in, if any."""
    raw = (request.form.get('opening_balance') or '').strip()
    if not raw:
        return None, None
    try:
        return float(raw.replace(',', '')), None
    except ValueError:
        return None, error_response("opening_balance must be a number.", 400)


def _account_plan(std_df):
    """Which account this import lands in, and whether this file can set its opening balance."""
    existing = statement_account.find_statement_account(g.db, current_user.id)
    if existing is None:
        return {"account_id": None, "is_new": True, "opening_applies": True}
    account_id = existing[0]
    earliest = statement_account.earliest_transaction_date(g.db, account_id)
    # The opening balance is the balance before the account's EARLIEST transaction,
    # so a file only gets to (re)set it if it starts at or before what's already there.
    return {"account_id": account_id, "is_new": False,
            "opening_applies": earliest is None or std_df['date'].min().date() <= earliest}


def _opening_info(std_df, plan, provided):
    if not plan["opening_applies"]:
        return {"value": None, "source": "existing_account", "editable": False}
    if provided is not None:
        return {"value": provided, "source": "user", "editable": True}
    estimated = estimate_opening_balance(std_df)
    if estimated is not None:
        return {"value": estimated, "source": "statement", "editable": False}
    return {"value": None, "source": "unknown", "editable": True}


def _already_imported(std_df, account_id):
    """(importable_rows, ids_already_in_the_database) for this file against the user's imported account."""
    importable = std_df[std_df['date'] <= pd.Timestamp.now()].copy()
    importable['account_id'] = account_id
    keys = compute_idempotency_key(importable)
    existing = statement_account.existing_transaction_ids(g.db, keys)
    return len(importable), existing


@upload_bp.route('/upload/preview', methods=['POST'])
@login_required
def preview_upload():
    """Nothing is saved here: shows how the file would be read (columns, a few
    rows, auto-assigned categories) so the user can fix the column mapping first."""
    raw_df, err = _read_uploaded_csv()
    if err:
        return err
    suggested, mapping, err = _requested_mapping(raw_df)
    if err:
        return err

    response = {
        "columns": list(raw_df.columns),
        "row_count": int(len(raw_df)),
        "sample_rows": raw_df.head(3).fillna('').astype(str).to_dict(orient='records'),  # blank cells -> "" (NaN is not valid JSON)
        "suggested_mapping": suggested,
        "mapping": mapping,
        "ready": False,
        "error": None,
        "issues": [],
        "preview": [],
        "category_counts": {},
        "summary": None,
        "account": None,
        "opening_balance": None,
    }

    try:
        std_df, issues = apply_mapping(raw_df, mapping)
    except MappingError as e:
        response["error"] = str(e)
        return jsonify(response)

    provided_opening, err = _requested_opening_balance()
    if err:
        return err

    std_df = categorize_transactions(std_df)
    std_df = _apply_user_rules(std_df, current_user.id)

    plan = _account_plan(std_df)
    opening = _opening_info(std_df, plan, provided_opening)
    importable, already = (len(std_df), set())
    if plan["account_id"]:
        importable, already = _already_imported(std_df, plan["account_id"])

    debits = std_df[std_df['transaction_type'] == 'DEBIT']
    credits = std_df[std_df['transaction_type'] == 'CREDIT']
    response.update({
        "ready": True,
        "issues": issues,
        "preview": [
            {
                "date": row['date'].strftime('%Y-%m-%d'),
                "description": row['merchant_id'],
                "amount": round(float(row['amount']), 2),
                "transaction_type": row['transaction_type'],
                "category_id": row['category_id'],
            }
            for _, row in std_df.head(8).iterrows()
        ],
        "category_counts": {k: int(v) for k, v in std_df['category_id'].value_counts().items()},
        "summary": {
            "rows": int(len(std_df)),
            "debit_count": int(len(debits)),
            "credit_count": int(len(credits)),
            "total_debit": round(float(debits['amount'].sum()), 2),
            "total_credit": round(float(credits['amount'].sum()), 2),
            "date_from": std_df['date'].min().strftime('%Y-%m-%d'),
            "date_to": std_df['date'].max().strftime('%Y-%m-%d'),
            "already_imported": len(already),
            "new_rows": importable - len(already),
        },
        "account": {"is_new": plan["is_new"]},
        "opening_balance": opening,
    })
    return jsonify(response)


@upload_bp.route('/upload', methods=['POST'])
@login_required
def upload_transactions():
    raw_df, err = _read_uploaded_csv()
    if err:
        return err
    _, mapping, err = _requested_mapping(raw_df)
    if err:
        return err

    try:
        df, issues = apply_mapping(raw_df, mapping)
    except MappingError as e:
        return error_response(str(e), 400)

    provided_opening, err = _requested_opening_balance()
    if err:
        return err

    plan = _account_plan(df)
    opening = _opening_info(df, plan, provided_opening)
    account_id = plan["account_id"] or str(uuid.uuid4())

    df['transaction_id'] = [str(uuid.uuid4()) for _ in range(len(df))]
    df['account_id'] = account_id

    try:
        df_clean = process_pipeline(df)
    except Exception:
        logger.exception("Could not process uploaded statement")
        return error_response("We couldn't process this file. Check the column mapping and try again.", 400)

    # De-duplicate against what's already imported: re-uploading a statement (or
    # one that overlaps an earlier one) adds only the rows that are actually new.
    already = statement_account.existing_transaction_ids(g.db, df_clean['transaction_id'])
    new_rows = df_clean[~df_clean['transaction_id'].isin(already)].copy()

    new_rows = _apply_user_rules(new_rows, current_user.id) if len(new_rows) else new_rows

    # Model output goes in fraud_score/scored_at. is_fraud is the ground-truth label the models are
    # evaluated against, so an import must never write the model's own flags into it.
    flagged_count = 0
    new_rows['is_fraud'] = False
    new_rows['fraud_score'] = None
    new_rows['scored_at'] = None
    if len(new_rows):
        try:
            scored = predict_fraud(new_rows)
            new_rows['fraud_score'] = scored['fraud_probability'].round(4)
            new_rows['scored_at'] = pd.Timestamp.now()
            flagged_count = int(scored['is_flagged'].sum())
        except FileNotFoundError:
            pass

    try:
        if plan["is_new"]:
            load_data(pd.DataFrame({
                'account_id': [account_id],
                'user_id': [current_user.id],
                'account_type': ['CHECKING'],
                'institution': [statement_account.STATEMENT_INSTITUTION],
                'account_number': [str(uuid.uuid4())[:8]],
                'credit_limit': [None],
                'opened_date': [pd.Timestamp.now().date()],
                'opening_balance': [opening["value"] if opening["value"] is not None else 0.0],
            }), 'dim_account', conflict_columns=['account_id'])
        elif opening["value"] is not None:
            statement_account.set_opening_balance(g.db, account_id, opening["value"])

        if len(new_rows):
            # One row per merchant_id: shortened long descriptions can collide, and an upsert can't
            # touch the same key twice in one batch. Shared reference data (merchants, categories) is
            # only ever added to here, never overwritten by one user's upload.
            unique_merchants = new_rows[['merchant_id', 'category_id']].drop_duplicates(subset='merchant_id')
            unique_merchants['merchant_name'] = unique_merchants['merchant_id']
            unique_merchants['mcc_code'] = '0000'
            unique_merchants = unique_merchants.rename(columns={'category_id': 'category'})

            unique_categories = pd.DataFrame({
                'category_id': unique_merchants['category'].unique(),
                'category_name': unique_merchants['category'].unique(),
                'budget_amount': 10000
            })
            load_data(unique_categories, 'dim_category', conflict_columns=['category_id'], update_on_conflict=False)
            load_data(unique_merchants, 'dim_merchant', conflict_columns=['merchant_id'], update_on_conflict=False)

            fact_cols = ['transaction_id', 'account_id', 'merchant_id', 'category_id', 'date_id', 'transaction_ts', 'amount', 'currency', 'transaction_type', 'is_fraud', 'is_recurring', 'fraud_score', 'scored_at']
            for col in fact_cols:
                if col not in new_rows.columns:
                    new_rows[col] = False if col in ['is_fraud', 'is_recurring'] else None

            # Existing rows are left untouched (not overwritten), so a re-import can't undo
            # a category the user has since corrected.
            load_data(new_rows[fact_cols], 'fact_transactions', conflict_columns=['transaction_id'], update_on_conflict=False)

        statement_account.rebuild_balances(g.db, account_id)
    except Exception:
        logger.exception("Database error while importing a statement")
        return error_response("Something went wrong while saving your transactions. Nothing was imported; please try again.", 500)

    if not plan["opening_applies"] and (provided_opening is not None or estimate_opening_balance(df) is not None):
        issues.append({"level": "info", "message": "This file starts after transactions you've already imported, so its opening balance was not used."})

    return jsonify({
        "message": "Upload successful",
        "transactions_processed": int(len(df_clean)),
        "transactions_added": int(len(new_rows)),
        "duplicates_skipped": int(len(df_clean) - len(new_rows)),
        "account_id": account_id,
        "existing_account": not plan["is_new"],
        "opening_balance": opening["value"],
        "opening_balance_source": opening["source"],
        "fraud_alerts_generated": flagged_count,
        "issues": issues
    })
