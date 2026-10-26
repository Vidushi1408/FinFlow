import uuid
from datetime import datetime

from urllib.parse import urlsplit

from flask import Blueprint, render_template, redirect, url_for, flash, g, request
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import text
from werkzeug.security import generate_password_hash, check_password_hash

from webapp.forms import SignupForm, LoginForm, DemoLoadForm, ForgotPasswordForm, ResetPasswordForm
from webapp.services.password_reset import (
    make_reset_token, make_verify_token, read_reset_token, read_verify_token, send_reset_email,
    send_verification_email, token_matches_password,
)
from webapp.auth import User
from webapp.extensions import limiter
from webapp.services.demo_seed import seed_demo_data_for_user

auth_bp = Blueprint('auth', __name__)


def safe_next_url(target):
    """`target` if it is a same-site path we may redirect to after login, else None. Rejects absolute URLs,
    protocol-relative ones (//evil.com) and backslash tricks (/\\evil.com, which browsers read as //evil.com)."""
    if not target or not target.startswith('/') or target.startswith('//'):
        return None
    if '\\' in target or any(ord(ch) < 32 for ch in target):
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return None
    return target


@auth_bp.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    form = SignupForm()
    if form.validate_on_submit():
        existing = g.db.execute(
            text("SELECT 1 FROM dim_user WHERE email = :email"),
            {"email": form.email.data.lower().strip()}
        ).fetchone()
        if existing:
            flash('An account with that email already exists.', 'error')
            return render_template('auth/signup.html', form=form)

        user_id = str(uuid.uuid4())
        g.db.execute(
            text("""
                INSERT INTO dim_user (user_id, name, email, password_hash, currency)
                VALUES (:user_id, :name, :email, :password_hash, 'INR')
            """),
            {
                "user_id": user_id,
                "name": form.name.data.strip(),
                "email": form.email.data.lower().strip(),
                "password_hash": generate_password_hash(form.password.data)
            }
        )
        g.db.commit()

        email = form.email.data.lower().strip()
        send_verification_email(email, url_for('auth.verify_email', token=make_verify_token(user_id, email), _external=True))
        login_user(User(user_id, form.name.data.strip(), email, 'INR', email_verified=False))
        return redirect(url_for('auth.onboarding'))

    return render_template('auth/signup.html', form=form)


def _login_email_key():
    """Rate-limit failed logins per target account too, so a botnet rotating IPs can't grind one password."""
    return "login-email:" + (request.form.get('email') or '').lower().strip()


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit('10 per minute')
@limiter.limit('5 per 15 minutes', key_func=_login_email_key, methods=['POST'],
               deduct_when=lambda response: response.status_code != 302)
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    form = LoginForm()
    if form.validate_on_submit():
        row = g.db.execute(
            text("SELECT user_id, name, email, currency, alert_sensitivity, password_hash, onboarded_at, email_verified_at FROM dim_user WHERE email = :email"),
            {"email": form.email.data.lower().strip()}
        ).fetchone()

        if row is None or not check_password_hash(row[5], form.password.data):
            flash('Invalid email or password.', 'error')
            return render_template('auth/login.html', form=form)

        login_user(User(row[0], row[1], row[2], row[3], row[4], email_verified=row[7] is not None), remember=form.remember.data)

        next_page = safe_next_url(request.args.get('next'))
        if next_page:
            return redirect(next_page)
        return redirect(url_for('auth.onboarding') if row[6] is None else url_for('index'))

    return render_template('auth/login.html', form=form)


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'success')
    return redirect(url_for('index'))


