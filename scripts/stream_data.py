"""
Live data source: continuously generates new, realistic transactions for
existing accounts and ingests them, so the running app has something moving
to look at instead of a static seeded snapshot.

Run manually (not part of `docker compose up`):

    python scripts/stream_data.py                # every 60 min, forever
    STREAM_INTERVAL_MINUTES=5 python scripts/stream_data.py
    python scripts/stream_data.py --once          # single batch, then exit

Each cycle: pick a handful of existing generated/demo accounts (never a user's
own imported statement account), generate 1-3 small transactions per account
dated "now", run them through the same categorization + idempotency-key logic
the CSV-upload path uses, and insert them. Then, so the new rows behave like
any other data in the app:

  * balances: today's fact_account_balance row for each affected account is
    updated so the dashboard reflects the new balances immediately;
  * fraud scoring: each new row gets a fraud_score (the calibrated 0-1 anomaly
    probability) and scored_at. is_fraud is deliberately left alone: it is the
    ground-truth label the models are evaluated against, and writing model
    output into it would make that evaluation circular;
  * recurring flags: is_recurring is re-derived from each affected account's
    FULL history (cadence and amount stability can't be judged from 1-3 fresh
    rows) and any flag that changed is updated, including on older rows.
"""
import argparse
import os
import random
import sys
import time
import uuid
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from psycopg2.extras import execute_values

from logging_config import logger
from etl.load.db import get_connection, load_data
from etl.transform.clean import categorize_transactions, compute_idempotency_key, detect_recurring_transactions
from ml.fraud_model import predict_fraud
from data_generator.generate_mock_data import MERCHANTS

DEFAULT_INTERVAL_MINUTES = int(os.environ.get("STREAM_INTERVAL_MINUTES", "60"))
ACCOUNTS_PER_CYCLE = int(os.environ.get("STREAM_ACCOUNTS_PER_CYCLE", "5"))
TX_PER_ACCOUNT_RANGE = (1, 3)
SPEND_MERCHANTS = [m for m in MERCHANTS if not m.get("is_recurring")]


def _fetch_account_ids():
    """Accounts the stream may write to. Accounts made from a user's own imported bank
    statements are excluded: synthetic transactions must never be mixed into real data."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT account_id FROM dim_account WHERE institution <> 'UPLOADED_STATEMENT'")
        return [r[0] for r in cursor.fetchall()]
    finally:
        conn.close()


def _latest_balances(account_ids):
    """account_id -> most recent closing_balance, for accounts that have one."""
    if not account_ids:
        return {}
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT ON (account_id) account_id, closing_balance
            FROM fact_account_balance
            WHERE account_id = ANY(%s)
            ORDER BY account_id, date_id DESC
            """,
            (account_ids,)
        )
        return {r[0]: float(r[1]) for r in cursor.fetchall()}
    finally:
        conn.close()


