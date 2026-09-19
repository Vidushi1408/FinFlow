"""
End-to-end route tests against a real (throwaway) PostgreSQL database. The
mocked-session tests in test_flask.py can't catch broken SQL or, more
importantly, one user seeing another user's data -- these can.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import etl.load.db as db_module
import webapp.app as app_module
import webapp.auth as auth_module
from webapp.services.demo_seed import seed_demo_data_for_user
from tests.conftest import query

pytestmark = pytest.mark.integration

THIS_MONTH = datetime.now().strftime("%Y-%m")


@pytest.fixture(scope="module")
def world(pg_db):
    """Two users, each with their own seeded demo account."""
    import uuid
    users = {}
    conn = db_module.get_connection()
    try:
        cur = conn.cursor()
        for label, persona in (("alice", "salaried"), ("bob", "student")):
            uid = str(uuid.uuid4())
            cur.execute("INSERT INTO dim_user (user_id, name, email, password_hash, onboarded_at) VALUES (%s,%s,%s,'x', now())",
                        (uid, label.title(), f"{label}-{uid[:6]}@example.com"))
            users[label] = {"id": uid, "persona": persona}
        conn.commit()
    finally:
        conn.close()
    for label, u in users.items():
        u["account_id"] = seed_demo_data_for_user(u["id"], u["persona"], history_months=4)
    return users


@pytest.fixture
def make_client(pg_db, world, monkeypatch):
    url = f"postgresql://{db_module.DB_USER}:{db_module.DB_PASSWORD}@{db_module.DB_HOST}:{db_module.DB_PORT}/{pg_db}"
    engine = create_engine(url)
    # Real before_request/teardown run unchanged -- only the session factory is redirected.
    monkeypatch.setattr(app_module, "SessionLocal", sessionmaker(bind=engine))

    by_id = {u["id"]: auth_module.User(u["id"], label.title(), f"{label}@example.com", "INR", 0.8) for label, u in world.items()}
    monkeypatch.setattr(auth_module.login_manager, "_user_callback", lambda uid: by_id.get(uid))

    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def _make(label):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = world[label]["id"]
            sess["_fresh"] = True
        return client

    yield _make
    engine.dispose()


def test_dashboard_summary_reflects_real_balances(make_client, world):
    data = make_client("alice").get("/api/v1/dashboard/summary").get_json()
    assert "current_balance" in data and "this_month" in data
    expected = query(
        "SELECT closing_balance FROM fact_account_balance WHERE account_id = %s ORDER BY date_id DESC LIMIT 1",
        (world["alice"]["account_id"],))[0][0]
    assert data["current_balance"] == pytest.approx(float(expected))


def test_transactions_list_only_returns_own_data(make_client, world):
    alice = make_client("alice").get("/api/v1/transactions?page_size=200").get_json()
    bob_ids = {r[0] for r in query("SELECT transaction_id FROM fact_transactions WHERE account_id = %s", (world["bob"]["account_id"],))}

    assert alice["total"] > 0
    assert not ({t["transaction_id"] for t in alice["transactions"]} & bob_ids)


def test_transactions_filters_and_pagination(make_client):
    client = make_client("alice")
    page1 = client.get("/api/v1/transactions?page=1&page_size=5").get_json()
    assert len(page1["transactions"]) == 5
    debits = client.get("/api/v1/transactions?type=DEBIT&page_size=200").get_json()
    assert debits["transactions"] and all(t["transaction_type"] == "DEBIT" for t in debits["transactions"])
    assert client.get("/api/v1/transactions?type=BOGUS").status_code == 400
    small = client.get("/api/v1/transactions?amount_max=100&page_size=200").get_json()
    assert all(t["amount"] <= 100 for t in small["transactions"])


def test_cannot_filter_by_another_users_account(make_client, world):
    response = make_client("alice").get(f"/api/v1/transactions?account_id={world['bob']['account_id']}")
    assert response.status_code == 400


def test_cannot_recategorize_another_users_transaction(make_client, world):
    bob_tx = query("SELECT transaction_id FROM fact_transactions WHERE account_id = %s LIMIT 1", (world["bob"]["account_id"],))[0][0]
    response = make_client("alice").patch(f"/api/v1/transactions/{bob_tx}/category", json={"category_id": "FOOD"})
    assert response.status_code == 404


def test_recategorize_creates_rule(make_client, world):
    tx_id, merchant = query("SELECT transaction_id, merchant_id FROM fact_transactions WHERE account_id = %s LIMIT 1",
                            (world["alice"]["account_id"],))[0]
    response = make_client("alice").patch(f"/api/v1/transactions/{tx_id}/category", json={"category_id": "OTHER", "create_rule": True})
    assert response.status_code == 200
    assert query("SELECT category_id FROM category_rule WHERE user_id = %s AND merchant_id = %s", (world["alice"]["id"], merchant)) == [("OTHER",)]
    assert query("SELECT category_id FROM fact_transactions WHERE transaction_id = %s", (tx_id,)) == [("OTHER",)]


def test_goals_crud_and_isolation(make_client):
    alice, bob = make_client("alice"), make_client("bob")
    created = alice.post("/api/v1/goals", json={"name": "Emergency fund", "target_amount": 50000, "deadline": "2030-01-01"})
    assert created.status_code in (200, 201)

    names = [g["name"] for g in alice.get("/api/v1/goals").get_json()["goals"]]
    assert "Emergency fund" in names
    assert "Emergency fund" not in [g["name"] for g in bob.get("/api/v1/goals").get_json()["goals"]]

    goal_id = next(g["goal_id"] for g in alice.get("/api/v1/goals").get_json()["goals"] if g["name"] == "Emergency fund")
    assert bob.delete(f"/api/v1/goals/{goal_id}").status_code in (403, 404)  # bob can't delete alice's goal
    assert alice.delete(f"/api/v1/goals/{goal_id}").status_code == 200
    assert "Emergency fund" not in [g["name"] for g in alice.get("/api/v1/goals").get_json()["goals"]]


def test_monthly_report_json_csv_and_pdf(make_client):
    client = make_client("alice")
    report = client.get(f"/api/v1/reports/monthly?month={THIS_MONTH}").get_json()
    assert report["month"] == THIS_MONTH
    assert {"total_spent", "categories", "subscription_count", "goals"} <= report.keys()

    csv_response = client.get(f"/api/v1/reports/monthly/export.csv?month={THIS_MONTH}")
    assert csv_response.status_code == 200 and csv_response.mimetype == "text/csv"
    assert csv_response.data.decode().splitlines()[0] == "date,merchant,category,type,amount"

    pdf_response = client.get(f"/api/v1/reports/monthly/export.pdf?month={THIS_MONTH}")
    assert pdf_response.status_code == 200 and pdf_response.data.startswith(b"%PDF")

    assert client.get("/api/v1/reports/monthly?month=garbage").status_code == 400


def test_spending_budgets_and_subscriptions_endpoints_run_real_sql(make_client):
    client = make_client("alice")
    summary = client.get(f"/api/v1/spending/summary?month={THIS_MONTH}")
    assert summary.status_code == 200 and "categories" in summary.get_json()
    assert client.get("/api/v1/spending/summary").status_code == 400  # month is required
    assert client.get("/api/v1/spending/summary?month=2026-9").status_code == 400
    trends = client.get("/api/v1/spending/trends?days=30")
    assert trends.status_code == 200 and trends.get_json()["trends"]
    assert client.get("/api/v1/spending/trends?days=9999").status_code == 400
    budgets = client.get("/api/v1/budgets")
    assert budgets.status_code == 200 and budgets.get_json()["budgets"]
    assert client.get("/api/v1/insights/subscriptions").status_code == 200


def test_budget_limit_is_saved_per_user(make_client):
    alice, bob = make_client("alice"), make_client("bob")
    assert alice.post("/api/v1/budgets", json={"category_id": "FOOD", "monthly_limit": 1234}).status_code == 200
    def food_limit(client):
        return next(b["monthly_limit"] for b in client.get("/api/v1/budgets").get_json()["budgets"] if b["category_id"] == "FOOD")

    assert food_limit(alice) == 1234
    assert food_limit(bob) != 1234


def test_settings_accounts_lists_only_own_masked_accounts(make_client, world):
    accounts = make_client("alice").get("/api/v1/settings/accounts").get_json()["accounts"]
    assert [a["account_id"] for a in accounts] == [world["alice"]["account_id"]]
    assert accounts[0]["account_number_masked"].startswith("••••")


# --- category rules keyed by payee ---

def _insert_upi_tx(account_id, merchant_id, amount=100.0, category="UPI"):
    import uuid
    conn = db_module.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO dim_merchant (merchant_id, merchant_name, category, mcc_code) VALUES (%s,%s,%s,'0000') ON CONFLICT DO NOTHING",
                    (merchant_id, merchant_id, category))
        tx_id = uuid.uuid4().hex
        cur.execute(
            "INSERT INTO fact_transactions (transaction_id, account_id, merchant_id, category_id, date_id, transaction_ts, amount, transaction_type) "
            "VALUES (%s,%s,%s,%s,20260801,'2026-08-01 10:00:00',%s,'DEBIT')",
            (tx_id, account_id, merchant_id, category, amount))
        conn.commit()
        return tx_id
    finally:
        conn.close()


def test_rule_covers_every_payment_to_the_same_payee_but_only_for_that_user(make_client, world):
    alice_acc, bob_acc = world["alice"]["account_id"], world["bob"]["account_id"]
    first = _insert_upi_tx(alice_acc, "UPI-KIRAN STORES-KIRAN@OKSBI-SBIN001-100000000001")
    second = _insert_upi_tx(alice_acc, "UPI-KIRAN STORES-KIRAN@OKSBI-SBIN001-100000000002")
    other_payee = _insert_upi_tx(alice_acc, "UPI-SOMEONE ELSE-SOME@OKSBI-SBIN001-100000000003")
    bobs_same_payee = _insert_upi_tx(bob_acc, "UPI-KIRAN STORES-KIRAN@OKSBI-SBIN001-100000000004")

    response = make_client("alice").patch(f"/api/v1/transactions/{first}/category", json={"category_id": "GROCERY", "create_rule": True})

    assert response.status_code == 200
    assert response.get_json()["transactions_updated"] == 2
    category = lambda tx: query("SELECT category_id FROM fact_transactions WHERE transaction_id = %s", (tx,))[0][0]  # noqa: E731
    assert category(first) == category(second) == "GROCERY"
    assert category(other_payee) == "UPI"          # different payee untouched
    assert category(bobs_same_payee) == "UPI"      # other user untouched
    assert query("SELECT merchant_id FROM category_rule WHERE user_id = %s AND category_id = 'GROCERY'", (world["alice"]["id"],)) == [("KIRAN STORES",)]


def test_without_create_rule_only_the_one_transaction_changes(make_client, world):
    acc = world["alice"]["account_id"]
    one = _insert_upi_tx(acc, "UPI-PRIYA NAIR-PRIYA@OKSBI-SBIN001-200000000001")
    two = _insert_upi_tx(acc, "UPI-PRIYA NAIR-PRIYA@OKSBI-SBIN001-200000000002")

    response = make_client("alice").patch(f"/api/v1/transactions/{one}/category", json={"category_id": "FOOD"})

    assert response.get_json()["transactions_updated"] == 1
    assert query("SELECT category_id FROM fact_transactions WHERE transaction_id = %s", (one,)) == [("FOOD",)]
    assert query("SELECT category_id FROM fact_transactions WHERE transaction_id = %s", (two,)) == [("UPI",)]


def test_saved_rule_is_applied_to_the_next_upload_preview(make_client, world):
    import io
    tx = _insert_upi_tx(world["alice"]["account_id"], "UPI-ANITA RAO-ANITA@OKSBI-SBIN0001234-300000000001")
    client = make_client("alice")
    assert client.patch(f"/api/v1/transactions/{tx}/category", json={"category_id": "SHOPPING", "create_rule": True}).status_code == 200

    csv = b"Date,Description,Amount\n2026-09-01,UPI-ANITA RAO-ANITA@OKSBI-SBIN0001234-999999999999,-500\n"
    body = client.post("/api/v1/transactions/upload/preview", data={"file": (io.BytesIO(csv), "s.csv")},
                       content_type="multipart/form-data").get_json()

    assert body["preview"][0]["category_id"] == "SHOPPING"  # a different reference number, same payee
