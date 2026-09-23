import pytest
from unittest.mock import MagicMock
from webapp.app import app
import webapp.auth as auth_module
import io

FAKE_USER_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def client():
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    with app.test_client() as client:
        yield client


@pytest.fixture(autouse=True)
def mock_db(monkeypatch):
    """
    Mock the database connection attached to Flask `g`.
    We patch the before_request to inject a mock session.
    """
    mock_session = MagicMock()

    # Mock behavior for /api/spending/summary (and reused generically elsewhere)
    mock_session.execute.return_value.fetchall.return_value = [
        ("FOOD", 15000.0, 45),
        ("ENTERTAINMENT", 5000.0, 10)
    ]
    # Mock behavior for the /api/what-if/budget current-balance lookup
    mock_session.execute.return_value.scalar.return_value = 50000.0

    # Override before_request to set g.db to our mock
    original_before_request = app.before_request_funcs.get(None, [])

    def override_before_request():
        from flask import g
        g.db = mock_session

    app.before_request_funcs[None] = [override_before_request]

    yield mock_session

    # Restore
    app.before_request_funcs[None] = original_before_request


@pytest.fixture(autouse=True)
def no_existing_statement_account(monkeypatch):
    """The MagicMock session can't answer the statement-account queries; these tests treat every user as
    having no imported account yet. The real queries are exercised in test_integration_statements.py."""
    import webapp.routes.upload as upload_module
    service = upload_module.statement_account
    monkeypatch.setattr(service, "find_statement_account", lambda db, uid: None)
    monkeypatch.setattr(service, "earliest_transaction_date", lambda db, aid: None)
    monkeypatch.setattr(service, "existing_transaction_ids", lambda db, ids: set())
    monkeypatch.setattr(service, "set_opening_balance", lambda db, aid, ob: None)
    monkeypatch.setattr(service, "rebuild_balances", lambda db, aid: None)


@pytest.fixture
def fake_user():
    return auth_module.User(FAKE_USER_ID, "Test User", "test@example.com", "INR", 0.8)


@pytest.fixture
def auth_client(client, fake_user, monkeypatch):
    """A test client with a logged-in user, bypassing the real DB-backed user_loader."""
    monkeypatch.setattr(auth_module.login_manager, "_user_callback", lambda uid: fake_user)
    with client.session_transaction() as sess:
        sess['_user_id'] = fake_user.id
        sess['_fresh'] = True
    return client


