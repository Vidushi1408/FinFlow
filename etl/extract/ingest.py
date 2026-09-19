import os

import pandas as pd

from logging_config import logger

def ingest_csv_transactions(file_path):
    """
    Load bank CSV exports with error handling.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")
    
    try:
        df = pd.read_csv(file_path)
        logger.info(f"Successfully loaded {len(df)} rows from {file_path}")
        return df
    except Exception as e:
        logger.error(f"Error loading {file_path}: {e}")
        return None

def fetch_api_transactions(api_config):
    """
    Mock connection to bank APIs.
    """
    logger.info(f"Mock fetching from API with config: {api_config}")
    return pd.DataFrame()

def ingest_csv_merchants(file_path):
    return ingest_csv_transactions(file_path)

def ingest_csv_accounts(file_path):
    return ingest_csv_transactions(file_path)

def ingest_csv_users(file_path):
    return ingest_csv_transactions(file_path)
