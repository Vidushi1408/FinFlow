"""
Each user's imported bank statements all go into ONE account, so that
importing an overlapping or repeated statement de-duplicates (transaction ids
include the account) instead of creating a parallel copy of every row. Its
balance history is rebuilt from the full set of transactions plus the
account's stored opening balance whenever more are added.
"""
from sqlalchemy import text

STATEMENT_INSTITUTION = "UPLOADED_STATEMENT"


def find_statement_account(db, user_id):
    """(account_id, opening_balance) of the user's imported-statement account, or None."""
    row = db.execute(
        text("""
            SELECT account_id, opening_balance FROM dim_account
            WHERE user_id = :uid AND institution = :inst
            ORDER BY opened_date, account_id LIMIT 1
        """),
        {"uid": user_id, "inst": STATEMENT_INSTITUTION},
    ).fetchone()
    return (row[0], float(row[1])) if row else None


def earliest_transaction_date(db, account_id):
    value = db.execute(
        text("SELECT MIN(transaction_ts) FROM fact_transactions WHERE account_id = :aid"), {"aid": account_id}
    ).scalar()
    return value.date() if value is not None else None


def existing_transaction_ids(db, transaction_ids):
    ids = list(transaction_ids)
    if not ids:
        return set()
    rows = db.execute(
        text("SELECT transaction_id FROM fact_transactions WHERE transaction_id = ANY(:ids)"), {"ids": ids}
    ).fetchall()
    return {r[0] for r in rows}


def set_opening_balance(db, account_id, opening_balance):
    db.execute(
        text("UPDATE dim_account SET opening_balance = :ob WHERE account_id = :aid"),
        {"ob": opening_balance, "aid": account_id},
    )
    db.commit()


def rebuild_balances(db, account_id):
    """Replace the account's daily balance rows with ones derived from ALL its
    transactions on top of its opening balance. Atomic: readers never see a
    half-rebuilt history."""
    opening = float(db.execute(
        text("SELECT opening_balance FROM dim_account WHERE account_id = :aid"), {"aid": account_id}
    ).scalar() or 0)

    daily = db.execute(
        text("""
            SELECT date_id,
                   COALESCE(SUM(amount) FILTER (WHERE transaction_type = 'CREDIT'), 0),
                   COALESCE(SUM(amount) FILTER (WHERE transaction_type = 'DEBIT'), 0)
            FROM fact_transactions WHERE account_id = :aid
            GROUP BY date_id ORDER BY date_id
        """),
        {"aid": account_id},
    ).fetchall()

    rows, running = [], opening
    for date_id, inflow, outflow in daily:
        inflow, outflow = float(inflow), float(outflow)
        closing = running + inflow - outflow
        rows.append({"aid": account_id, "did": date_id, "open": round(running, 2), "close": round(closing, 2),
                     "inflow": round(inflow, 2), "outflow": round(outflow, 2)})
        running = closing

    try:
        db.execute(text("DELETE FROM fact_account_balance WHERE account_id = :aid"), {"aid": account_id})
        if rows:
            db.execute(
                text("""
                    INSERT INTO fact_account_balance (account_id, date_id, opening_balance, closing_balance, inflow, outflow)
                    VALUES (:aid, :did, :open, :close, :inflow, :outflow)
                """),
                rows,
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
