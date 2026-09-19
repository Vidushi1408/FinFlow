"""
Times every data endpoint for ONE user with a large transaction history, in a
throwaway database (your real data is never touched).

    python scripts/benchmark_endpoints.py                 # 50,000 transactions
    python scripts/benchmark_endpoints.py --rows 200000

Prints the median of a few requests per endpoint, slowest first.
"""
import argparse
import contextlib
import os
import random
import statistics
import sys
import time
import uuid
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from faker import Faker
from sqlalchemy.orm import sessionmaker

import etl.load.db as db
from data_generator.generate_mock_data import generate_transactions
from database.seed_data import setup_database
from etl.load.reference import load_reference_catalog
from etl.transform.clean import process_pipeline
from logging_config import logger

ENDPOINTS = [
    "/api/v1/dashboard/summary",
    "/api/v1/transactions?page_size=25",
    "/api/v1/transactions?search=zomato&type=DEBIT",
    "/api/v1/budgets",
    "/api/v1/spending/summary?month={month}",
    "/api/v1/spending/trends?days=90",
    "/api/v1/insights/subscriptions",
    "/api/v1/fraud/alerts",
    "/api/v1/cash-flow/forecast",
    "/api/v1/reports/monthly?month={month}",
    "/api/v1/goals",
    "/api/v1/settings/accounts",
]


@contextlib.contextmanager
def scratch_database():
    """Point the app at a brand-new throwaway database (schema, dates and reference data loaded), drop it afterwards."""
    real_name = db.DB_NAME
    scratch = f"finflow_bench_{uuid.uuid4().hex[:8]}"
    db.DB_NAME = scratch
    try:
        setup_database()
        db.load_dates("2020-01-01", "2030-12-31")
        load_reference_catalog()
        yield scratch
    finally:
        db.DB_NAME = real_name
        admin = db.get_connection("postgres")
        admin.autocommit = True
        admin.cursor().execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        admin.close()


def seed_user_with_history(rows, years=3):
    """One salaried user, `rows` transactions spread over `years` years, loaded the same way the real pipeline does."""
    random.seed(1)
    Faker.seed(1)
    user_id, account_id = str(uuid.uuid4()), str(uuid.uuid4())
    end = datetime.now()
    start = end - timedelta(days=365 * years)

    db.load_data(pd.DataFrame([{
        "user_id": user_id, "name": "Load Test", "email": f"{user_id}@example.com", "password_hash": "x",
        "currency": "INR", "onboarded_at": datetime.now(),
    }]), "dim_user", conflict_columns=["user_id"])
    db.load_data(pd.DataFrame([{
        "account_id": account_id, "user_id": user_id, "account_type": "CHECKING", "institution": "HDFC",
        "account_number": "1234567890", "credit_limit": None, "opened_date": start.date(),
    }]), "dim_account", conflict_columns=["account_id"])

    transactions = generate_transactions([{"account_id": account_id, "persona": "salaried"}], rows, start, end)
    df = process_pipeline(pd.DataFrame(transactions))
    db.load_data(df[["transaction_id", "account_id", "merchant_id", "category_id", "date_id", "transaction_ts",
                     "amount", "currency", "transaction_type", "is_fraud", "is_recurring"]],
                 "fact_transactions", conflict_columns=["transaction_id"])
    logger.info(f"Seeded {len(df)} transactions")
    return user_id, account_id


def logged_in_client(user_id):
    """A Flask test client signed in as `user_id`, reading from whichever database db.DB_NAME points at.
    (Mutates the app's session factory and login loader; callers that need to undo that use monkeypatch.)"""
    import webapp.app as app_module
    import webapp.auth as auth_module

    app_module.SessionLocal = sessionmaker(bind=db.get_engine())
    user = auth_module.User(user_id, "Load Test", "load@example.com", "INR", 0.8)
    auth_module.login_manager._user_callback = lambda uid: user if uid == user_id else None
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = user_id
        sess["_fresh"] = True
    return client


def time_endpoints(client, repeats=3):
    """[(median_ms, status, url)] for every data endpoint, slowest first."""
    month = datetime.now().strftime("%Y-%m")
    results = []
    for template in ENDPOINTS:
        url = template.format(month=month)
        samples, status = [], None
        for _ in range(repeats):
            started = time.perf_counter()
            response = client.get(url)
            samples.append((time.perf_counter() - started) * 1000)
            status = response.status_code
        results.append((statistics.median(samples), status, url))
    return sorted(results, reverse=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", type=int, default=50000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    with scratch_database():
        user_id, _ = seed_user_with_history(args.rows)
        results = time_endpoints(logged_in_client(user_id), args.repeats)

    print(f"\n{args.rows:,} transactions for one user (median of {args.repeats} requests)\n")
    print(f"{'ms':>9}  status  endpoint")
    for ms, status, url in results:
        print(f"{ms:9.0f}  {status:6}  {url}")


if __name__ == "__main__":
    main()
