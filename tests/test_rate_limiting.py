"""
Rate-limit storage selection, plus proof that Redis-backed storage actually
solves the problem it's meant to: with in-memory storage, each worker process
has its own counter (so N workers behind one login endpoint give N times the
intended limit); with Redis, the counter is shared.

The Redis-dependent tests use the `redis_url` fixture (tests/conftest.py) --
a real Redis connection, skipped if unreachable, exactly like `pg_db` for
Postgres. CI runs a real `redis` service so these always execute there; on a
machine without Redis running locally they skip cleanly rather than fail.

(An earlier version of this file used fakeredis's TcpFakeServer + the `lupa`
Lua-scripting package to avoid needing a real Redis at all. That added a
compiled C-extension as a test-only dependency and broke CI, for a benefit
this codebase doesn't need: every other piece of external infrastructure here
-- Postgres included -- is tested the same "real service, skip if absent" way.)
"""
import pytest

from webapp.extensions import load_limiter_storage_uri


# --- storage URI selection (no infra needed) ---

def test_defaults_to_in_memory_storage_when_redis_url_is_unset():
    assert load_limiter_storage_uri({}) == "memory://"


def test_uses_redis_url_when_set():
    assert load_limiter_storage_uri({"REDIS_URL": "redis://cache:6379/0"}) == "redis://cache:6379/0"


def test_blank_redis_url_falls_back_to_memory():
    assert load_limiter_storage_uri({"REDIS_URL": "   "}) == "memory://"


def test_warns_when_running_in_production_without_redis(app_log):
    assert load_limiter_storage_uri({"FLASK_ENV": "production"}) == "memory://"
    assert "REDIS_URL is not set" in app_log.text


def test_does_not_warn_in_development_without_redis(app_log):
    load_limiter_storage_uri({"FLASK_ENV": "development"})
    assert "REDIS_URL is not set" not in app_log.text


def test_production_with_redis_url_does_not_warn(app_log):
    load_limiter_storage_uri({"FLASK_ENV": "production", "REDIS_URL": "redis://cache:6379/0"})
    assert "REDIS_URL is not set" not in app_log.text


# --- real Redis-backed storage ---

pytestmark_redis = pytest.mark.integration  # applied per-test below; module docstring explains why these need `redis_url`


@pytest.mark.integration
def test_redis_url_is_a_genuinely_usable_redis_connection(redis_url):
    """Proves the `redis` package (an actual runtime dependency, added alongside this feature) is
    installed and works -- Flask-Limiter's RedisStorage imports it lazily, so a missing dependency
    would only surface the first time a real request hit a rate-limited route in production."""
    import redis as redis_client
    client = redis_client.from_url(redis_url)
    assert client.ping() is True


def _build_limited_app(storage_uri):
    """A minimal Flask app with one route limited to 3 requests/minute, storage pointed at `storage_uri`.
    Mirrors webapp/routes/auth.py's @limiter.limit('10 per minute') pattern at a smaller, faster-to-test limit."""
    from flask import Flask
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    app = Flask(__name__)
    limiter = Limiter(app=app, key_func=get_remote_address, storage_uri=storage_uri)

    @app.route("/protected")
    @limiter.limit("3 per minute")
    def protected():
        return "ok"

    return app


def test_in_memory_storage_limits_within_one_app_but_not_across_two():
    """Baseline: this is the exact problem Redis-backed storage fixes. Two separately-constructed
    apps (standing in for two worker processes) using in-memory storage do NOT share a counter.
    (No Redis needed for this one -- it's the in-memory case.)"""
    app_a = _build_limited_app("memory://")
    app_b = _build_limited_app("memory://")
    client_a, client_b = app_a.test_client(), app_b.test_client()

    for _ in range(3):
        assert client_a.get("/protected").status_code == 200
    assert client_a.get("/protected").status_code == 429   # worker A's own counter is exhausted

    # worker B has never seen a request, so its (separate, in-memory) counter is fresh
    assert client_b.get("/protected").status_code == 200


@pytest.mark.integration
def test_redis_storage_shares_the_counter_across_separate_app_instances(redis_url):
    """The actual fix: two separately-constructed apps (standing in for two worker processes) pointed
    at the SAME Redis instance share one counter for the same client, so the limit means what it says
    regardless of which worker handles which request."""
    app_a = _build_limited_app(redis_url)
    app_b = _build_limited_app(redis_url)
    client_a, client_b = app_a.test_client(), app_b.test_client()

    assert client_a.get("/protected").status_code == 200   # worker A: 1/3
    assert client_b.get("/protected").status_code == 200   # worker B: 2/3 (shared counter)
    assert client_a.get("/protected").status_code == 200   # worker A: 3/3

    assert client_b.get("/protected").status_code == 429   # worker B sees the limit worker A hit
    assert client_a.get("/protected").status_code == 429


@pytest.mark.integration
def test_redis_storage_still_separates_different_clients(redis_url):
    """Sharing the counter across workers must not mean sharing it across different clients."""
    app = _build_limited_app(redis_url)
    client = app.test_client()

    for _ in range(3):
        assert client.get("/protected", environ_overrides={"REMOTE_ADDR": "10.0.0.1"}).status_code == 200
    assert client.get("/protected", environ_overrides={"REMOTE_ADDR": "10.0.0.1"}).status_code == 429

    assert client.get("/protected", environ_overrides={"REMOTE_ADDR": "10.0.0.2"}).status_code == 200


@pytest.mark.integration
def test_real_login_route_is_rate_limited_end_to_end_via_redis(redis_url, monkeypatch):
    """The actual login route (not a stand-in), with its real @limiter.limit('10 per minute') decorator,
    backed by Redis storage: 11 requests from one client, the 11th is rejected."""
    # Import first, so Flask-Limiter's init_app() has already run (its storage backend is built
    # inside init_app, not lazily) -- only then does `.limiter` resolve to a real strategy object
    # whose `.storage` this test swaps out for the real Redis.
    import webapp.extensions as extensions_module
    from webapp.app import app
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    from limits.storage import storage_from_string
    monkeypatch.setattr(extensions_module.limiter.limiter, "storage", storage_from_string(redis_url))

    client = app.test_client()
    statuses = [client.get("/login").status_code for _ in range(11)]

    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429
