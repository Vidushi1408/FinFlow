import os
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
import psycopg2
from psycopg2.extras import execute_values
import pandas as pd
from dotenv import load_dotenv

from logging_config import logger

load_dotenv()

DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "finflow")

# The single place database settings are read. Everything that talks to
# Postgres (ETL, scripts, the web app, tests) goes through these three
# helpers, and they read the module settings at call time so a test can point
# them at a throwaway database by changing DB_NAME.

def get_database_url(dbname=None):
    """SQLAlchemy URL for `dbname` (default: the configured database). Built with URL.create so a
    password containing characters like @ or / doesn't corrupt the URL."""
    return URL.create("postgresql", username=DB_USER, password=DB_PASSWORD, host=DB_HOST, port=int(DB_PORT), database=dbname or DB_NAME)


def get_engine(**engine_kwargs):
    return create_engine(get_database_url(), **engine_kwargs)


def get_connection(dbname=None, **connect_kwargs):
    """Raw psycopg2 connection (for execute_values / DDL). Pass dbname="postgres" for admin work such as CREATE DATABASE."""
    return psycopg2.connect(
        dbname=dbname or DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
        **connect_kwargs
    )

def load_data(df, table_name, conflict_columns=None, update_on_conflict=True):
    """
    Batch insert/upsert for performance. With conflict_columns, existing rows are
    updated (or, with update_on_conflict=False, left alone). Raises on failure rather than
    swallowing it -- a load that silently fails but reports "success" is
    worse than a loud crash: it masks real bugs (an oversized column, a
    missing FK row) as an empty table instead of a stack trace pointing at
    the cause.
    """
    if df is None or len(df) == 0:
        logger.info(f"No data to load for {table_name}")
        return

    conn = get_connection()
    cursor = conn.cursor()

    columns = list(df.columns)
    # astype(object).tolist() yields native Python values; psycopg2 can't adapt
    # numpy scalars (e.g. numpy.int64 from an all-integer frame).
    values = [tuple(x) for x in df.astype(object).to_numpy().tolist()]

    insert_query = f"INSERT INTO {table_name} ({','.join(columns)}) VALUES %s"

    if conflict_columns:
        update_set = ', '.join([f"{col} = EXCLUDED.{col}" for col in columns if col not in conflict_columns])
        # update_on_conflict=False keeps rows that already exist untouched (e.g. a
        # re-imported statement must not overwrite categories the user corrected).
        if update_set and update_on_conflict:
            insert_query += f" ON CONFLICT ({','.join(conflict_columns)}) DO UPDATE SET {update_set}"
        else:
            insert_query += f" ON CONFLICT ({','.join(conflict_columns)}) DO NOTHING"

    try:
        execute_values(cursor, insert_query, values, page_size=1000)
        conn.commit()
        logger.info(f"Successfully loaded {len(df)} rows into {table_name}")
    except Exception as e:
        conn.rollback()
        logger.error(f"Error loading data into {table_name}: {e}")
        raise
    finally:
        cursor.close()
        conn.close()

def load_dates(start_date='2020-01-01', end_date='2030-12-31'):
    """Populate the dim_date table."""
    import holidays
    in_holidays = holidays.country_holidays('IN', years=range(int(start_date[:4]), int(end_date[:4]) + 1))

    dates = pd.date_range(start=start_date, end=end_date)
    df = pd.DataFrame({
        'date_id': dates.strftime('%Y%m%d').astype(int),
        'date': dates,
        'year': dates.year,
        'month': dates.month,
        'quarter': dates.quarter,
        'day_of_week': dates.dayofweek,
        'is_holiday': [d in in_holidays for d in dates]
    })
    load_data(df, 'dim_date', conflict_columns=['date_id'])
