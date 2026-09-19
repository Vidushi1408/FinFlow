"""
Importing bank statements against a real database: one shared account per user,
no duplicates on re-upload/overlap, and balances that come from the statement
(or zero) -- never an invented starting number.
"""
import io

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import etl.load.db as db_module
import webapp.app as app_module
import webapp.auth as auth_module
from tests.conftest import query

pytestmark = pytest.mark.integration

HEADER = "Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
AUG = (
    HEADER +
    "01/08/2026,ZOMATO,250.00,,9750.00\n"
    "02/08/2026,SALARY CREDIT ACME,,5000.00,14750.00\n"
    "03/08/2026,UBER INDIA,150.00,,14600.00\n"
)
# overlaps AUG on 03/08 and continues to 05/08
AUG_OVERLAP = (
    HEADER +
    "03/08/2026,UBER INDIA,150.00,,14600.00\n"
    "04/08/2026,SWIGGY,100.00,,14500.00\n"
    "05/08/2026,AMAZON,500.00,,14000.00\n"
)
# comes before AUG
JULY = (
    HEADER +
    "28/07/2026,BIGBASKET,300.00,,7700.00\n"
    "30/07/2026,UBER INDIA,50.00,,7650.00\n"
    "31/07/2026,SALARY CREDIT ACME,,2350.00,10000.00\n"
)
NO_BALANCE = "Date,Description,Amount\n2026-08-01,ZOMATO,-250\n2026-08-02,SALARY,5000\n"


@pytest.fixture
def import_as(pg_db, make_user, monkeypatch):
    engine = create_engine(f"postgresql://{db_module.DB_USER}:{db_module.DB_PASSWORD}@{db_module.DB_HOST}:{db_module.DB_PORT}/{pg_db}")
    monkeypatch.setattr(app_module, "SessionLocal", sessionmaker(bind=engine))
    users = {}
    monkeypatch.setattr(auth_module.login_manager, "_user_callback", lambda uid: users.get(uid))
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def new_user():
        user_id, _ = make_user(with_account=False)
        users[user_id] = auth_module.User(user_id, "T", f"{user_id}@example.com", "INR", 0.8)
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = user_id
            sess["_fresh"] = True

        def send(path, csv_text, **extra):
            data = {"file": (io.BytesIO(csv_text.encode()), "statement.csv"), **extra}
            return client.post(f"/api/v1/transactions{path}", data=data, content_type="multipart/form-data")

        return user_id, send

    yield new_user
    engine.dispose()


def _accounts(user_id):
    return [r[0] for r in query("SELECT account_id FROM dim_account WHERE user_id = %s AND institution = 'UPLOADED_STATEMENT'", (user_id,))]


def _count(account_id):
    return query("SELECT count(*) FROM fact_transactions WHERE account_id = %s", (account_id,))[0][0]


def _latest_balance(account_id):
    return float(query("SELECT closing_balance FROM fact_account_balance WHERE account_id = %s ORDER BY date_id DESC LIMIT 1", (account_id,))[0][0])


def test_first_import_uses_the_statements_own_balance_not_an_invented_one(import_as):
    user, send = import_as()
    body = send("/upload", AUG).get_json()

    assert body["transactions_added"] == 3 and body["duplicates_skipped"] == 0
    assert body["opening_balance"] == 10000.0 and body["opening_balance_source"] == "statement"
    (account,) = _accounts(user)
    assert _latest_balance(account) == 14600.0  # the statement's own closing balance, not 50000 + net


def test_reuploading_the_same_statement_adds_nothing(import_as):
    user, send = import_as()
    send("/upload", AUG)
    second = send("/upload", AUG).get_json()

    assert second["transactions_added"] == 0 and second["duplicates_skipped"] == 3
    assert second["existing_account"] is True
    (account,) = _accounts(user)             # still ONE account, not two
    assert _count(account) == 3
    assert _latest_balance(account) == 14600.0


def test_overlapping_statement_adds_only_the_new_rows_and_balance_stays_right(import_as):
    user, send = import_as()
    send("/upload", AUG)
    body = send("/upload", AUG_OVERLAP).get_json()

    assert body["transactions_added"] == 2 and body["duplicates_skipped"] == 1
    (account,) = _accounts(user)
    assert _count(account) == 5
    assert _latest_balance(account) == 14000.0  # matches the second statement's closing balance


def test_an_earlier_statement_imported_later_re_anchors_the_opening_balance(import_as):
    user, send = import_as()
    send("/upload", AUG)
    body = send("/upload", JULY).get_json()

    assert body["transactions_added"] == 3
    (account,) = _accounts(user)
    assert float(query("SELECT opening_balance FROM dim_account WHERE account_id = %s", (account,))[0][0]) == 8000.0  # 7700 + 300
    assert _latest_balance(account) == 14600.0  # history now runs July -> August consistently


def test_a_later_statements_opening_balance_is_not_applied_over_existing_history(import_as):
    user, send = import_as()
    send("/upload", AUG)
    body = send("/upload", AUG_OVERLAP, opening_balance="99999").get_json()

    (account,) = _accounts(user)
    assert float(query("SELECT opening_balance FROM dim_account WHERE account_id = %s", (account,))[0][0]) == 10000.0
    assert any("opening balance was not used" in i["message"] for i in body["issues"])


