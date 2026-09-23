"""
Backup filename/rotation logic (pure filesystem, no database needed), plus a real end-to-end
pg_dump -> pg_restore round trip against a throwaway Postgres database (skips cleanly if pg_dump/
pg_restore or a reachable Postgres server aren't available, matching this codebase's pattern for
every other piece of external infrastructure).
"""
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from scripts.backup_db import (
    BackupError, _backup_filename, _parse_backup_timestamp,
    create_backup, rotate_backups,
)


# --- filename encoding/parsing (no infra needed) ---

def test_backup_filename_encodes_dbname_and_utc_timestamp():
    now = datetime(2026, 3, 5, 14, 30, 0, tzinfo=timezone.utc)
    assert _backup_filename("finflow", now) == "finflow_20260305T143000Z.dump"


def test_backup_filename_round_trips_through_parsing(tmp_path):
    now = datetime(2026, 3, 5, 14, 30, 0, tzinfo=timezone.utc)
    path = tmp_path / _backup_filename("finflow", now)
    path.touch()
    assert _parse_backup_timestamp(path) == now


def test_parse_backup_timestamp_ignores_files_that_are_not_ours(tmp_path):
    for name in ["notes.dump", "finflow.dump", "random_file.txt", "finflow_2026-03-05.dump"]:
        assert _parse_backup_timestamp(tmp_path / name) is None


# --- rotation (pure filesystem: tmp_path fixture, no database) ---

def _make_backup(directory, dbname, age_days, now):
    path = directory / _backup_filename(dbname, now - timedelta(days=age_days))
    path.write_bytes(b"fake dump content")
    return path


def test_rotate_backups_removes_only_files_older_than_keep_days(tmp_path):
    now = datetime(2026, 3, 5, tzinfo=timezone.utc)
    old = _make_backup(tmp_path, "finflow", age_days=31, now=now)
    recent = _make_backup(tmp_path, "finflow", age_days=29, now=now)
    exactly_at_cutoff = _make_backup(tmp_path, "finflow", age_days=30, now=now)

    removed = rotate_backups(tmp_path, keep_days=30, now=now)

    assert old in removed and not old.exists()
    assert recent not in removed and recent.exists()
    assert exactly_at_cutoff not in removed and exactly_at_cutoff.exists()  # "30 days" keeps day 30 itself


def test_rotate_backups_leaves_non_backup_files_alone(tmp_path):
    old = _make_backup(tmp_path, "finflow", age_days=99, now=datetime.now(timezone.utc))
    unrelated = tmp_path / "README.txt"
    unrelated.write_text("not a backup")

    rotate_backups(tmp_path, keep_days=1)

    assert not old.exists()
    assert unrelated.exists()


@pytest.mark.parametrize("keep_days", [0, -1, -30])
def test_zero_or_negative_keep_days_means_keep_everything(tmp_path, keep_days):
    """A bad or zero argument must not silently become "delete all backups"."""
    old = _make_backup(tmp_path, "finflow", age_days=9999, now=datetime.now(timezone.utc))
    removed = rotate_backups(tmp_path, keep_days=keep_days)
    assert removed == [] and old.exists()


def test_rotate_backups_on_an_empty_or_missing_directory_does_nothing(tmp_path):
    assert rotate_backups(tmp_path / "does_not_exist", keep_days=30) == []
    assert rotate_backups(tmp_path, keep_days=30) == []


# --- real pg_dump / pg_restore ---

pg_dump_available = shutil.which("pg_dump") is not None and shutil.which("pg_restore") is not None


@pytest.mark.integration
@pytest.mark.skipif(not pg_dump_available, reason="pg_dump/pg_restore not installed")
def test_create_backup_produces_a_restorable_dump(pg_db, make_user, tmp_path):
    make_user()  # so the dump has at least one real row to round-trip
    from tests.conftest import query

    dump_path = create_backup(output_dir=tmp_path)

    assert dump_path.exists() and dump_path.stat().st_size > 0
    assert dump_path.name.startswith(pg_db)  # pg_db is the throwaway database's name; create_backup defaults to db_module.DB_NAME

    # pg_restore --list reads the archive's table of contents without needing a live connection --
    # proves this is a genuine, well-formed pg_dump custom-format archive, not just a nonempty file.
    listing = subprocess.run(["pg_restore", "--list", str(dump_path)], capture_output=True, text=True, timeout=30)
    assert listing.returncode == 0
    assert "dim_user" in listing.stdout and "fact_transactions" in listing.stdout

    # Full round trip: restore into a SEPARATE scratch database and confirm the row count matches.
    import etl.load.db as db_module
    import uuid
    restore_db = f"finflow_restore_test_{uuid.uuid4().hex[:8]}"
    admin = db_module.get_connection("postgres")
    admin.autocommit = True
    try:
        admin.cursor().execute(f"CREATE DATABASE {restore_db}")
        restore = subprocess.run(
            ["pg_restore", "--no-owner", "-h", db_module.DB_HOST, "-p", str(db_module.DB_PORT),
             "-U", db_module.DB_USER, "-d", restore_db, str(dump_path)],
            env={**os.environ, "PGPASSWORD": db_module.DB_PASSWORD}, capture_output=True, text=True, timeout=60,
        )
        assert restore.returncode == 0, restore.stderr

        original_count = query("SELECT count(*) FROM dim_user")[0][0]
        conn = db_module.get_connection(restore_db)
        try:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM dim_user")
            restored_count = cur.fetchone()[0]
        finally:
            conn.close()
        assert restored_count == original_count and original_count > 0
    finally:
        admin.cursor().execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (restore_db,),
        )
        admin.cursor().execute(f"DROP DATABASE IF EXISTS {restore_db}")
        admin.close()


@pytest.mark.integration
@pytest.mark.skipif(not pg_dump_available, reason="pg_dump/pg_restore not installed")
def test_create_backup_of_a_nonexistent_database_raises_and_leaves_no_file(pg_db, tmp_path):
    with pytest.raises(BackupError):
        create_backup(output_dir=tmp_path, dbname="this_database_does_not_exist_xyz")
    assert list(tmp_path.glob("*.dump")) == []


def test_pg_dump_not_installed_raises_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(BackupError, match="pg_dump is not installed"):
        create_backup(output_dir=tmp_path, dbname="whatever")