def _ensure_date_id(now):
    """dim_date is pre-seeded through 2030-12-31; back it up with an
    on-demand insert if a fresh install has a narrower range."""
    date_id = int(now.strftime("%Y%m%d"))
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM dim_date WHERE date_id = %s", (date_id,))
        if cursor.fetchone() is None:
            cursor.execute(
                """
                INSERT INTO dim_date (date_id, date, year, month, quarter, day_of_week, is_holiday)
                VALUES (%s, %s, %s, %s, %s, %s, FALSE)
                ON CONFLICT (date_id) DO NOTHING
                """,
                (date_id, now.date(), now.year, now.month, (now.month - 1) // 3 + 1, now.weekday())
            )
            conn.commit()
    finally:
        conn.close()
    return date_id


def generate_batch(account_ids, now):
    """One small, mostly-DEBIT batch of transactions dated `now` for a random
    subset of accounts -- simulates real-time card swipes/UPI payments."""
    sample_size = min(ACCOUNTS_PER_CYCLE, len(account_ids))
    chosen_accounts = random.sample(account_ids, sample_size)

    rows = []
    for account_id in chosen_accounts:
        for _ in range(random.randint(*TX_PER_ACCOUNT_RANGE)):
            merchant = random.choice(SPEND_MERCHANTS)
            is_credit = random.random() < 0.05  # occasional refund/transfer-in
            amount = round(random.uniform(500.0, 4000.0) if is_credit else random.uniform(20.0, 3000.0), 2)
            rows.append({
                "transaction_id": str(uuid.uuid4()),
                "account_id": account_id,
                "merchant_id": ("Transfer" if is_credit else merchant["name"]).replace(" ", "_").upper(),
                "category_id": "TRANSFER" if is_credit else merchant["category"],
                "date": now.strftime("%Y-%m-%d %H:%M:%S"),
                "amount": amount,
                "transaction_type": "CREDIT" if is_credit else "DEBIT",
                "is_fraud": False,
                "is_recurring": False,
            })

    return pd.DataFrame(rows)


def _upsert_balances(balance_rows):
    """
    Add this batch to each account's balance row for today. If today's row
    already exists (an earlier batch the same day), keep its opening balance
    and accumulate inflow/outflow/closing -- a plain overwrite would make the
    row describe only the most recent batch.
    """
    if not balance_rows:
        return
    conn = get_connection()
    try:
        cursor = conn.cursor()
        execute_values(
            cursor,
            """
            INSERT INTO fact_account_balance (account_id, date_id, opening_balance, closing_balance, inflow, outflow)
            VALUES %s
            ON CONFLICT (account_id, date_id) DO UPDATE SET
                inflow = fact_account_balance.inflow + EXCLUDED.inflow,
                outflow = fact_account_balance.outflow + EXCLUDED.outflow,
                closing_balance = fact_account_balance.closing_balance + EXCLUDED.inflow - EXCLUDED.outflow
            """,
            [(r["account_id"], r["date_id"], r["opening_balance"], r["closing_balance"], r["inflow"], r["outflow"]) for r in balance_rows],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _score_new_rows(df, scored_at):
    """Write fraud_score + scored_at for the just-inserted rows. Returns how many were scored.
    Inference features depend only on the row itself and fixed training statistics, so scoring
    a small batch on its own is valid."""
    try:
        scored = predict_fraud(df[['transaction_id', 'merchant_id', 'amount', 'transaction_ts']])
    except FileNotFoundError:
        logger.warning("Stream: fraud model not trained yet -- new transactions left unscored")
        return 0

    rows = [(tid, round(float(p), 4), scored_at) for tid, p in zip(df['transaction_id'], scored['fraud_probability'])]
    conn = get_connection()
    try:
        cursor = conn.cursor()
        execute_values(
            cursor,
            """
            UPDATE fact_transactions f SET fraud_score = v.score, scored_at = v.ts
            FROM (VALUES %s) AS v(tid, score, ts)
            WHERE f.transaction_id = v.tid
            """,
            rows,
            template="(%s, %s::numeric, %s::timestamp)",
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return len(rows)


def _refresh_recurring_flags(account_ids):
    """Re-derive is_recurring from each affected account's whole history and update every row whose
    flag changed (a new row can start a series, extend one, or break one's amount stability).
    Returns how many rows changed."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT transaction_id, account_id, merchant_id, amount::float, transaction_ts, is_recurring "
            "FROM fact_transactions WHERE account_id = ANY(%s)",
            (list(account_ids),),
        )
        history = pd.DataFrame(cursor.fetchall(), columns=['transaction_id', 'account_id', 'merchant_id', 'amount', 'date', 'was_recurring'])
        if history.empty:
            return 0
        history['date'] = pd.to_datetime(history['date'])
        detected = detect_recurring_transactions(history.copy())
        changed = detected[detected['is_recurring'].astype(bool) != history['was_recurring'].fillna(False).astype(bool)]
        if changed.empty:
            return 0

        execute_values(
            cursor,
            "UPDATE fact_transactions f SET is_recurring = v.flag FROM (VALUES %s) AS v(tid, flag) WHERE f.transaction_id = v.tid",
            [(tid, bool(flag)) for tid, flag in zip(changed['transaction_id'], changed['is_recurring'])],
            template="(%s, %s::boolean)",
        )
        conn.commit()
        return len(changed)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_cycle(now=None):
    account_ids = _fetch_account_ids()
    if not account_ids:
        logger.info("Stream: no accounts exist yet -- run the seed script first. Skipping cycle.")
        return

    now = now or datetime.now()
    date_id = _ensure_date_id(now)

    df = generate_batch(account_ids, now)
    df = categorize_transactions(df)
    df['transaction_id'] = compute_idempotency_key(df)
    df = df.drop_duplicates(subset=['transaction_id'])
    df['date'] = pd.to_datetime(df['date'])
    df['transaction_ts'] = df['date']
    df['date_id'] = date_id
    df['currency'] = 'INR'

    fact_columns = [
        'transaction_id', 'account_id', 'merchant_id', 'category_id', 'date_id',
        'transaction_ts', 'amount', 'currency', 'transaction_type', 'is_fraud', 'is_recurring'
    ]
    load_data(df[fact_columns], 'fact_transactions', conflict_columns=['transaction_id'])

    affected_accounts = df['account_id'].unique().tolist()
    balances_before = _latest_balances(affected_accounts)

    balance_rows = []
    for account_id, group in df.groupby('account_id'):
        opening = balances_before.get(account_id, 50000.0)
        inflow = float(group.loc[group['transaction_type'] == 'CREDIT', 'amount'].sum())
        outflow = float(group.loc[group['transaction_type'] == 'DEBIT', 'amount'].sum())
        closing = opening + inflow - outflow
        balance_rows.append({
            "account_id": account_id, "date_id": date_id,
            "opening_balance": opening, "closing_balance": closing,
            "inflow": inflow, "outflow": outflow
        })
    _upsert_balances(balance_rows)

    # The rows and balances are already committed, so a problem in either enrichment step
    # is logged rather than allowed to fail the whole cycle.
    scored = recurring_changed = 0
    try:
        scored = _score_new_rows(df, now)
    except Exception:
        logger.exception("Stream: scoring failed; new transactions left unscored")
    try:
        recurring_changed = _refresh_recurring_flags(affected_accounts)
    except Exception:
        logger.exception("Stream: recurring-flag refresh failed")

    logger.info(f"Stream: inserted {len(df)} transactions across {len(affected_accounts)} accounts "
                f"({scored} scored, {recurring_changed} recurring flags updated)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run a single batch and exit instead of looping.")
    parser.add_argument("--interval-minutes", type=float, default=DEFAULT_INTERVAL_MINUTES)
    args = parser.parse_args()

    if args.once:
        run_cycle()
        return

    logger.info(f"Stream: starting, generating a new batch every {args.interval_minutes} minute(s). Ctrl+C to stop.")
    while True:
        try:
            run_cycle()
        except Exception:
            logger.exception("Stream: cycle failed, will retry next interval")
        time.sleep(args.interval_minutes * 60)


if __name__ == "__main__":
    main()