def test_repeated_identical_rows_are_all_kept_and_not_duplicated_on_reimport(import_as):
    user, send = import_as()
    chai = HEADER + "01/08/2026,CHAI POINT,50.00,,9950.00\n01/08/2026,CHAI POINT,50.00,,9900.00\n01/08/2026,CHAI POINT,50.00,,9850.00\n"

    first = send("/upload", chai).get_json()
    again = send("/upload", chai).get_json()

    assert first["transactions_added"] == 3        # three real purchases, not collapsed into one
    assert again["transactions_added"] == 0 and again["duplicates_skipped"] == 3
    (account,) = _accounts(user)
    assert _count(account) == 3 and _latest_balance(account) == 9850.0


def test_a_recategorized_transaction_survives_a_reimport(import_as):
    user, send = import_as()
    send("/upload", AUG)
    (account,) = _accounts(user)
    query("SELECT 1")  # (keep helper import used)
    conn = db_module.get_connection()
    conn.cursor().execute("UPDATE fact_transactions SET category_id = 'SHOPPING' WHERE account_id = %s AND merchant_id = 'ZOMATO'", (account,))
    conn.commit()
    conn.close()

    send("/upload", AUG)

    assert query("SELECT category_id FROM fact_transactions WHERE account_id = %s AND merchant_id = 'ZOMATO'", (account,)) == [("SHOPPING",)]


def test_without_a_balance_column_balances_start_from_zero_or_what_the_user_says(import_as):
    zero_user, send_zero = import_as()
    body = send_zero("/upload", NO_BALANCE).get_json()
    assert body["opening_balance_source"] == "unknown"
    assert _latest_balance(_accounts(zero_user)[0]) == 4750.0     # net change only: -250 + 5000

    told_user, send_told = import_as()
    body = send_told("/upload", NO_BALANCE, opening_balance="20,000").get_json()
    assert body["opening_balance_source"] == "user"
    assert _latest_balance(_accounts(told_user)[0]) == 24750.0


def test_invalid_opening_balance_is_rejected(import_as):
    _, send = import_as()
    assert send("/upload", NO_BALANCE, opening_balance="lots").status_code == 400
    assert send("/upload/preview", NO_BALANCE, opening_balance="lots").status_code == 400


def test_users_never_share_an_account_or_dedupe_against_each_other(import_as):
    alice, send_a = import_as()
    bob, send_b = import_as()
    send_a("/upload", AUG)
    body = send_b("/upload", AUG).get_json()

    assert body["transactions_added"] == 3 and body["duplicates_skipped"] == 0
    assert _accounts(alice) != _accounts(bob) and len(_accounts(alice)) == len(_accounts(bob)) == 1


def test_preview_reports_duplicates_and_opening_balance_and_writes_nothing(import_as):
    user, send = import_as()
    send("/upload", AUG)
    (account,) = _accounts(user)

    body = send("/upload/preview", AUG_OVERLAP).get_json()

    assert body["summary"]["already_imported"] == 1 and body["summary"]["new_rows"] == 2
    assert body["account"]["is_new"] is False
    assert body["opening_balance"]["source"] == "existing_account" and body["opening_balance"]["editable"] is False
    assert _count(account) == 3                      # preview saved nothing


def test_preview_of_a_first_import_explains_where_the_opening_balance_comes_from(import_as):
    _, send = import_as()
    with_balance = send("/upload/preview", AUG).get_json()
    assert with_balance["opening_balance"] == {"value": 10000.0, "source": "statement", "editable": False}
    assert with_balance["account"]["is_new"] is True

    without = send("/upload/preview", NO_BALANCE).get_json()
    assert without["opening_balance"] == {"value": None, "source": "unknown", "editable": True}


def test_importing_never_overwrites_shared_reference_data(import_as, pg_db):
    conn = db_module.get_connection()
    conn.cursor().execute("UPDATE dim_category SET budget_amount = 12345 WHERE category_id = 'FOOD'")
    conn.commit()
    conn.close()

    _, send = import_as()
    send("/upload", AUG)   # ZOMATO -> FOOD

    assert float(query("SELECT budget_amount FROM dim_category WHERE category_id = 'FOOD'")[0][0]) == 12345.0


def test_imported_rows_are_scored_in_the_database_and_is_fraud_stays_false(import_as, monkeypatch):
    import pandas as pd
    import webapp.routes.upload as upload_module
    monkeypatch.setattr(upload_module, "predict_fraud", lambda df, **kw: pd.DataFrame(
        {"fraud_score": -0.3, "fraud_probability": 0.91, "is_flagged": True}, index=df.index))
    user, send = import_as()

    body = send("/upload", AUG).get_json()

    (account,) = _accounts(user)
    rows = query("SELECT fraud_score, scored_at, is_fraud FROM fact_transactions WHERE account_id = %s", (account,))
    assert len(rows) == 3 and body["fraud_alerts_generated"] == 3
    assert all(float(score) == 0.91 and scored_at is not None and is_fraud is False for score, scored_at, is_fraud in rows)
