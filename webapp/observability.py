"""Optional error tracking. Off unless SENTRY_DSN is set; when on, it is configured so that financial
data never leaves the machine: no request bodies, no cookies/headers PII, no performance tracing."""
import os

from logging_config import logger


def _scrub(event, hint):
    """Last line of defence: drop anything request-shaped that could carry transactions or credentials."""
    request = event.get("request")
    if isinstance(request, dict):
        for key in ("data", "cookies", "headers", "query_string"):
            request.pop(key, None)
    event.pop("user", None)
    return event


def init_error_tracking(env=os.environ):
    """Initialise Sentry if SENTRY_DSN is configured. Returns True when tracking is active."""
    dsn = (env.get("SENTRY_DSN") or "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; error tracking is off")
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=env.get("FLASK_ENV", "development"),
        integrations=[FlaskIntegration()],
        send_default_pii=False,
        max_request_body_size="never",
        traces_sample_rate=0.0,
        before_send=_scrub,
    )
    logger.info("Sentry error tracking enabled")
    return True
