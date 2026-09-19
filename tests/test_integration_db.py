import uuid

import pandas as pd
import pytest
import psycopg2

import etl.load.db as db_module
from tests.conftest import query

pytestmark = pytest.mark.integration


def _category_df(rows):
    return pd.DataFrame(rows, columns=["category_id", "category_name", "budget_amount"])


def test_load_data_empty_frame_is_a_noop(pg_db):
    db_module.load_data(pd.DataFrame(), "dim_category")
    db_module.load_data(None, "dim_category")


def test_load_data_inserts_rows(pg_db):
    cid = f"CAT_{uuid.uuid4().hex[:6]}"
    db_module.load_data(_category_df([(cid, f"Name of {cid}", 100)]), "dim_category", conflict_columns=["category_id"])
    assert query("SELECT category_name FROM dim_category WHERE category_id = %s", (cid,)) == [(f"Name of {cid}",)]


def test_load_data_upserts_on_conflict(pg_db):
    cid = f"CAT_{uuid.uuid4().hex[:6]}"
    db_module.load_data(_category_df([(cid, cid, 100)]), "dim_category", conflict_columns=["category_id"])
    db_module.load_data(_category_df([(cid, cid, 999)]), "dim_category", conflict_columns=["category_id"])
    assert float(query("SELECT budget_amount FROM dim_category WHERE category_id = %s", (cid,))[0][0]) == 999.0


def test_load_data_key_only_frame_does_nothing_on_conflict(pg_db):
    # When every column is a conflict column there is nothing to update -> ON CONFLICT DO NOTHING.
    # Also covers all-integer frames (numpy.int64 values), which psycopg2 can't adapt directly.
    conn = db_module.get_connection()
    conn.cursor().execute("CREATE TABLE key_only (id INT PRIMARY KEY)")
    conn.commit()
    conn.close()

    df = pd.DataFrame({"id": [1, 2]})
    db_module.load_data(df, "key_only", conflict_columns=["id"])
    db_module.load_data(df, "key_only", conflict_columns=["id"])  # re-load must not raise
    assert query("SELECT count(*) FROM key_only") == [(2,)]


def test_load_data_raises_and_rolls_back_on_failure(pg_db):
    bad = pd.DataFrame([{
        "transaction_id": "t1", "account_id": "missing-account", "merchant_id": "ZOMATO",
        "category_id": "FOOD", "date_id": 20240101, "transaction_ts": "2024-01-01 10:00:00",
        "amount": 10, "currency": "INR", "transaction_type": "DEBIT", "is_fraud": False, "is_recurring": False,
    }])
    with pytest.raises(psycopg2.Error):
        db_module.load_data(bad, "fact_transactions", conflict_columns=["transaction_id"])
    assert query("SELECT count(*) FROM fact_transactions WHERE transaction_id = 't1'") == [(0,)]


def test_load_dates_populates_dimension(pg_db):
    rows = query("SELECT year, month, day_of_week FROM dim_date WHERE date_id = 20260826")
    assert rows == [(2026, 8, 2)]  # a Wednesday
    # Republic Day is an Indian public holiday
    assert query("SELECT is_holiday FROM dim_date WHERE date_id = 20250126") == [(True,)]
