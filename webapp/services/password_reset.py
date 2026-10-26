"""Signed, expiring, single-use password-reset links, and the (optional) SMTP sender that delivers them.

No token is stored. The token is a timed signature over the user id plus a fingerprint of their current
password hash, so it dies the moment the password changes (single use) or after RESET_TOKEN_MAX_AGE_S.
"""
import hashlib
import os
import smtplib
from email.message import EmailMessage

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from logging_config import logger

RESET_TOKEN_MAX_AGE_S = 3600
_SALT = "finflow-password-reset"


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=_SALT)


def _fingerprint(password_hash):
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def make_reset_token(user_id, password_hash):
    return _serializer().dumps({"uid": str(user_id), "fp": _fingerprint(password_hash)})


def read_reset_token(token, max_age=RESET_TOKEN_MAX_AGE_S):
    """(user_id, fingerprint) for a valid, unexpired token, else None."""
    try:
        data = _serializer().loads(token, max_age=max_age)
        return data["uid"], data["fp"]
    except (BadSignature, SignatureExpired, KeyError, TypeError):
        return None


def token_matches_password(fingerprint, password_hash):
    return fingerprint == _fingerprint(password_hash)


def send_email(to_email, subject, body, link=None):
    """Send via SMTP when SMTP_HOST is set; otherwise log the link (development: no mail server needed)."""
    host = os.environ.get("SMTP_HOST")
    if not host:
        logger.warning("SMTP_HOST not set; email '%s' for %s: %s", subject, to_email, link or body)
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_FROM", "no-reply@finflow.app")
    msg["To"] = to_email
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=10) as smtp:
            smtp.starttls()
            if os.environ.get("SMTP_USER"):
                smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            smtp.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException):
        logger.exception("Could not send email '%s'", subject)  # never shown to the requester
        return False


def send_reset_email(to_email, reset_url):
    return send_email(
        to_email, "Reset your FinFlow password",
        "Someone asked to reset the password for this FinFlow account.\n\n"
        f"Reset it here (the link works once and expires in {RESET_TOKEN_MAX_AGE_S // 60} minutes):\n{reset_url}\n\n"
        "If this wasn't you, ignore this email; your password hasn't changed.",
        link=reset_url,
    )


# --- email verification (same signed-token approach, different salt, longer life) ---
VERIFY_TOKEN_MAX_AGE_S = 7 * 24 * 3600


def make_verify_token(user_id, email):
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="finflow-email-verify").dumps(
        {"uid": str(user_id), "email": email})


def read_verify_token(token, max_age=VERIFY_TOKEN_MAX_AGE_S):
    """(user_id, email) for a valid, unexpired token, else None."""
    try:
        data = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="finflow-email-verify").loads(token, max_age=max_age)
        return data["uid"], data["email"]
    except (BadSignature, SignatureExpired, KeyError, TypeError):
        return None


def send_verification_email(to_email, verify_url):
    return send_email(
        to_email, "Confirm your FinFlow email",
        f"Welcome to FinFlow. Confirm this email address (the link works for 7 days):\n{verify_url}\n\n"
        "If you didn't create an account, ignore this email.",
        link=verify_url,
    )
