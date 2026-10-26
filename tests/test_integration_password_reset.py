"""Password reset and per-account login lockout against a real throwaway Postgres."""
import re
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from werkzeug.security import check_password_hash, generate_password_hash

import etl.load.db as db_module
import webapp.app as app_module
import webapp.routes.auth as auth_routes
from tests.conftest import query
from webapp.extensions import limiter

pytestmark = pytest.mark.integration

OLD_PASSWORD, NEW_PASSWORD = "old-password-1", "brand-new-pass-2"


@pytest.fixture
def user(pg_db):
    uid, email = str(uuid.uuid4()), f"reset-{uuid.uuid4().hex[:8]}@example.com"
    conn = db_module.get_connection()
    try:
        conn.cursor().execute(
            "INSERT INTO dim_user (user_id, name, email, password_hash, onboarded_at) VALUES (%s,'Reset Me',%s,%s, now())",
            (uid, email, generate_password_hash(OLD_PASSWORD)))
        conn.commit()
    finally:
        conn.close()
    return {"id": uid, "email": email}


@pytest.fixture
def client(pg_db, monkeypatch):
    url = f"postgresql://{db_module.DB_USER}:{db_module.DB_PASSWORD}@{db_module.DB_HOST}:{db_module.DB_PORT}/{pg_db}"
    engine = create_engine(url)
    monkeypatch.setattr(app_module, "SessionLocal", sessionmaker(bind=engine))
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret-key-for-tokens")
    limiter.enabled = False
    yield app_module.app.test_client()
    limiter.enabled = True
    engine.dispose()


@pytest.fixture
def sent(monkeypatch):
    """Capture reset links instead of mailing/logging them."""
    links = []
    monkeypatch.setattr(auth_routes, "send_reset_email", lambda to, url: links.append((to, url)))
    return links


def _token_path(link):
    return re.sub(r"^https?://[^/]+", "", link)


def test_reset_flow_changes_password_and_link_is_single_use(client, user, sent):
    assert client.post("/forgot-password", data={"email": user["email"]}).status_code == 302
    assert len(sent) == 1 and sent[0][0] == user["email"]
    path = _token_path(sent[0][1])

    assert client.get(path).status_code == 200
    done = client.post(path, data={"password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    assert done.status_code == 302 and done.headers["Location"].endswith("/login")

    stored = query("SELECT password_hash FROM dim_user WHERE user_id = %s", (user["id"],))[0][0]
    assert check_password_hash(stored, NEW_PASSWORD) and not check_password_hash(stored, OLD_PASSWORD)

    # Same link again: the password changed, so the token's fingerprint no longer matches.
    reuse = client.post(path, data={"password": "another-pass-3", "confirm_password": "another-pass-3"})
    assert reuse.status_code == 302 and "forgot-password" in reuse.headers["Location"]
    assert check_password_hash(query("SELECT password_hash FROM dim_user WHERE user_id = %s", (user["id"],))[0][0], NEW_PASSWORD)


def test_unknown_email_gets_the_same_response_and_sends_nothing(client, sent):
    response = client.post("/forgot-password", data={"email": "nobody-here@example.com"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")
    assert sent == []


def test_tampered_or_expired_tokens_are_rejected(client, user, sent, monkeypatch):
    client.post("/forgot-password", data={"email": user["email"]})
    path = _token_path(sent[0][1])

    assert "forgot-password" in client.get(path + "x").headers["Location"]

    import webapp.services.password_reset as pr
    real = pr.read_reset_token
    monkeypatch.setattr(auth_routes, "read_reset_token", lambda t: real(t, max_age=-1))
    assert "forgot-password" in client.get(path).headers["Location"]


def test_reset_rejects_mismatched_or_short_passwords(client, user, sent):
    client.post("/forgot-password", data={"email": user["email"]})
    path = _token_path(sent[0][1])
    assert client.post(path, data={"password": NEW_PASSWORD, "confirm_password": "different"}).status_code == 200
    assert client.post(path, data={"password": "short", "confirm_password": "short"}).status_code == 200
    assert check_password_hash(query("SELECT password_hash FROM dim_user WHERE user_id = %s", (user["id"],))[0][0], OLD_PASSWORD)


def test_failed_logins_lock_one_account_even_from_fresh_clients(client, user):
    limiter.enabled = True
    limiter.reset()
    try:
        statuses = []
        for _ in range(7):
            fresh = app_module.app.test_client()  # a different "attacker" session each attempt
            statuses.append(fresh.post("/login", data={"email": user["email"], "password": "wrong-password"}).status_code)
        assert statuses[:5] == [200] * 5 and 429 in statuses[5:]
    finally:
        limiter.reset()
        limiter.enabled = False


# --- email verification ---

def test_signup_starts_unverified_sends_a_link_and_the_link_verifies(client, monkeypatch):
    import webapp.routes.auth as routes
    sent_links = []
    monkeypatch.setattr(routes, "send_verification_email", lambda to, url: sent_links.append((to, url)))

    email = f"new-{uuid.uuid4().hex[:8]}@example.com"
    response = client.post("/signup", data={"name": "New Person", "email": email, "password": "long-enough-1", "confirm_password": "long-enough-1"})
    assert response.status_code == 302
    assert query("SELECT email_verified_at FROM dim_user WHERE email = %s", (email,))[0][0] is None
    assert "Please confirm your email address" in client.get("/onboarding").get_data(as_text=True)

    assert sent_links and sent_links[0][0] == email
    assert client.get(_token_path(sent_links[0][1])).status_code == 302
    assert query("SELECT email_verified_at FROM dim_user WHERE email = %s", (email,))[0][0] is not None
    assert "Please confirm your email address" not in client.get("/onboarding").get_data(as_text=True)


def test_bad_or_other_users_verification_tokens_do_nothing(client, user):
    from webapp.services.password_reset import make_verify_token
    assert "login" in client.get("/verify-email/garbage").headers["Location"]
    # a token for an address that is no longer the account's address is rejected
    with app_module.app.app_context():
        stale = make_verify_token(user["id"], "old-address@example.com")
    assert "login" in client.get(f"/verify-email/{stale}").headers["Location"]
    assert query("SELECT email_verified_at FROM dim_user WHERE user_id = %s", (user["id"],))[0][0] is None


def test_resend_requires_login(client):
    assert client.post("/resend-verification").status_code == 302
