import logging
import uuid

import psycopg2
import pytest

import etl.load.db as db_module
from etl.load.reference import load_reference_catalog


def _admin_connection():
    return db_module.get_connection("postgres", connect_timeout=3)


@pytest.fixture(scope="session")
def pg_db():
    """
    A throwaway PostgreSQL database with the real schema, date dimension and
    merchant/category catalog loaded. Points etl.load.db (and everything that
    calls its get_connection) at it for the whole test session, then drops it.
    Skips -- rather than fails -- when no server is reachable, so the plain
    unit-test run still works on a machine without Postgres.
    """
    try:
        admin = _admin_connection()
    except psycopg2.OperationalError as e:
        pytest.skip(f"PostgreSQL not reachable, skipping integration tests: {e}")

    test_db_name = f"finflow_test_{uuid.uuid4().hex[:8]}"
    admin.autocommit = True
    admin.cursor().execute(f"CREATE DATABASE {test_db_name}")

    original_name = db_module.DB_NAME
    db_module.DB_NAME = test_db_name
    try:
        from pathlib import Path
        schema_sql = (Path(__file__).resolve().parent.parent / "database" / "schema.sql").read_text()
        conn = db_module.get_connection()
        try:
            conn.cursor().execute(schema_sql)
            conn.commit()
        finally:
            conn.close()

        db_module.load_dates("2024-01-01", "2030-12-31")
        load_reference_catalog()
        yield test_db_name
    finally:
        db_module.DB_NAME = original_name
        admin.cursor().execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (test_db_name,),
        )
        admin.cursor().execute(f"DROP DATABASE IF EXISTS {test_db_name}")
        admin.close()


@pytest.fixture
def make_user(pg_db):
    """Insert a dim_user (+ optionally an account) and return their ids."""
    def _make(with_account=True):
        user_id = str(uuid.uuid4())
        account_id = str(uuid.uuid4())
        conn = db_module.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO dim_user (user_id, name, email, password_hash) VALUES (%s, %s, %s, %s)",
                (user_id, "Test User", f"{user_id}@example.com", "x"),
            )
            if with_account:
                cur.execute(
                    "INSERT INTO dim_account (account_id, user_id, account_type, institution, account_number, opened_date) "
                    "VALUES (%s, %s, 'SAVINGS', 'HDFC', '1234567890', '2024-01-01')",
                    (account_id, user_id),
                )
            conn.commit()
        finally:
            conn.close()
        return user_id, account_id
    return _make


def query(sql, params=None):
    conn = db_module.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


@pytest.fixture
def app_log(caplog):
    """Capture the app's 'finflow' logger (it doesn't propagate to the root logger, so caplog alone sees nothing)."""
    app_logger = logging.getLogger("finflow")
    app_logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="finflow")
    yield caplog
    app_logger.removeHandler(caplog.handler)
