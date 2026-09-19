import threading
import uuid

import pytest

import etl.load.db as db_module
from database import migrate
from database.migrate import MigrationError, discover, run_migrations, status
from tests.conftest import query


def _write(dir_path, name, sql):
    (dir_path / name).write_text(sql)


# --- discovery (no database needed) ---

def test_discover_sorts_by_filename_and_ignores_badly_named_files(tmp_path, app_log):
    _write(tmp_path, "0002_second.sql", "SELECT 1;")
    _write(tmp_path, "0001_first.sql", "SELECT 1;")
    _write(tmp_path, "notes.sql", "SELECT 1;")
    _write(tmp_path, "3_bad_prefix.sql", "SELECT 1;")
    _write(tmp_path, "readme.md", "x")

    found = [v for v, _ in discover(tmp_path)]

    assert found == ["0001_first", "0002_second"]
    assert "notes.sql" in app_log.text


def test_the_repos_own_migrations_are_all_well_formed():
    versions = [v for v, _ in discover()]
    assert versions == sorted(versions) and len(versions) >= 4
    assert len({v[:4] for v in versions}) == len(versions)   # no two migrations share a number


# --- against a real database ---

pytestmark_db = pytest.mark.integration


@pytest.fixture
def fresh_tracking(pg_db):
    conn = db_module.get_connection()
    conn.cursor().execute("DROP TABLE IF EXISTS schema_migrations")
    conn.commit()
    conn.close()
    yield


def _table_exists(name):
    return query("SELECT to_regclass(%s) IS NOT NULL", (name,))[0][0]


@pytest.mark.integration
def test_applies_pending_migrations_in_order_exactly_once(fresh_tracking, tmp_path):
    t = f"mig_{uuid.uuid4().hex[:6]}"
    _write(tmp_path, "0001_create.sql", f"CREATE TABLE {t} (id INT);")
    _write(tmp_path, "0002_insert.sql", f"INSERT INTO {t} VALUES (1);")   # only works if 0001 ran first

    assert run_migrations(tmp_path) == ["0001_create", "0002_insert"]
    assert query(f"SELECT count(*) FROM {t}") == [(1,)]

    assert run_migrations(tmp_path) == []                                 # second run: nothing to do
    assert query(f"SELECT count(*) FROM {t}") == [(1,)]                   # and nothing was applied twice
    assert query("SELECT version FROM schema_migrations ORDER BY version") == [("0001_create",), ("0002_insert",)]


@pytest.mark.integration
def test_a_new_migration_added_later_is_picked_up(fresh_tracking, tmp_path):
    t = f"mig_{uuid.uuid4().hex[:6]}"
    _write(tmp_path, "0001_create.sql", f"CREATE TABLE {t} (id INT);")
    run_migrations(tmp_path)

    _write(tmp_path, "0002_add_column.sql", f"ALTER TABLE {t} ADD COLUMN note TEXT;")
    assert run_migrations(tmp_path) == ["0002_add_column"]


@pytest.mark.integration
def test_a_failing_migration_rolls_back_only_itself_and_stops_the_run(fresh_tracking, tmp_path):
    ok, half, never = (f"mig_{uuid.uuid4().hex[:6]}" for _ in range(3))
    _write(tmp_path, "0001_ok.sql", f"CREATE TABLE {ok} (id INT);")
    _write(tmp_path, "0002_broken.sql", f"CREATE TABLE {half} (id INT);\nSELECT * FROM table_that_does_not_exist;")
    _write(tmp_path, "0003_after.sql", f"CREATE TABLE {never} (id INT);")

    with pytest.raises(MigrationError, match="0002_broken"):
        run_migrations(tmp_path)

    assert _table_exists(ok)                          # the earlier, successful migration stays applied
    assert not _table_exists(half)                    # the failed one left nothing behind (it ran in one transaction)
    assert not _table_exists(never)                   # and the run stopped there
    assert query("SELECT version FROM schema_migrations") == [("0001_ok",)]

    # fixing the broken migration lets the next run pick up exactly where it stopped
    _write(tmp_path, "0002_broken.sql", f"CREATE TABLE {half} (id INT);")
    assert run_migrations(tmp_path) == ["0002_broken", "0003_after"]


@pytest.mark.integration
def test_an_edited_applied_migration_is_flagged_but_not_rerun(fresh_tracking, tmp_path, app_log):
    t = f"mig_{uuid.uuid4().hex[:6]}"
    _write(tmp_path, "0001_create.sql", f"CREATE TABLE {t} (id INT);")
    run_migrations(tmp_path)

    _write(tmp_path, "0001_create.sql", f"CREATE TABLE {t} (id INT, sneaky TEXT);")
    assert run_migrations(tmp_path) == []

    assert "edited after it was applied" in app_log.text
    assert [r for r in status(tmp_path)] == [{"version": "0001_create", "state": "changed"}]