@auth_bp.route('/onboarding', methods=['GET', 'POST'])
@login_required
def onboarding():
    demo_form = DemoLoadForm()
    if demo_form.validate_on_submit():
        try:
            seed_demo_data_for_user(current_user.id, demo_form.persona.data)
        except Exception as e:
            flash(f"Could not load demo data: {e}", 'error')
            return render_template('onboarding.html', demo_form=demo_form)

        g.db.execute(
            text("UPDATE dim_user SET onboarded_at = :now WHERE user_id = :uid"),
            {"now": datetime.utcnow(), "uid": current_user.id}
        )
        g.db.commit()
        flash('Demo data loaded.', 'success')
        return redirect(url_for('index'))

    return render_template('onboarding.html', demo_form=demo_form)


@auth_bp.route('/onboarding/complete', methods=['POST'])
@login_required
def onboarding_complete():
    """Called after a successful CSV upload during onboarding."""
    g.db.execute(
        text("UPDATE dim_user SET onboarded_at = :now WHERE user_id = :uid"),
        {"now": datetime.utcnow(), "uid": current_user.id}
    )
    g.db.commit()
    return redirect(url_for('index'))


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit('5 per 15 minutes', methods=['POST'])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    form = ForgotPasswordForm()
    if form.validate_on_submit():
        row = g.db.execute(
            text("SELECT user_id, password_hash FROM dim_user WHERE email = :email"),
            {"email": form.email.data.lower().strip()}
        ).fetchone()
        if row:
            token = make_reset_token(row[0], row[1])
            send_reset_email(form.email.data.lower().strip(), url_for('auth.reset_password', token=token, _external=True))
        # Same answer whether or not the address exists, so this can't be used to find who has an account.
        flash('If that email has a FinFlow account, a reset link is on its way. It expires in 60 minutes.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('auth/forgot_password.html', form=form)


@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
@limiter.limit('10 per 15 minutes', methods=['POST'])
def reset_password(token):
    parsed = read_reset_token(token)
    row = None
    if parsed:
        row = g.db.execute(
            text("SELECT user_id, password_hash FROM dim_user WHERE user_id = :uid"), {"uid": parsed[0]}
        ).fetchone()
    if not row or not token_matches_password(parsed[1], row[1]):
        flash('That reset link is invalid or has expired. Request a new one.', 'error')
        return redirect(url_for('auth.forgot_password'))

    form = ResetPasswordForm()
    if form.validate_on_submit():
        g.db.execute(
            text("UPDATE dim_user SET password_hash = :h WHERE user_id = :uid"),
            {"h": generate_password_hash(form.password.data), "uid": row[0]}
        )
        g.db.commit()
        flash('Password updated. Log in with your new password.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('auth/reset_password.html', form=form)


@auth_bp.route('/verify-email/<token>')
def verify_email(token):
    """Confirms the address in the link. Works whether or not the visitor is logged in, and only for the
    address the token was issued for (so changing the email later invalidates old links)."""
    parsed = read_verify_token(token)
    row = None
    if parsed:
        row = g.db.execute(text("SELECT email FROM dim_user WHERE user_id = :uid"), {"uid": parsed[0]}).fetchone()
    if not row or row[0] != parsed[1]:
        flash('That confirmation link is invalid or has expired. Log in and request a new one.', 'error')
        return redirect(url_for('auth.login'))
    g.db.execute(
        text("UPDATE dim_user SET email_verified_at = COALESCE(email_verified_at, :now) WHERE user_id = :uid"),
        {"now": datetime.utcnow(), "uid": parsed[0]}
    )
    g.db.commit()
    flash('Email confirmed. Thanks!', 'success')
    return redirect(url_for('index') if current_user.is_authenticated else url_for('auth.login'))


@auth_bp.route('/resend-verification', methods=['POST'])
@login_required
@limiter.limit('3 per hour')
def resend_verification():
    if current_user.email_verified:
        flash('Your email is already confirmed.', 'success')
    else:
        send_verification_email(
            current_user.email,
            url_for('auth.verify_email', token=make_verify_token(current_user.id, current_user.email), _external=True))
        flash('Confirmation email sent. Check your inbox.', 'success')
    return redirect(request.referrer if safe_next_url(urlsplit(request.referrer or '').path) else url_for('index'))
