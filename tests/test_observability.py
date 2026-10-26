import sys
import types

from webapp.observability import _scrub, init_error_tracking


def _fake_sentry(monkeypatch):
    calls = {}
    sdk = types.ModuleType("sentry_sdk")
    sdk.init = lambda **kw: calls.update(kw)
    integrations = types.ModuleType("sentry_sdk.integrations")
    flask_mod = types.ModuleType("sentry_sdk.integrations.flask")
    flask_mod.FlaskIntegration = lambda: "flask-integration"
    monkeypatch.setitem(sys.modules, "sentry_sdk", sdk)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations", integrations)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.flask", flask_mod)
    return calls


def test_off_without_a_dsn(monkeypatch):
    calls = _fake_sentry(monkeypatch)
    assert init_error_tracking({}) is False and init_error_tracking({"SENTRY_DSN": "  "}) is False
    assert calls == {}


def test_on_with_a_dsn_and_never_sends_pii_or_bodies(monkeypatch):
    calls = _fake_sentry(monkeypatch)
    assert init_error_tracking({"SENTRY_DSN": "https://k@example.ingest.sentry.io/1", "FLASK_ENV": "production"}) is True
    assert calls["send_default_pii"] is False and calls["max_request_body_size"] == "never"
    assert calls["traces_sample_rate"] == 0.0 and calls["environment"] == "production"
    assert calls["before_send"] is _scrub


def test_scrub_strips_request_payloads_and_user():
    event = {"request": {"url": "/x", "data": {"amount": 5}, "cookies": {"s": "1"}, "headers": {"a": "b"}, "query_string": "q=1"},
             "user": {"email": "a@b.c"}, "message": "boom"}
    out = _scrub(event, None)
    assert out["request"] == {"url": "/x"} and "user" not in out and out["message"] == "boom"


def test_missing_sdk_degrades_to_off(monkeypatch):
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)   # makes `import sentry_sdk` raise ImportError
    assert init_error_tracking({"SENTRY_DSN": "https://k@example.ingest.sentry.io/1"}) is False
