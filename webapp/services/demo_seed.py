import random
import uuid
from datetime import datetime, timedelta

import pandas as pd
from faker import Faker

from data_generator.generate_mock_data import PERSONAS, generate_transactions
from etl.transform.clean import process_pipeline, derive_account_balances
from etl.load.db import load_data
from etl_pipeline import generate_sip_investments

fake = Faker()


TX_PER_ACCOUNT_PER_MONTH = 75  # ~2.5/day; matches data_generator/generate_mock_data.py's default density


def seed_demo_data_for_user(user_id, persona_key, history_months=6, num_transactions=None):
    """
    Create one demo account for `user_id` and populate it with persona-shaped
    transactions/balances/investments, reusing the same generator + ETL
    transforms as the bulk data generator (data_generator/generate_mock_data.py).

    Assumes the base merchant/category catalog (dim_merchant, dim_category) has
    already been loaded at least once via etl_pipeline.py -- this only adds one
    new account and its transactions, not the shared reference data.
    """
    if persona_key not in PERSONAS:
        raise ValueError(f"Unknown persona: {persona_key}")

    if num_transactions is None:
        num_transactions = int(history_months * TX_PER_ACCOUNT_PER_MONTH)

    persona = PERSONAS[persona_key]
    account_id = str(uuid.uuid4())
    end_date = datetime.now()
    start_date = end_date - timedelta(days=30 * history_months)
    starting_balance = round(random.uniform(*persona['starting_balance']), 2)

    account_row = {
        'account_id': account_id,
        'user_id': user_id,
        'account_type': persona['account_type'],
        'institution': random.choice(['HDFC', 'SBI', 'ICICI', 'Axis']),
        'account_number': fake.bban(),
        'credit_limit': None,
        'opened_date': (start_date - timedelta(days=200)).date()
    }
    load_data(pd.DataFrame([account_row]), 'dim_account', conflict_columns=['account_id'])

    fake_accounts = [{'account_id': account_id, 'persona': persona_key}]
    transactions = generate_transactions(fake_accounts, num_transactions, start_date, end_date)
    df_tx = pd.DataFrame(transactions)
    df_clean = process_pipeline(df_tx)

    fact_cols = [
        'transaction_id', 'account_id', 'merchant_id', 'category_id',
        'date_id', 'transaction_ts', 'amount', 'currency', 'transaction_type',
        'is_fraud', 'is_recurring'
    ]
    load_data(df_clean[fact_cols], 'fact_transactions', conflict_columns=['transaction_id'])

    df_balances = derive_account_balances(df_clean, starting_balances={account_id: starting_balance})
    load_data(df_balances, 'fact_account_balance', conflict_columns=['account_id', 'date_id'])

    df_acc_for_sip = pd.DataFrame([{'account_id': account_id}])
    investments = generate_sip_investments(df_acc_for_sip, df_clean)
    if investments:
        load_data(pd.DataFrame(investments), 'fact_investments', conflict_columns=['investment_id'])

    return account_id
