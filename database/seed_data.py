import os
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import etl.load.db as db
from database.migrate import run_migrations
from logging_config import logger


def setup_database():
    """Create the database if needed, apply schema.sql, then any pending migrations. Idempotent."""
    logger.info("Setting up database schema")

    # A failure creating the database is non-fatal (it commonly just means it
    # already exists, or this role can't see the admin database), but applying
    # the schema below is NOT optional: raise there so a broken deployment fails
    # loudly instead of silently running against a database with no tables.
    try:
        conn = db.get_connection("postgres")
        conn.autocommit = True
        cursor = conn.cursor()

        cursor.execute("SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s", (db.DB_NAME,))
        if not cursor.fetchone():
            # CREATE DATABASE doesn't support parameter binding; DB_NAME comes from trusted
            # deployment config (.env), never user input, so a validated identifier is used
            # instead of interpolating an arbitrary string.
            if not db.DB_NAME.isidentifier():
                raise ValueError(f"Invalid DB_NAME: {db.DB_NAME!r}")
            cursor.execute(f"CREATE DATABASE {db.DB_NAME}")
            logger.info(f"Created database {db.DB_NAME}")

        cursor.close()
        conn.close()
    except Exception as e:
        logger.warning(f"Could not create database (may already exist): {e}")

    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute((Path(__file__).parent / 'schema.sql').read_text())
        conn.commit()
        logger.info("Schema applied successfully")
    except Exception:
        conn.rollback()
        logger.exception("Error applying schema")
        raise
    finally:
        conn.close()

    run_migrations()

if __name__ == "__main__":
    try:
        setup_database()
    except Exception:
        sys.exit(1)
