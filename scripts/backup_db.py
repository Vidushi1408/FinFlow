"""
Back up the FinFlow database with `pg_dump` (custom format: compressed,
supports selective/parallel restore with `pg_restore`), and delete backups
older than the retention window.

    python scripts/backup_db.py                       # backup to ./backups/, keep 30 days
    python scripts/backup_db.py --dir /mnt/backups --keep-days 7
    python scripts/backup_db.py --keep-days 0          # back up only, keep everything

Or from Docker, using the app image's own postgresql-client (no local pg_dump needed):

    docker compose run --rm -v "$(pwd)/backups:/app/backups" webapp python scripts/backup_db.py

Restoring is intentionally NOT automated here -- a script that can restore
can also overwrite a live database by mistake. See docs/DATA_POLICY.md for
the manual restore command (`pg_restore ...`) and the retention/backup policy
this script implements.
"""
import argparse
import os
import re
# pg_dump/pg_restore are external tools with no Python API; every subprocess call below uses a
# fixed argv list (never shell=True, never string concatenation), and dbname/output paths come
# from CLI args or this repo's own DB_* config -- never from a web request or other untrusted input.
import subprocess  # nosec B404
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from etl.load import db as db_module
from logging_config import logger

DEFAULT_BACKUP_DIR = Path(__file__).resolve().parent.parent / "backups"
DEFAULT_KEEP_DAYS = 30
_FILENAME_RE = re.compile(r"^(?P<dbname>.+)_(?P<timestamp>\d{8}T\d{6}Z)\.dump$")


class BackupError(RuntimeError):
    pass


def _backup_filename(dbname, now=None):
    now = now or datetime.now(timezone.utc)
    return f"{dbname}_{now.strftime('%Y%m%dT%H%M%SZ')}.dump"


def _parse_backup_timestamp(path):
    """UTC datetime encoded in a backup's filename, or None if it doesn't look like one of ours
    (so rotation only ever touches files this script created)."""
    match = _FILENAME_RE.match(path.name)
    if not match:
        return None
    return datetime.strptime(match.group("timestamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def create_backup(output_dir=DEFAULT_BACKUP_DIR, dbname=None, now=None):
    """Run pg_dump -Fc (custom format) for `dbname` (default: the configured database) into
    `output_dir`. Returns the created file's path. Raises BackupError on failure or if pg_dump
    isn't installed, rather than leaving a truncated file that looks like a valid backup."""
    dbname = dbname or db_module.DB_NAME
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / _backup_filename(dbname, now)

    env = {**os.environ, "PGPASSWORD": db_module.DB_PASSWORD}
    command = [
        "pg_dump", "-Fc",
        "-h", db_module.DB_HOST, "-p", str(db_module.DB_PORT), "-U", db_module.DB_USER,
        "-d", dbname, "-f", str(dest),
    ]

    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=600)  # nosec B603
    except FileNotFoundError as e:
        raise BackupError(
            "pg_dump is not installed. Install the PostgreSQL client tools (e.g. `apt-get install "
            "postgresql-client` / `brew install postgresql`), or run this from the app's Docker "
            "image, which has them."
        ) from e
    except subprocess.TimeoutExpired as e:
        dest.unlink(missing_ok=True)
        raise BackupError(f"pg_dump did not finish within {e.timeout}s") from e

    if result.returncode != 0:
        dest.unlink(missing_ok=True)  # never leave a partial/failed file that looks like a real backup
        raise BackupError(f"pg_dump failed (exit {result.returncode}): {result.stderr.strip()}")

    size = dest.stat().st_size
    if size == 0:
        dest.unlink(missing_ok=True)
        raise BackupError("pg_dump produced an empty file")

    logger.info(f"Backed up {dbname} to {dest} ({size:,} bytes)")
    return dest


def rotate_backups(output_dir=DEFAULT_BACKUP_DIR, keep_days=DEFAULT_KEEP_DAYS, now=None):
    """Delete backups (matching this script's own filename pattern, in `output_dir`) older than
    `keep_days`. keep_days=0 (or negative) means keep everything -- rotation is opt-out, not
    something a bad argument can accidentally turn into "delete all backups"."""
    if keep_days <= 0:
        return []

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=keep_days)
    removed = []

    for path in Path(output_dir).glob("*.dump"):
        timestamp = _parse_backup_timestamp(path)
        if timestamp is not None and timestamp < cutoff:
            path.unlink()
            removed.append(path)

    if removed:
        logger.info(f"Removed {len(removed)} backup(s) older than {keep_days} day(s)")
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path, default=DEFAULT_BACKUP_DIR, help=f"Backup directory (default: {DEFAULT_BACKUP_DIR})")
    parser.add_argument("--keep-days", type=int, default=DEFAULT_KEEP_DAYS, help=f"Delete backups older than this many days (default: {DEFAULT_KEEP_DAYS}; 0 = keep forever)")
    parser.add_argument("--dbname", default=None, help="Database to back up (default: DB_NAME from the environment)")
    args = parser.parse_args()

    create_backup(args.dir, args.dbname)
    rotate_backups(args.dir, args.keep_days)


if __name__ == "__main__":
    try:
        main()
    except BackupError as e:
        logger.error(str(e))
        sys.exit(1)