def test_landing_page_when_logged_out(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Where did my money go?" in response.data


def test_dashboard_requires_login(client):
    response = client.get("/transactions")
    assert response.status_code == 302  # redirected to login


def test_dashboard_page_when_logged_in(auth_client):
    response = auth_client.get("/")
    assert response.status_code == 200
    assert b"Dashboard" in response.data


def test_spending_summary(auth_client):
    response = auth_client.get("/api/v1/spending/summary?month=2026-08")
    assert response.status_code == 200
    data = response.get_json()
    assert data["month"] == "2026-08"
    assert data["total_spent"] == 20000.0
    assert data["transaction_count"] == 55
    assert len(data["categories"]) == 2
    assert data["categories"][0]["category_id"] == "FOOD"


def test_spending_summary_invalid_month(auth_client):
    response = auth_client.get("/api/v1/spending/summary?month=invalid")
    assert response.status_code == 400


def test_whatif_budget(auth_client):
    payload = {
        "reduce_categories": {"FOOD": 0.5},
        "add_savings": 500
    }
    response = auth_client.post("/api/v1/what-if/budget", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data["monthly_savings_impact"] == 8000.0
    assert data["projected_balance_6_months"] == 98000.0


def test_whatif_budget_rejects_unknown_category_key(auth_client):
    """Regression test for the SQL injection fix: an unknown/malicious category
    key must be rejected with 400, not interpolated into a query."""
    payload = {
        "reduce_categories": {"FOOD'; DROP TABLE fact_transactions; --": 0.5},
        "add_savings": 0
    }
    response = auth_client.post("/api/v1/what-if/budget", json=payload)
    assert response.status_code == 400
    data = response.get_json()
    assert "error" in data


def test_whatif_budget_requires_login(client):
    response = client.post("/api/v1/what-if/budget", json={"add_savings": 0})
    assert response.status_code == 302


def test_upload_transactions(auth_client, monkeypatch):
    # We need to mock the load_data and process_pipeline functions to avoid hitting the real DB
    import webapp.routes.upload as upload_module

    monkeypatch.setattr(upload_module, "load_data", MagicMock())

    csv_content = b"Date,Description,Amount,Type\n2026-09-01,ZOMATO,500,DEBIT\n2026-09-02,SALARY,50000,CREDIT"
    data = {'file': (io.BytesIO(csv_content), 'statement.csv')}

    response = auth_client.post("/api/v1/transactions/upload", data=data, content_type='multipart/form-data')

    assert response.status_code == 200
    data = response.get_json()
    assert data["message"] == "Upload successful"
    assert data["transactions_processed"] == 2
    assert "account_id" in data


def test_upload_requires_login(client):
    csv_content = b"Date,Description,Amount,Type\n2026-09-01,ZOMATO,500,DEBIT"
    data = {'file': (io.BytesIO(csv_content), 'statement.csv')}
    response = client.post("/api/v1/transactions/upload", data=data, content_type='multipart/form-data')
    assert response.status_code == 302


def test_update_category_rejects_transaction_not_owned_by_user(auth_client, mock_db):
    """A transaction belonging to another user's account must not be editable
    (user-data-isolation regression): the ownership lookup returns nothing."""
    # First execute() call validates the category exists (truthy); second checks
    # transaction ownership scoped to the current user's accounts (returns None).
    mock_db.execute.return_value.fetchone.side_effect = [("FOOD",), None]

    response = auth_client.patch(
        "/api/v1/transactions/someone-elses-transaction/category",
        json={"category_id": "FOOD"}
    )
    assert response.status_code == 404


def test_csrf_protection_is_registered_on_every_form():
    """Signup/login forms must carry a CSRF token (Flask-WTF's CSRFProtect is
    active app-wide), so a submission without one is rejected."""
    from flask_wtf.csrf import CSRFProtect
    assert isinstance(app.extensions.get('csrf'), CSRFProtect)


def test_goals_endpoint_requires_login(client):
    response = client.get("/api/v1/goals")
    assert response.status_code == 302


def test_create_goal_rejects_past_deadline(auth_client, mock_db):
    mock_db.execute.return_value.fetchall.return_value = []  # no accounts -> avg savings 0
    response = auth_client.post("/api/v1/goals", json={
        "name": "Too late", "target_amount": 1000, "deadline": "2020-01-01"
    })
    assert response.status_code == 400
    assert "error" in response.get_json()


def test_reports_monthly_requires_login(client):
    response = client.get("/api/v1/reports/monthly")
    assert response.status_code == 302


def test_reports_monthly_rejects_bad_month(auth_client):
    response = auth_client.get("/api/v1/reports/monthly?month=not-a-month")
    assert response.status_code == 400


def test_reports_export_csv_returns_csv(auth_client, mock_db):
    mock_db.execute.return_value.fetchall.return_value = []
    response = auth_client.get("/api/v1/reports/monthly/export.csv?month=2026-09")
    assert response.status_code == 200
    assert response.content_type.startswith("text/csv")
    assert b"date,merchant,category,type,amount" in response.data


def test_reports_export_pdf_returns_pdf(auth_client, mock_db):
    mock_db.execute.return_value.fetchall.return_value = []
    response = auth_client.get("/api/v1/reports/monthly/export.pdf?month=2026-09")
    assert response.status_code == 200
    assert response.content_type == "application/pdf"
    assert response.data[:4] == b"%PDF"


# --- Budgets ---

def test_list_budgets_requires_login(client):
    response = client.get("/api/v1/budgets")
    assert response.status_code == 302


def test_list_budgets_returns_status_per_category(auth_client, mock_db):
    mock_db.execute.return_value.fetchall.side_effect = [
        [("acc-1",)],                 # user_account_ids
        [("FOOD", 5000.0)],           # user_category_budget limits
        [("FOOD", 6000.0)],           # this-month actuals
        [("FOOD", "Food & Dining")],  # dim_category
    ]
    response = auth_client.get("/api/v1/budgets")
    assert response.status_code == 200
    data = response.get_json()
    budget = data["budgets"][0]
    assert budget["category_id"] == "FOOD"
    assert budget["monthly_limit"] == 5000.0
    assert budget["spent_this_month"] == 6000.0
    assert budget["status"] == "red"  # spent > limit


def test_set_budget_rejects_negative_limit(auth_client):
    response = auth_client.post("/api/v1/budgets", json={"category_id": "FOOD", "monthly_limit": -5})
    assert response.status_code == 400


def test_set_budget_rejects_unknown_category(auth_client, mock_db):
    mock_db.execute.return_value.fetchone.return_value = None
    response = auth_client.post("/api/v1/budgets", json={"category_id": "NOT_REAL", "monthly_limit": 100})
    assert response.status_code == 400


def test_set_budget_saves_valid_limit(auth_client, mock_db):
    mock_db.execute.return_value.fetchone.return_value = ("FOOD",)
    response = auth_client.post("/api/v1/budgets", json={"category_id": "FOOD", "monthly_limit": 5000})
    assert response.status_code == 200
    assert response.get_json()["monthly_limit"] == 5000


# --- Transactions list/filter ---

def test_list_transactions_requires_login(client):
    response = client.get("/api/v1/transactions")
    assert response.status_code == 302


def test_list_transactions_returns_rows(auth_client, mock_db):
    mock_db.execute.return_value.fetchall.side_effect = [
        [("acc-1",)],  # user_account_ids
        [("tx-1", "acc-1", "ZOMATO", "FOOD", 250.0, "DEBIT", "2026-09-01 10:00:00", False, False, None)],
    ]
    mock_db.execute.return_value.scalar.return_value = 1
    response = auth_client.get("/api/v1/transactions?page=1&page_size=10")
    assert response.status_code == 200
    data = response.get_json()
    assert data["total"] == 1
    assert data["transactions"][0]["merchant_id"] == "ZOMATO"
    assert data["transactions"][0]["fraud_score"] is None and data["transactions"][0]["is_flagged"] is False


def test_transactions_are_flagged_by_score_against_the_users_sensitivity_or_by_ground_truth_label(auth_client, mock_db):
    """fake_user.alert_sensitivity is 0.8, the same threshold the Alerts page applies to the probability."""
    def row(tx, is_fraud, score):
        return (tx, "acc-1", "M", "FOOD", 10.0, "DEBIT", "2026-09-01 10:00:00", is_fraud, False, score)
    mock_db.execute.return_value.fetchall.side_effect = [
        [("acc-1",)],
        [row("high", False, 0.95), row("edge", False, 0.80), row("low", False, 0.79), row("labelled", True, None), row("unscored", False, None)],
    ]
    mock_db.execute.return_value.scalar.return_value = 5
    flags = {t["transaction_id"]: t["is_flagged"] for t in auth_client.get("/api/v1/transactions").get_json()["transactions"]}
    assert flags == {"high": True, "edge": True, "low": False, "labelled": True, "unscored": False}


def test_list_transactions_rejects_invalid_type(auth_client):
    response = auth_client.get("/api/v1/transactions?type=NOT_A_TYPE")
    assert response.status_code == 400


# --- Settings ---

def test_update_profile_requires_login(client):
    response = client.post("/api/v1/settings/profile", json={"name": "X"})
    assert response.status_code == 302


def test_update_profile_rejects_bad_sensitivity(auth_client):
    response = auth_client.post("/api/v1/settings/profile", json={
        "name": "New Name", "currency": "INR", "alert_sensitivity": 5
    })
    assert response.status_code == 400


def test_update_profile_accepts_valid_payload(auth_client):
    response = auth_client.post("/api/v1/settings/profile", json={
        "name": "New Name", "currency": "INR", "alert_sensitivity": 0.5
    })
    assert response.status_code == 200


def test_delete_data_requires_confirmation_phrase(auth_client):
    response = auth_client.post("/api/v1/settings/delete-data", json={"confirm": "nope"})
    assert response.status_code == 400


def test_list_accounts_requires_login(client):
    response = client.get("/api/v1/settings/accounts")
    assert response.status_code == 302


def test_list_accounts_masks_account_number(auth_client, mock_db):
    mock_db.execute.return_value.fetchall.return_value = [
        ("acc-1", "CHECKING", "HDFC", "1234567890123456", "2025-01-01", 50000.0)
    ]
    response = auth_client.get("/api/v1/settings/accounts")
    assert response.status_code == 200
    account = response.get_json()["accounts"][0]
    assert account["account_number_masked"] == "••••3456"
    assert "1234567890123456" not in str(response.get_json())


# --- Goals ---

def test_list_goals_empty(auth_client, mock_db):
    mock_db.execute.return_value.fetchall.return_value = []
    response = auth_client.get("/api/v1/goals")
    assert response.status_code == 200
    assert response.get_json()["goals"] == []


def test_delete_goal_not_found(auth_client, mock_db):
    mock_db.execute.return_value.rowcount = 0
    response = auth_client.delete("/api/v1/goals/does-not-exist")
    assert response.status_code == 404


# --- Model info ---

def test_model_info_requires_login(client):
    response = client.get("/api/v1/models/info")
    assert response.status_code == 302


def test_model_info_reports_not_trained_when_missing(auth_client, monkeypatch):
    import webapp.routes.model_info as model_info_module

    def raise_not_found():
        raise FileNotFoundError()

    monkeypatch.setattr(model_info_module, "load_fraud_metadata", raise_not_found)
    monkeypatch.setattr(model_info_module, "load_cashflow_metadata", raise_not_found)

    response = auth_client.get("/api/v1/models/info")
    assert response.status_code == 200
    data = response.get_json()
    assert data["fraud_model"] is None
    assert data["cashflow_model"] is None


# --- Dashboard summary (heavier: mixes g.db.execute and pd.read_sql) ---

def test_dashboard_summary_empty_when_no_accounts(auth_client, mock_db):
    mock_db.execute.return_value.fetchall.return_value = []
    response = auth_client.get("/api/v1/dashboard/summary")
    assert response.status_code == 200
    data = response.get_json()
    assert data["current_balance"] == 0
    assert data["financial_health"]["score"] == 0


def test_dashboard_summary_with_data(auth_client, mock_db, monkeypatch):
    import pandas as pd
    import webapp.routes.dashboard as dashboard_module

    mock_db.execute.return_value.fetchall.side_effect = [
        [("acc-1",)],                        # user_account_ids
        [("FOOD", 3000.0)],                  # category_rows
        [(2026, 9, 3000.0, 10000.0)],        # trend_rows
        [("FOOD", 5000.0)],                  # budget_limits
    ]
    mock_db.execute.return_value.scalar.side_effect = [50000.0, 100.0]  # balance, avg_daily_spend
    mock_db.execute.return_value.fetchone.return_value = (10000.0, 3000.0)  # income, spending

    empty_df = pd.DataFrame(columns=['transaction_id', 'account_id', 'merchant_id', 'amount', 'date'])
    monkeypatch.setattr(dashboard_module.pd, "read_sql", lambda *a, **k: empty_df)

    response = auth_client.get("/api/v1/dashboard/summary")
    assert response.status_code == 200
    data = response.get_json()
    assert data["current_balance"] == 50000.0
    assert data["this_month"]["income"] == 10000.0
    assert data["spending_by_category"][0]["category_id"] == "FOOD"
    assert data["trend_6_months"][0]["month"] == "2026-09"
    assert 0 <= data["financial_health"]["score"] <= 100


# --- Fraud alerts (uses pd.read_sql + predict_fraud) ---

def test_fraud_alerts_empty_when_no_accounts(auth_client, mock_db):
    mock_db.execute.return_value.fetchall.return_value = []
    response = auth_client.get("/api/v1/fraud/alerts")
    assert response.status_code == 200
    assert response.get_json()["alerts"] == []


def test_fraud_alerts_rejects_out_of_range_threshold(auth_client):
    response = auth_client.get("/api/v1/fraud/alerts?threshold=5")
    assert response.status_code == 400


def test_fraud_alerts_returns_flagged_transactions(auth_client, mock_db, monkeypatch):
    import pandas as pd
    import webapp.routes.fraud as fraud_module

    mock_db.execute.return_value.fetchall.side_effect = [
        [("acc-1",)],  # user_account_ids
        [],            # fraud_feedback
    ]

    tx_df = pd.DataFrame({
        'transaction_id': ['tx-1'],
        'account_id': ['acc-1'],
        'merchant_id': ['UNKNOWN_SHOP'],
        'amount': [99999.0],
        'transaction_type': ['DEBIT'],
        'date': ['2026-09-01'],
        'transaction_ts': pd.to_datetime(['2026-09-01 03:00:00'])
    })
    profile_df = pd.DataFrame(columns=['amount', 'merchant_id', 'hour'])
    monkeypatch.setattr(fraud_module.pd, "read_sql", lambda query, con, params=None: (
        tx_df if 'fact_transactions f' in str(query) and 'JOIN dim_date' in str(query) else profile_df
    ))
    monkeypatch.setattr(fraud_module, "predict_fraud", lambda df: pd.DataFrame({
        'fraud_probability': [0.99], 'is_flagged': [True]
    }, index=df.index))

    response = auth_client.get("/api/v1/fraud/alerts?threshold=0.5")
    assert response.status_code == 200
    data = response.get_json()
    assert len(data["alerts"]) == 1
    assert data["alerts"][0]["merchant_id"] == "UNKNOWN_SHOP"
    assert "reasons" in data["alerts"][0]


def test_fraud_feedback_requires_valid_choice(auth_client):
    response = auth_client.post("/api/v1/fraud/feedback", json={"transaction_id": "tx-1", "feedback": "MAYBE"})
    assert response.status_code == 400


# --- Insights / subscriptions (pd.read_sql) ---

def test_subscriptions_empty_when_no_accounts(auth_client, mock_db):
    mock_db.execute.return_value.fetchall.return_value = []
    response = auth_client.get("/api/v1/insights/subscriptions")
    assert response.status_code == 200
    assert response.get_json()["subscriptions"] == []


def test_subscriptions_returns_detected_recurring_series(auth_client, mock_db, monkeypatch):
    import pandas as pd
    import webapp.routes.insights as insights_module

    base = pd.Timestamp('2026-01-01')
    rows = [{
        'transaction_id': f'tx-{i}', 'account_id': 'acc-1', 'merchant_id': 'NETFLIX',
        'amount': 500.0, 'date': base + pd.Timedelta(days=30 * i)
    } for i in range(6)]
    tx_df = pd.DataFrame(rows)

    monkeypatch.setattr(insights_module.pd, "read_sql", lambda *a, **k: tx_df)
    mock_db.execute.return_value.fetchall.side_effect = [
        [("acc-1",)],  # user_account_ids
        [],            # subscription_override
    ]

    response = auth_client.get("/api/v1/insights/subscriptions")
    assert response.status_code == 200
    data = response.get_json()
    assert len(data["subscriptions"]) == 1
    assert data["subscriptions"][0]["merchant_id"] == "NETFLIX"
    assert data["subscriptions"][0]["cadence"] == "monthly"
    assert data["total_annual_cost"] > 0


def test_subscriptions_override_success(auth_client, mock_db):
    mock_db.execute.return_value.fetchall.return_value = [("acc-1",)]
    response = auth_client.post("/api/v1/insights/subscriptions/override", json={
        "account_id": "acc-1", "merchant_id": "NETFLIX", "status": "NOT_SUBSCRIPTION"
    })
    assert response.status_code == 200
    assert response.get_json()["status"] == "NOT_SUBSCRIPTION"


def test_subscriptions_override_rejects_bad_status(auth_client):
    response = auth_client.post("/api/v1/insights/subscriptions/override", json={
        "account_id": "acc-1", "merchant_id": "NETFLIX", "status": "WHATEVER"
    })
    assert response.status_code == 400


# --- Cash flow forecast ---

def test_cashflow_forecast_empty_when_no_accounts(auth_client, mock_db):
    mock_db.execute.return_value.fetchall.return_value = []
    response = auth_client.get("/api/v1/cash-flow/forecast")
    assert response.status_code == 200
    assert response.get_json()["daily_forecasts"] == []


def test_cashflow_forecast_returns_model_not_found_gracefully(auth_client, mock_db, monkeypatch):
    import pandas as pd
    import webapp.routes.cashflow as cashflow_module

    mock_db.execute.return_value.fetchall.side_effect = [
        [("acc-1",)],  # user_account_ids
    ]
    monkeypatch.setattr(cashflow_module.pd, "read_sql", lambda *a, **k: pd.DataFrame({'date_id': [20260901], 'amount': [500.0]}))

    def raise_not_found(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(cashflow_module, "predict_cashflow", raise_not_found)

    response = auth_client.get("/api/v1/cash-flow/forecast")
    assert response.status_code == 200
    assert response.get_json()["daily_forecasts"] == []


# --- Auth: signup/login happy paths ---

def test_signup_creates_user_and_redirects_to_onboarding(client, mock_db):
    mock_db.execute.return_value.fetchone.return_value = None  # no existing user with that email
    response = client.post("/signup", data={
        "name": "New User", "email": "newuser@example.com",
        "password": "supersecret1", "confirm_password": "supersecret1", "submit": "Create account"
    })
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/onboarding")


def test_signup_rejects_duplicate_email(client, mock_db):
    mock_db.execute.return_value.fetchone.return_value = (1,)  # existing user found
    response = client.post("/signup", data={
        "name": "New User", "email": "existing@example.com",
        "password": "supersecret1", "confirm_password": "supersecret1", "submit": "Create account"
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"already exists" in response.data


def test_login_rejects_wrong_password(client, mock_db):
    from werkzeug.security import generate_password_hash
    mock_db.execute.return_value.fetchone.return_value = (
        FAKE_USER_ID, "Test User", "test@example.com", "INR", 0.8, generate_password_hash("correct-password"), None
    )
    response = client.post("/login", data={
        "email": "test@example.com", "password": "wrong-password", "submit": "Log in"
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Invalid email or password" in response.data


def test_login_succeeds_and_redirects_to_dashboard_when_onboarded(client, mock_db):
    from werkzeug.security import generate_password_hash
    from datetime import datetime, timezone
    mock_db.execute.return_value.fetchone.return_value = (
        FAKE_USER_ID, "Test User", "test@example.com", "INR", 0.8,
        generate_password_hash("correct-password"), datetime.now(timezone.utc)
    )
    response = client.post("/login", data={
        "email": "test@example.com", "password": "correct-password", "submit": "Log in"
    })
    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_model_info_reports_plain_language_summary(auth_client, monkeypatch):
    import webapp.routes.model_info as model_info_module

    monkeypatch.setattr(model_info_module, "load_fraud_metadata", lambda: {
        "training_date": "2026-01-01T00:00:00+00:00",
        "training_rows": 1000,
        "feature_columns": ["log_amount"],
        "contamination": 0.03,
        "metrics": {"precision": 0.75, "recall": 1.0, "confusion_matrix": {"tp": 10, "fp": 3, "fn": 0, "tn": 900}}
    })
    monkeypatch.setattr(model_info_module, "load_cashflow_metadata", lambda: {
        "training_date": "2026-01-01T00:00:00+00:00",
        "training_rows": 200,
        "selected_strategy": "model",
        "metrics": {"model_mae": 100.0, "baseline_last_month_mae": 150.0, "baseline_trailing_avg_mae": 160.0}
    })

    response = auth_client.get("/api/v1/models/info")
    assert response.status_code == 200
    data = response.get_json()
    assert "75" in data["fraud_model"]["plain_language"] or "7.5" in data["fraud_model"]["plain_language"]
    assert "beat both naive baselines" in data["cashflow_model"]["plain_language"]


# --- Health + API docs ---

def test_health_reports_db_and_model_status(client, mock_db):
    response = client.get("/health")
    assert response.status_code in (200, 503)
    data = response.get_json()
    assert "database" in data
    assert "models" in data
    assert set(data["models"].keys()) == {"fraud", "cashflow"}


def test_health_degraded_when_db_unavailable(client, mock_db):
    mock_db.execute.side_effect = Exception("connection refused")
    response = client.get("/health")
    assert response.status_code == 503
    assert response.get_json()["status"] == "down"


def test_openapi_spec_is_served_and_valid(client):
    response = client.get("/docs/openapi.json")
    assert response.status_code == 200
    spec = response.get_json()
    assert spec["openapi"].startswith("3.")
    assert "/budgets" in spec["paths"]


def test_swagger_ui_is_served(client):
    response = client.get("/api/docs", follow_redirects=True)
    assert response.status_code == 200
    assert b"swagger" in response.data.lower()


def test_update_profile_success_persists_and_returns_values(auth_client, mock_db):
    response = auth_client.post("/api/v1/settings/profile", json={
        "name": "  New Name  ", "currency": "USD", "alert_sensitivity": 0.5
    })
    assert response.status_code == 200
    assert response.get_json() == {"name": "New Name", "currency": "USD", "alert_sensitivity": 0.5}
    mock_db.commit.assert_called()


def test_update_profile_rejects_blank_name(auth_client):
    response = auth_client.post("/api/v1/settings/profile", json={"name": "   ", "currency": "INR", "alert_sensitivity": 0.5})
    assert response.status_code == 400


def test_update_profile_rejects_non_numeric_sensitivity(auth_client):
    response = auth_client.post("/api/v1/settings/profile", json={"name": "A", "currency": "INR", "alert_sensitivity": "high"})
    assert response.status_code == 400


def test_delete_data_with_confirmation_deletes_and_commits(auth_client, mock_db, monkeypatch):
    monkeypatch.setattr("webapp.routes.settings.user_account_ids", lambda uid: ["acc-1"])
    response = auth_client.post("/api/v1/settings/delete-data", json={"confirm": "DELETE"})
    assert response.status_code == 200
    statements = " ".join(str(call.args[0]) for call in mock_db.execute.call_args_list)
    assert "DELETE FROM fact_transactions" in statements
    assert "DELETE FROM dim_account" in statements
    assert "DELETE FROM dim_user" not in statements  # the login itself must survive
    mock_db.commit.assert_called()


def test_delete_data_without_accounts_skips_fact_deletes(auth_client, mock_db, monkeypatch):
    monkeypatch.setattr("webapp.routes.settings.user_account_ids", lambda uid: [])
    response = auth_client.post("/api/v1/settings/delete-data", json={"confirm": "DELETE"})
    assert response.status_code == 200
    statements = " ".join(str(call.args[0]) for call in mock_db.execute.call_args_list)
    assert "fact_transactions" not in statements


def test_reset_demo_reseeds_with_requested_persona(auth_client, monkeypatch):
    seen = {}
    monkeypatch.setattr("webapp.routes.settings.user_account_ids", lambda uid: [])
    monkeypatch.setattr("webapp.services.demo_seed.seed_demo_data_for_user", lambda uid, persona: seen.update(uid=uid, persona=persona))
    response = auth_client.post("/api/v1/settings/reset-demo", json={"persona": "freelancer"})
    assert response.status_code == 200
    assert seen["persona"] == "freelancer"


def test_reset_demo_with_an_invalid_persona_is_rejected_before_anything_is_deleted(auth_client, mock_db, monkeypatch):
    """Regression: the user's data used to be deleted first and the persona validated afterwards, so a bad
    request wiped everything and then failed."""
    called = []
    monkeypatch.setattr("webapp.routes.settings._delete_all_user_data", lambda uid: called.append(uid))
    response = auth_client.post("/api/v1/settings/reset-demo", json={"persona": "nope"})
    assert response.status_code == 400
    assert "persona must be one of" in response.get_json()["error"]["message"]
    assert called == []                       # nothing was deleted


def test_reset_demo_seed_failure_does_not_leak_internals_to_the_client(auth_client, monkeypatch):
    def boom(uid, persona):
        raise RuntimeError('relation "secret_table" violates constraint fk_internal')
    monkeypatch.setattr("webapp.routes.settings.user_account_ids", lambda uid: [])
    monkeypatch.setattr("webapp.services.demo_seed.seed_demo_data_for_user", boom)
    response = auth_client.post("/api/v1/settings/reset-demo", json={"persona": "student"})
    assert response.status_code == 500
    message = response.get_json()["error"]["message"]
    assert "secret_table" not in message and "constraint" not in message


def test_reset_demo_requires_login(client):
    assert client.post("/api/v1/settings/reset-demo", json={}).status_code == 302


# --- Upload preview / column mapping ---

def _csv_upload(csv_bytes, mapping=None, filename="statement.csv"):
    import json
    data = {'file': (io.BytesIO(csv_bytes), filename)}
    if mapping is not None:
        data['mapping'] = json.dumps(mapping)
    return data


def test_upload_preview_requires_login(client):
    response = client.post("/api/v1/transactions/upload/preview", data=_csv_upload(b"a,b\n1,2"), content_type='multipart/form-data')
    assert response.status_code == 302


def test_upload_preview_suggests_mapping_and_categorizes_without_saving(auth_client, mock_db, monkeypatch):
    import webapp.routes.upload as upload_module
    load = MagicMock()
    monkeypatch.setattr(upload_module, "load_data", load)
    mock_db.execute.return_value.fetchall.return_value = []  # no user category rules

    csv = b"Date,Description,Amount\n2026-09-01,ZOMATO ORDER,-500\n2026-09-02,SALARY CREDIT,50000\n"
    response = auth_client.post("/api/v1/transactions/upload/preview", data=_csv_upload(csv), content_type='multipart/form-data')

    body = response.get_json()
    assert response.status_code == 200 and body["ready"] is True
    assert body["suggested_mapping"]["description"] == "Description"
    assert body["summary"]["rows"] == 2 and body["summary"]["total_debit"] == 500.0 and body["summary"]["total_credit"] == 50000.0
    assert {p["category_id"] for p in body["preview"]} == {"FOOD", "INCOME"}
    assert body["category_counts"] == {"FOOD": 1, "INCOME": 1}
    load.assert_not_called()  # preview must never write


def test_upload_preview_reports_unmappable_columns_without_failing(auth_client):
    response = auth_client.post("/api/v1/transactions/upload/preview", data=_csv_upload(b"foo,bar\n1,2\n"), content_type='multipart/form-data')
    body = response.get_json()
    assert response.status_code == 200
    assert body["ready"] is False and "We need" in body["error"]
    assert body["columns"] == ["foo", "bar"]  # so the UI can offer them in dropdowns


def test_upload_preview_uses_user_supplied_mapping(auth_client, mock_db):
    mock_db.execute.return_value.fetchall.return_value = []
    csv = b"When,What,How much\n2026-09-01,UBER TRIP,120\n"
    mapping = {"date": "When", "description": "What", "amount": "How much"}
    body = auth_client.post("/api/v1/transactions/upload/preview", data=_csv_upload(csv, mapping), content_type='multipart/form-data').get_json()
    assert body["ready"] is True and body["preview"][0]["category_id"] == "TRANSPORT"


def test_upload_preview_rejects_malformed_mapping_field(auth_client):
    bad = {'file': (io.BytesIO(b"Date,Description,Amount\n2026-09-01,X,1\n"), 's.csv'), 'mapping': '{"bogus": "x"}'}
    assert auth_client.post("/api/v1/transactions/upload/preview", data=bad, content_type='multipart/form-data').status_code == 400
    bad['file'] = (io.BytesIO(b"Date,Description,Amount\n2026-09-01,X,1\n"), 's.csv')
    bad['mapping'] = 'not json'
    assert auth_client.post("/api/v1/transactions/upload/preview", data=bad, content_type='multipart/form-data').status_code == 400


def test_upload_preview_validates_file(auth_client):
    assert auth_client.post("/api/v1/transactions/upload/preview", data={}, content_type='multipart/form-data').status_code == 400
    not_csv = {'file': (io.BytesIO(b"x"), 'notes.txt')}
    assert auth_client.post("/api/v1/transactions/upload/preview", data=not_csv, content_type='multipart/form-data').status_code == 400


def test_upload_with_debit_credit_columns_and_user_mapping(auth_client, monkeypatch):
    import webapp.routes.upload as upload_module
    load = MagicMock()
    monkeypatch.setattr(upload_module, "load_data", load)

    csv = b"Txn Date,Narration,Withdrawal Amt.,Deposit Amt.\n01/09/2026,ZOMATO,250.00,\n02/09/2026,SALARY,,50000.00\n"
    response = auth_client.post("/api/v1/transactions/upload", data=_csv_upload(csv), content_type='multipart/form-data')
    assert response.status_code == 200 and response.get_json()["transactions_processed"] == 2

    facts = next(call.args[0] for call in load.call_args_list if call.args[1] == 'fact_transactions')
    assert sorted(facts["transaction_type"]) == ["CREDIT", "DEBIT"]


def test_upload_with_unusable_mapping_is_a_clean_400(auth_client):
    response = auth_client.post("/api/v1/transactions/upload", data=_csv_upload(b"foo,bar\n1,2\n"), content_type='multipart/form-data')
    assert response.status_code == 400 and "We need" in response.get_json()["error"]["message"]


def test_upload_long_descriptions_do_not_produce_duplicate_merchant_rows(auth_client, monkeypatch):
    import webapp.routes.upload as upload_module
    load = MagicMock()
    monkeypatch.setattr(upload_module, "load_data", load)
    # Identical for the first 50 characters (so the shortened merchant_id collides) but the
    # tails differ enough to categorize differently (FOOD vs FUEL).
    prefix = "UPI-SOME LONG PAYEE NAME-VPA123456789@OKHDFCBANK-UTIB0000-"
    assert len(prefix) > 50
    csv = f"Date,Description,Amount\n2026-09-01,{prefix}ZOMATO,-100\n2026-09-02,{prefix}INDIAN OIL,-200\n".encode()

    assert auth_client.post("/api/v1/transactions/upload", data=_csv_upload(csv), content_type='multipart/form-data').status_code == 200
    merchants = next(call.args[0] for call in load.call_args_list if call.args[1] == 'dim_merchant')
    assert merchants["merchant_id"].is_unique
    assert merchants["merchant_id"].str.len().max() <= 50


def test_upload_preview_response_is_strict_json_even_with_blank_cells(auth_client, mock_db):
    """Regression: blank cells (an empty Deposit column on a debit row) used to serialize as a bare
    NaN, which is not valid JSON and made the browser reject the whole preview."""
    import json
    mock_db.execute.return_value.fetchall.return_value = []
    csv = b"Date,Narration,Withdrawal Amt.,Deposit Amt.\n01/09/2026,ZOMATO,250.00,\n02/09/2026,SALARY,,50000.00\n"

    response = auth_client.post("/api/v1/transactions/upload/preview", data=_csv_upload(csv), content_type='multipart/form-data')

    def reject_nan(token):
        raise AssertionError(f"non-JSON constant in response: {token}")
    body = json.loads(response.data.decode(), parse_constant=reject_nan)
    assert body["ready"] is True
    assert body["sample_rows"][0]["Deposit Amt."] == ""


def test_model_info_describes_alerts_as_unusual_activity_not_proof_of_fraud(auth_client, monkeypatch):
    import webapp.routes.model_info as model_info_module
    monkeypatch.setattr(model_info_module, "load_fraud_metadata", lambda: {"metrics": {"precision": 0.6, "recall": 0.5}})
    monkeypatch.setattr(model_info_module, "load_cashflow_metadata", lambda: (_ for _ in ()).throw(FileNotFoundError()))

    text_out = auth_client.get("/api/v1/models/info").get_json()["fraud_model"]["plain_language"]

    assert "synthetic" in text_out
    assert "unusual for you" in text_out
    assert "6.0 of every 10" in text_out


def test_alerts_and_model_pages_use_unusual_activity_wording(auth_client):
    alerts_html = auth_client.get("/alerts").data.decode()
    models_html = auth_client.get("/about-models").data.decode()
    assert "not a confirmed fraud" in alerts_html
    assert "Unusual-Activity Detection" in models_html
    assert "cannot tell whether a transaction is actually fraud" in models_html


def test_every_api_route_is_documented_in_the_openapi_spec():
    """Keeps docs/openapi.json from drifting: any /api/v1 route + method missing from the spec fails here."""
    import json
    import re
    from pathlib import Path

    spec = json.loads((Path(__file__).resolve().parent.parent / "docs" / "openapi.json").read_text())
    documented = {(path, method.upper()) for path, ops in spec["paths"].items() for method in ops if method in ("get", "post", "patch", "put", "delete")}

    from webapp.app import app
    missing = []
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/api/v1/"):
            continue
        path = re.sub(r"<(?:[^:>]+:)?([^>]+)>", r"{\1}", rule.rule[len("/api/v1"):])
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            if (path, method) not in documented:
                missing.append(f"{method} {path}")
    assert not missing, "Routes missing from docs/openapi.json:\n" + "\n".join(sorted(missing))


def test_upload_stores_model_output_in_fraud_score_and_never_in_the_ground_truth_label(auth_client, monkeypatch):
    """Regression: an import used to write the model's flags into is_fraud -- the label the models are
    then evaluated against -- so the model ended up being graded on its own answers."""
    import pandas as pd
    import webapp.routes.upload as upload_module
    load = MagicMock()
    monkeypatch.setattr(upload_module, "load_data", load)
    monkeypatch.setattr(upload_module, "predict_fraud", lambda df, **kw: pd.DataFrame(
        {"fraud_score": -0.2, "fraud_probability": [0.97, 0.1], "is_flagged": [True, False]}, index=df.index))

    csv = b"Date,Description,Amount\n2026-09-01,ZOMATO,-500\n2026-09-02,UBER,-100\n"
    response = auth_client.post("/api/v1/transactions/upload", data={'file': (io.BytesIO(csv), 's.csv')}, content_type='multipart/form-data')

    assert response.get_json()["fraud_alerts_generated"] == 1
    facts = next(call.args[0] for call in load.call_args_list if call.args[1] == 'fact_transactions')
    assert not facts["is_fraud"].any()
    assert sorted(facts["fraud_score"]) == [0.1, 0.97]
    assert facts["scored_at"].notna().all()


def test_upload_without_a_trained_model_leaves_scores_empty(auth_client, monkeypatch):
    import webapp.routes.upload as upload_module
    load = MagicMock()
    monkeypatch.setattr(upload_module, "load_data", load)

    def no_model(df, **kw):
        raise FileNotFoundError()
    monkeypatch.setattr(upload_module, "predict_fraud", no_model)

    csv = b"Date,Description,Amount\n2026-09-01,ZOMATO,-500\n"
    response = auth_client.post("/api/v1/transactions/upload", data={'file': (io.BytesIO(csv), 's.csv')}, content_type='multipart/form-data')

    assert response.status_code == 200 and response.get_json()["fraud_alerts_generated"] == 0
    facts = next(call.args[0] for call in load.call_args_list if call.args[1] == 'fact_transactions')
    assert facts["fraud_score"].isna().all() and facts["scored_at"].isna().all()


# --- security hardening ---

def test_upload_database_errors_do_not_leak_internals(auth_client, monkeypatch):
    import webapp.routes.upload as upload_module

    def failing_load(*args, **kwargs):
        raise RuntimeError('duplicate key value violates unique constraint "dim_merchant_pkey" DETAIL: Key (merchant_id)=(X)')
    monkeypatch.setattr(upload_module, "load_data", failing_load)

    csv = b"Date,Description,Amount\n2026-09-01,ZOMATO,-500\n"
    response = auth_client.post("/api/v1/transactions/upload", data={'file': (io.BytesIO(csv), 's.csv')}, content_type='multipart/form-data')

    assert response.status_code == 500
    message = response.get_json()["error"]["message"]
    assert "dim_merchant_pkey" not in message and "constraint" not in message


def test_security_headers_are_set_on_every_response(client):
    response = client.get("/")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "same-origin"


@pytest.mark.parametrize("target", [
    "//evil.com", "//evil.com/path", "/\\evil.com", "/\\/evil.com", "https://evil.com", "http://evil.com/x",
    "evil.com", "javascript:alert(1)", "/ok\nX-Injected: 1", "", None, "/path\\with\\backslash",
])
def test_safe_next_url_rejects_anything_that_could_leave_the_site(target):
    from webapp.routes.auth import safe_next_url
    assert safe_next_url(target) is None


@pytest.mark.parametrize("target", ["/", "/transactions", "/transactions?page=2&type=DEBIT", "/goals#top"])
def test_safe_next_url_allows_same_site_paths(target):
    from webapp.routes.auth import safe_next_url
    assert safe_next_url(target) == target


def test_login_never_redirects_off_site_via_next(client, mock_db, monkeypatch):
    from werkzeug.security import generate_password_hash
    mock_db.execute.return_value.fetchone.return_value = (
        "22222222-2222-2222-2222-222222222222", "T", "t@example.com", "INR", 0.8, generate_password_hash("correct-horse"), "2026-01-01")
    for next_url in ("//evil.com", "/\\evil.com", "https://evil.com"):
        response = client.post(f"/login?next={next_url}", data={"email": "t@example.com", "password": "correct-horse"})
        assert response.status_code == 302
        location = response.headers["Location"]
        assert "evil.com" not in location, f"redirected to {location} for next={next_url!r}"


def test_login_still_honours_a_safe_next(client, mock_db):
    from werkzeug.security import generate_password_hash
    mock_db.execute.return_value.fetchone.return_value = (
        "22222222-2222-2222-2222-222222222222", "T", "t@example.com", "INR", 0.8, generate_password_hash("correct-horse"), "2026-01-01")
    response = client.post("/login?next=/transactions", data={"email": "t@example.com", "password": "correct-horse"})
    assert response.headers["Location"].endswith("/transactions")


# --- SECRET_KEY handling ---

def test_secret_key_placeholders_are_never_used_as_the_real_key(app_log):
    from webapp.app import load_secret_key
    placeholders = ["", "change-me-to-a-random-secret", "dev-only-insecure-key-change-me"]
    keys = {load_secret_key({"SECRET_KEY": p}) for p in placeholders}
    keys.add(load_secret_key({}))
    assert len(keys) == 4                                          # each call got its own random key
    assert not keys & set(placeholders)
    assert all(len(k) == 64 for k in keys)                         # token_hex(32)
    assert "SECRET_KEY is unset or a placeholder" in app_log.text


def test_a_real_secret_key_is_used_as_is():
    from webapp.app import load_secret_key
    assert load_secret_key({"SECRET_KEY": "a-real-private-key-value"}) == "a-real-private-key-value"
    assert load_secret_key({"SECRET_KEY": "short", "FLASK_ENV": "development"}) == "short"


@pytest.mark.parametrize("env", [
    {"FLASK_ENV": "production"},
    {"FLASK_ENV": "production", "SECRET_KEY": ""},
    {"FLASK_ENV": "production", "SECRET_KEY": "change-me-to-a-random-secret"},
    {"FLASK_ENV": "production", "SECRET_KEY": "dev-only-insecure-key-change-me"},
    {"FLASK_ENV": "production", "SECRET_KEY": "too-short"},
])
def test_production_refuses_to_start_without_a_strong_private_key(env):
    from webapp.app import load_secret_key
    with pytest.raises(RuntimeError, match="SECRET_KEY must be set"):
        load_secret_key(env)


def test_production_accepts_a_strong_key():
    from webapp.app import load_secret_key
    key = "0123456789abcdef0123456789abcdef"
    assert load_secret_key({"FLASK_ENV": "production", "SECRET_KEY": key}) == key


# --- SECRET_KEY x multiple gunicorn workers ---
# gunicorn forks each worker from a fresh import of the app, so a random per-process key means every
# worker signs sessions with a DIFFERENT key -- unlike the single-worker case, this isn't just an
# inconvenience (sessions resetting on restart), it's a live bug (logins randomly fail depending on
# which worker handles the next request), so it's enforced the same way production is.

def test_worker_count_defaults_to_one():
    from webapp.app import _worker_count
    assert _worker_count({}) == 1


@pytest.mark.parametrize("raw,expected", [("1", 1), ("4", 4), ("10", 10)])
def test_worker_count_reads_gunicorn_workers(raw, expected):
    from webapp.app import _worker_count
    assert _worker_count({"GUNICORN_WORKERS": raw}) == expected


@pytest.mark.parametrize("raw", ["", "not-a-number", "-1", "0"])
def test_worker_count_falls_back_to_one_for_garbage_or_non_positive_values(raw, app_log):
    from webapp.app import _worker_count
    assert _worker_count({"GUNICORN_WORKERS": raw}) == 1


def test_zero_workers_does_not_trigger_the_multi_worker_requirement():
    """GUNICORN_WORKERS=0 is nonsensical, but _worker_count's floor of 1 means it's treated as the
    harmless single-worker case (falls back to a random key), not silently interpreted as "many
    workers" (which would raise for a missing key)."""
    from webapp.app import load_secret_key
    key = load_secret_key({"GUNICORN_WORKERS": "0", "SECRET_KEY": ""})
    assert len(key) == 64  # a random token_hex(32), not a raised exception


@pytest.mark.parametrize("workers", ["2", "4", "10"])
def test_multiple_workers_refuse_to_start_without_a_strong_private_key(workers):
    from webapp.app import load_secret_key
    for bad_key in ("", "change-me-to-a-random-secret", "dev-only-insecure-key-change-me", "too-short"):
        with pytest.raises(RuntimeError, match="SECRET_KEY must be set"):
            load_secret_key({"GUNICORN_WORKERS": workers, "SECRET_KEY": bad_key})


def test_multiple_workers_error_message_names_the_actual_reason():
    """Distinguishes this from the production message, so someone hitting it in dev isn't confused
    about why a non-production run is refusing to start."""
    from webapp.app import load_secret_key
    with pytest.raises(RuntimeError, match="GUNICORN_WORKERS=4"):
        load_secret_key({"GUNICORN_WORKERS": "4", "SECRET_KEY": ""})


def test_multiple_workers_with_a_strong_key_starts_normally():
    from webapp.app import load_secret_key
    key = "0123456789abcdef0123456789abcdef"
    assert load_secret_key({"GUNICORN_WORKERS": "4", "SECRET_KEY": key}) == key


def test_single_worker_still_falls_back_to_a_random_key_outside_production():
    """The pre-existing, unchanged behavior for the common case: one worker, no key set."""
    from webapp.app import load_secret_key
    assert load_secret_key({"GUNICORN_WORKERS": "1"}) != ""
    assert len(load_secret_key({"GUNICORN_WORKERS": "1"})) == 64


def test_production_with_one_worker_still_requires_a_key():
    """Production's requirement doesn't depend on worker count -- it applied before GUNICORN_WORKERS
    existed and must keep applying with the (default) single worker too."""
    from webapp.app import load_secret_key
    with pytest.raises(RuntimeError, match="in production"):
        load_secret_key({"FLASK_ENV": "production", "GUNICORN_WORKERS": "1", "SECRET_KEY": ""})
