import pytest
from flask import g
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import etl.load.db as db_module
from webapp.app import app
from webapp.routes.settings import _delete_all_user_data
from webapp.services.demo_seed import seed_demo_data_for_user
from tests.conftest import query

pytestmark = pytest.mark.integration


@pytest.fixture
def session(pg_db):
    # Deliberately NOT webapp.database.SessionLocal: that engine is bound to the
    # real database at import time, and these tests delete data.
    url = (f"postgresql://{db_module.DB_USER}:{db_module.DB_PASSWORD}@"
           f"{db_module.DB_HOST}:{db_module.DB_PORT}/{pg_db}")
    engine = create_engine(url)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def test_seed_demo_data_creates_full_account(make_user):
    user_id, _ = make_user(with_account=False)

    account_id = seed_demo_data_for_user(user_id, "salaried", history_months=3)

    assert query("SELECT user_id::text FROM dim_account WHERE account_id = %s", (account_id,)) == [(user_id,)]
    tx_count, unknown = query(
        "SELECT count(*), count(*) FILTER (WHERE category_id = 'UNKNOWN') FROM fact_transactions WHERE account_id = %s",
        (account_id,))[0]
    assert tx_count > 0 and unknown == 0
    assert query("SELECT count(*) FROM fact_account_balance WHERE account_id = %s", (account_id,))[0][0] > 0
    assert query("SELECT count(*) FROM fact_transactions WHERE account_id = %s AND transaction_type = 'CREDIT'", (account_id,))[0][0] > 0


def test_seed_demo_data_is_scoped_to_the_requested_user(make_user):
    user_a, _ = make_user(with_account=False)
    user_b, _ = make_user(with_account=False)

    seed_demo_data_for_user(user_a, "student", history_months=2)

    assert query("SELECT count(*) FROM dim_account WHERE user_id = %s", (user_a,)) == [(1,)]
    assert query("SELECT count(*) FROM dim_account WHERE user_id = %s", (user_b,)) == [(0,)]


def test_delete_all_user_data_removes_only_that_users_records(make_user, session):
    victim, _ = make_user(with_account=False)
    bystander, _ = make_user(with_account=False)
    victim_account = seed_demo_data_for_user(victim, "salaried", history_months=2)
    bystander_account = seed_demo_data_for_user(bystander, "student", history_months=2)

    with app.test_request_context():
        g.db = session
        _delete_all_user_data(victim)

    assert query("SELECT count(*) FROM dim_account WHERE account_id = %s", (victim_account,)) == [(0,)]
    assert query("SELECT count(*) FROM fact_transactions WHERE account_id = %s", (victim_account,)) == [(0,)]
    assert query("SELECT count(*) FROM fact_account_balance WHERE account_id = %s", (victim_account,)) == [(0,)]
    # the login itself survives so the user can start over
    assert query("SELECT count(*) FROM dim_user WHERE user_id = %s", (victim,)) == [(1,)]
    # ...and nobody else was touched
    assert query("SELECT count(*) FROM dim_account WHERE account_id = %s", (bystander_account,)) == [(1,)]
    assert query("SELECT count(*) FROM fact_transactions WHERE account_id = %s", (bystander_account,))[0][0] > 0
