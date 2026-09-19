"""
Applies the numbered SQL files in database/migrations/ to a database, each
exactly once, in order, and records what it has applied in `schema_migrations`.

    python database/migrate.py            # apply anything pending
    python database/migrate.py --status   # show applied / pending / changed

Safe on both fresh and long-lived databases: every migration is written to be
idempotent (IF NOT EXISTS ...), so on a database that already has these
changes (schema.sql creates the latest shape from scratch, or someone ran them
by hand) they are simply recorded. Each migration runs in its own transaction,
so a failure leaves the database as it was before that migration, and an
advisory lock stops two runners (e.g. two containers starting together) from
applying the same migration twice.
"""
import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from etl.load.db import get_connection
from logging_config import logger

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_LOCK_KEY = "finflow_schema_migrations"


class MigrationError(RuntimeError):
    pass


def _checksum(sql):
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def discover(migrations_dir=None):
    """[(version, path)] sorted by filename. Files that don't look like NNNN_name.sql are ignored with a warning."""
    found = []
    for path in sorted(Path(migrations_dir or MIGRATIONS_DIR).glob("*.sql")):
        if _FILENAME.match(path.name):
            found.append((path.stem, path))
        else:
            logger.warning(f"Ignoring {path.name}: migration files must be named NNNN_description.sql")
    return found


def _ensure_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version VARCHAR(100) PRIMARY KEY,
            checksum CHAR(64) NOT NULL,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


def _applied(cursor):
    cursor.execute("SELECT version, checksum FROM schema_migrations")
    return dict(cursor.fetchall())


def status(migrations_dir=None, conn=None):
    """[{version, state}] where state is applied | pending | changed (applied, but the file was edited since)."""
    own = conn is None
    conn = conn or get_connection()
    try:
        cursor = conn.cursor()
        _ensure_table(cursor)
        conn.commit()
        applied = _applied(cursor)
        result = []
        for version, path in discover(migrations_dir):
            if version not in applied:
                state = "pending"
            elif applied[version].strip() != _checksum(path.read_text()):
                state = "changed"
            else:
                state = "applied"
            result.append({"version": version, "state": state})
        return result
    finally:
        if own:
            conn.close()


def run_migrations(migrations_dir=None, conn=None):
    """Apply pending migrations. Returns the versions applied by this call."""
    own = conn is None
    conn = conn or get_connection()
    newly_applied = []
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_advisory_lock(hashtext(%s))", (_LOCK_KEY,))
        try:
            _ensure_table(cursor)
            conn.commit()
            applied = _applied(cursor)

            on_disk = discover(migrations_dir)
            for version in sorted(set(applied) - {v for v, _ in on_disk}):
                logger.warning(f"Migration {version} is recorded as applied but its file is missing")

            for version, path in on_disk:
                sql = path.read_text()
                if version in applied:
                    if applied[version].strip() != _checksum(sql):
                        logger.warning(f"Migration {version} was edited after it was applied; not re-running it. Add a new migration instead.")
                    continue
                try:
                    cursor.execute(sql)
                    cursor.execute("INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)", (version, _checksum(sql)))
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    raise MigrationError(f"Migration {version} failed and was rolled back: {e}") from e
                logger.info(f"Applied migration {version}")
                newly_applied.append(version)
        finally:
            cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_LOCK_KEY,))
            conn.commit()
    finally:
        if own:
            conn.close()

    if not newly_applied:
        logger.info("Database is up to date; no migrations to apply")
    return newly_applied


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true", help="Show migration state instead of applying anything.")
    args = parser.parse_args()
    if args.status:
        for row in status():
            print(f"{row['state']:8} {row['version']}")
        return
    run_migrations()


if __name__ == "__main__":
    try:
        main()
    except MigrationError as e:
        logger.error(str(e))
        sys.exit(1)
