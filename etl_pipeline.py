import os
import random
import sys
from etl.extract.ingest import ingest_csv_accounts, ingest_csv_merchants, ingest_csv_transactions, ingest_csv_users
from etl.transform.clean import process_pipeline, derive_account_balances
from etl.load.db import load_data, load_dates
import pandas as pd
import uuid

from logging_config import logger

SIP_SYMBOLS = ['RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'NIFTYBEES']

def generate_sip_investments(df_accounts, df_clean, sip_fraction=0.5, monthly_amount_range=(1000, 8000)):
    """
    Simulate SIP-style monthly contributions: a subset of accounts invests a
    fixed rupee amount into 1-2 symbols on the same day each month, and the
    unit price drifts slowly (a simple random walk) so quantity accumulates
    and current_price/gain_loss reflect a real running position, not a
    one-off random buy.
    """
    investments = []
    if df_accounts is None or len(df_accounts) == 0 or len(df_clean) == 0:
        return investments

    start_date = df_clean['date'].min()
    end_date = df_clean['date'].max()

    investing_accounts = df_accounts['account_id'].sample(
        max(1, int(len(df_accounts) * sip_fraction))
    )

    for acc_id in investing_accounts:
        for symbol in random.sample(SIP_SYMBOLS, k=random.randint(1, 2)):
            monthly_amount = random.uniform(*monthly_amount_range)
            price = random.uniform(200, 3000)  # starting unit price for this SIP
            cumulative_qty = 0.0
            cumulative_cost = 0.0

            current_month = start_date.replace(day=1)
            while current_month <= end_date:
                # Random walk with a slight upward drift, like a real NAV series
                price = max(10.0, price * (1 + random.uniform(-0.05, 0.07)))
                qty = monthly_amount / price
                cumulative_qty += qty
                cumulative_cost += monthly_amount

                avg_cost_price = cumulative_cost / cumulative_qty
                investments.append({
                    'investment_id': str(uuid.uuid4()),
                    'account_id': acc_id,
                    'date_id': int(current_month.strftime('%Y%m%d')),
                    'symbol': symbol,
                    'quantity': round(cumulative_qty, 6),
                    'cost_price': round(avg_cost_price, 2),
                    'current_price': round(price, 2),
                    'gain_loss': round((price - avg_cost_price) * cumulative_qty, 2)
                })

                current_month = (current_month.replace(day=28) + pd.Timedelta(days=4)).replace(day=1)

    return investments

def run_pipeline():
    logger.info("Starting ETL pipeline")

    # 1. Populate dimension tables
    logger.info("Loading date dimension")
    load_dates()

    # Read generated files (assume they are generated in project root)
    if not os.path.exists('accounts.csv') or not os.path.exists('transactions.csv'):
        logger.error("Data files not found. Run 'python data_generator/generate_mock_data.py' first.")
        return

    if os.path.exists('users.csv'):
        logger.info("Loading users")
        df_users = ingest_csv_users('users.csv')
        if df_users is not None:
            load_data(df_users, 'dim_user', conflict_columns=['user_id'])

    logger.info("Loading accounts")
    df_accounts = ingest_csv_accounts('accounts.csv')
    starting_balances = {}
    if df_accounts is not None:
        # starting_balance seeds fact_account_balance below; dim_account has no such column.
        if 'starting_balance' in df_accounts.columns:
            starting_balances = dict(zip(df_accounts['account_id'], df_accounts['starting_balance']))
            df_accounts = df_accounts.drop(columns=['starting_balance'])
        load_data(df_accounts, 'dim_account', conflict_columns=['account_id'])

    logger.info("Loading merchants and categories")
    df_merchants = ingest_csv_merchants('merchants.csv')
    if df_merchants is not None:
        # Load unique categories with seeded budget
        categories = df_merchants['category'].unique()
        budget_map = {
            'FOOD': 20000,
            'SHOPPING': 30000,
            'TRANSPORT': 10000,
            'ENTERTAINMENT': 5000,
            'HEALTH': 15000,
            'FUEL': 8000,
            'GROCERY': 25000,
            'FINANCE': 50000,
            'RENT': 15000,
            'RECHARGE': 1000,
            'UPI': 6000,
            'INCOME': 0,
            'TRANSFER': 0
        }
        df_categories = pd.DataFrame({
            'category_id': categories,
            'category_name': categories,
            'budget_amount': [budget_map.get(cat, 10000) for cat in categories]
        })
        load_data(df_categories, 'dim_category', conflict_columns=['category_id'])

        # Load merchants
        load_data(df_merchants[['merchant_id', 'merchant_name', 'category', 'mcc_code']], 'dim_merchant', conflict_columns=['merchant_id'])

    logger.info("Loading transactions")
    df_transactions = ingest_csv_transactions('transactions.csv')
    if df_transactions is not None:
        df_clean = process_pipeline(df_transactions)

        # Map to fact_transactions schema
        df_fact = df_clean[[
            'transaction_id', 'account_id', 'merchant_id', 'category_id',
            'date_id', 'transaction_ts', 'amount', 'currency', 'transaction_type',
            'is_fraud', 'is_recurring'
        ]]

        load_data(df_fact, 'fact_transactions', conflict_columns=['transaction_id'])

        logger.info("Deriving account balances")
        df_balances = derive_account_balances(df_clean, starting_balances=starting_balances)
        load_data(df_balances, 'fact_account_balance', conflict_columns=['account_id', 'date_id'])

        logger.info("Generating SIP-style investments")
        investments = generate_sip_investments(df_accounts, df_clean)
        df_investments = pd.DataFrame(investments)
        load_data(df_investments, 'fact_investments', conflict_columns=['investment_id'])

    logger.info("ETL pipeline complete")

if __name__ == "__main__":
    try:
        run_pipeline()
    except Exception:
        logger.exception("ETL pipeline failed")
        sys.exit(1)
