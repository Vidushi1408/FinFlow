"""
Idempotent one-shot seed script: makes `docker compose up` (or a fresh local
setup) produce a working demo account with realistic data, with no manual
steps. Safe to re-run -- if demo data already exists, it does nothing.

What it does, in order:
  1. Apply the database schema (idempotent: CREATE TABLE IF NOT EXISTS / ALTER
     TABLE ADD COLUMN IF NOT EXISTS throughout schema.sql).
  2. If dim_user is already populated, stop here -- someone has real data.
  3. Otherwise: generate synthetic users/accounts/transactions, run the ETL
     pipeline, and train the ML models against it.
  4. Point one of the generated accounts at a fixed, documented demo login
     (see DEMO_EMAIL/DEMO_PASSWORD below) so there's always a guaranteed way
     in, alongside the other generated personas.

Note: the spec's suggested demo email "demo@finflow.local" doesn't pass this
app's own email validation (.local is a reserved TLD, correctly rejected by
the email-validator library WTForms uses) -- nobody could actually log in
with it through the real login form. Using a valid-looking domain instead so
the seeded account is actually usable, not just present in the database.
"""
import os
import sys
import time

import psycopg2

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logging_config import logger
from database.seed_data import setup_database
from etl.load.db import get_connection

DEMO_EMAIL = os.environ.get("SEED_DEMO_EMAIL", "demo@finflow.app")
DEMO_PASSWORD = "demo1234"  # nosec B105 - matches data_generator.generate_mock_data.DEMO_PASSWORD, same documented demo credential

SEED_USERS = int(os.environ.get("SEED_USERS", "9"))
SEED_HISTORY_MONTHS = int(os.environ.get("SEED_HISTORY_MONTHS", "12"))
SEED_RANDOM_SEED = int(os.environ.get("SEED_RANDOM_SEED", "7"))


def _wait_for_db(max_attempts=30, delay_seconds=2):
    """Wait until the Postgres SERVER accepts connections. The target database itself may not exist yet
    (setup_database creates it), so the always-present "postgres" database is used as the fallback probe."""
    for attempt in range(1, max_attempts + 1):
        try:
            try:
                conn = get_connection()
            except psycopg2.OperationalError as e:
                if "does not exist" not in str(e):
                    raise
                conn = get_connection("postgres")
            conn.close()
            return
        except Exception as e:
            logger.info(f"Database not ready yet (attempt {attempt}/{max_attempts}): {e}")
            time.sleep(delay_seconds)
    raise RuntimeError("Database never became ready")


def _demo_data_already_exists():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM dim_user")
        count = cursor.fetchone()[0]
        return count > 0
    finally:
        conn.close()


def _generate_and_load():
    import random
    from faker import Faker
    from datetime import datetime, timedelta

    from data_generator.generate_mock_data import (
        generate_users, generate_accounts, generate_transactions,
        MERCHANTS, EMPLOYERS, CLIENTS, RENT_MERCHANT, STIPEND_SOURCE, save_csv
    )

    random.seed(SEED_RANDOM_SEED)
    Faker.seed(SEED_RANDOM_SEED)

    end_date = datetime.now()
    start_date = end_date - timedelta(days=30 * SEED_HISTORY_MONTHS)

    logger.info(f"Generating {SEED_USERS} users over {SEED_HISTORY_MONTHS} months of history")
    users = generate_users(SEED_USERS)

    # Point one salaried-persona account at the documented demo login so it's
    # always reachable, without giving up the varied persona mix for everyone else.
    demo_user = next((u for u in users if u["persona"] == "salaried"), users[0])
    demo_user["email"] = DEMO_EMAIL
    from werkzeug.security import generate_password_hash
    demo_user["password_hash"] = generate_password_hash(DEMO_PASSWORD)

    save_csv(users, "users.csv", ["user_id", "name", "email", "password_hash", "currency", "created_at"])

    accounts = generate_accounts(users, start_date)
    save_csv(accounts, "accounts.csv", [
        "account_id", "user_id", "account_type", "institution", "account_number",
        "credit_limit", "opened_date", "starting_balance"
    ])

    all_merchants = MERCHANTS + EMPLOYERS + CLIENTS + [
        RENT_MERCHANT, STIPEND_SOURCE, {"name": "Transfer", "category": "TRANSFER", "mcc": "0000"}
    ]
    seen_ids = set()
    merchant_data = []
    for m in all_merchants:
        merchant_id = m["name"].replace(" ", "_").upper()
        if merchant_id in seen_ids:
            continue
        seen_ids.add(merchant_id)
        merchant_data.append({
            "merchant_id": merchant_id, "merchant_name": m["name"],
            "category": m["category"], "mcc_code": m["mcc"]
        })
    save_csv(merchant_data, "merchants.csv", ["merchant_id", "merchant_name", "category", "mcc_code"])

    tx_per_month = 75  # matches the generator's own default density
    num_transactions = int(SEED_USERS * SEED_HISTORY_MONTHS * tx_per_month)
    logger.info(f"Generating ~{num_transactions} transactions")
    transactions = generate_transactions(accounts, num_transactions, start_date, end_date)
    save_csv(transactions, "transactions.csv", [
        "transaction_id", "account_id", "merchant_id", "category_id", "date",
        "amount", "transaction_type", "is_fraud", "is_recurring"
    ])

    logger.info("Running ETL pipeline")
    import etl_pipeline
    etl_pipeline.run_pipeline()

    logger.info("Marking generated users as onboarded")
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE dim_user SET onboarded_at = CURRENT_TIMESTAMP WHERE onboarded_at IS NULL")
        conn.commit()
    finally:
        conn.close()

    logger.info("Training ML models")
    import train_models
    train_models.main()


def main():
    logger.info("Seed: waiting for database")
    _wait_for_db()

    logger.info("Seed: applying schema")
    setup_database()

    if _demo_data_already_exists():
        logger.info("Seed: demo data already present, nothing to do")
        return

    _generate_and_load()

    logger.info(f"Seed complete. Demo login: {DEMO_EMAIL} / {DEMO_PASSWORD}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Seed script failed")
        sys.exit(1)