@pytest.mark.integration
def test_status_reports_applied_and_pending(fresh_tracking, tmp_path):
    t = f"mig_{uuid.uuid4().hex[:6]}"
    _write(tmp_path, "0001_a.sql", f"CREATE TABLE {t} (id INT);")
    run_migrations(tmp_path)
    _write(tmp_path, "0002_b.sql", f"ALTER TABLE {t} ADD COLUMN x INT;")

    assert status(tmp_path) == [{"version": "0001_a", "state": "applied"}, {"version": "0002_b", "state": "pending"}]


@pytest.mark.integration
def test_a_missing_file_for_an_applied_migration_only_warns(fresh_tracking, tmp_path, app_log):
    t = f"mig_{uuid.uuid4().hex[:6]}"
    _write(tmp_path, "0001_a.sql", f"CREATE TABLE {t} (id INT);")
    run_migrations(tmp_path)
    (tmp_path / "0001_a.sql").unlink()

    assert run_migrations(tmp_path) == []
    assert "file is missing" in app_log.text


@pytest.mark.integration
def test_two_runners_starting_together_apply_each_migration_once(fresh_tracking, tmp_path):
    """e.g. two containers booting at the same time: without the advisory lock the second would try to
    apply the same migration and fail on the duplicate."""
    t = f"mig_{uuid.uuid4().hex[:6]}"
    _write(tmp_path, "0001_slow.sql", f"SELECT pg_sleep(0.6);\nCREATE TABLE {t} (id INT);")
    results, errors = [], []

    def worker():
        try:
            results.append(run_migrations(tmp_path))
        except Exception as e:      # noqa: BLE001 - surfaced by the assertion below
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    [th.start() for th in threads]
    [th.join() for th in threads]

    assert not errors
    assert sorted(len(r) for r in results) == [0, 1]         # exactly one runner applied it
    assert query("SELECT count(*) FROM schema_migrations") == [(1,)]


@pytest.mark.integration
def test_the_real_migrations_are_idempotent_on_a_database_built_from_schema_sql(fresh_tracking):
    """pg_db was built from schema.sql (already the latest shape), so this is the 'existing database that
    already has these changes' case: everything must apply cleanly and just be recorded."""
    applied = run_migrations()
    assert applied == [v for v, _ in discover()]
    assert run_migrations() == []
    assert query("SELECT count(*) FROM information_schema.columns WHERE table_name = 'dim_account' AND column_name = 'opening_balance'") == [(1,)]


@pytest.mark.integration
def test_setup_database_builds_a_fresh_database_and_records_its_migrations(pg_db, monkeypatch):
    from database.seed_data import setup_database
    name = f"finflow_setup_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(db_module, "DB_NAME", name)
    try:
        setup_database()
        assert query("SELECT version FROM schema_migrations ORDER BY version") == [(v,) for v, _ in discover()]
        assert _table_exists("fact_transactions")
        setup_database()       # running it again is harmless
        assert query("SELECT count(*) FROM schema_migrations") == [(len(discover()),)]
    finally:
        monkeypatch.undo()
        admin = db_module.get_connection("postgres")
        admin.autocommit = True
        admin.cursor().execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()


def test_cli_status_prints_a_line_per_migration(pg_db, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["migrate.py", "--status"])
    migrate.main()
    out = capsys.readouterr().out
    assert all(v in out for v, _ in discover())


@pytest.mark.integration
def test_seed_waits_only_for_the_server_not_for_the_target_database(pg_db, monkeypatch):
    """Regression: on a fresh Postgres the target database doesn't exist yet (setup_database creates it), yet
    the seed script's readiness check insisted on connecting to it and gave up after a minute."""
    from scripts.seed_demo import _wait_for_db
    monkeypatch.setattr(db_module, "DB_NAME", f"finflow_does_not_exist_{uuid.uuid4().hex[:8]}")
    _wait_for_db(max_attempts=1, delay_seconds=0)          # returns instead of raising


@pytest.mark.integration
def test_seed_still_fails_when_the_server_itself_is_unreachable(monkeypatch):
    from scripts.seed_demo import _wait_for_db
    monkeypatch.setattr(db_module, "DB_PORT", "1")           # nothing listens here
    with pytest.raises(RuntimeError, match="never became ready"):
        _wait_for_db(max_attempts=1, delay_seconds=0)
