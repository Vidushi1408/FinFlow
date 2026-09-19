import uuid
from datetime import datetime

from urllib.parse import urlsplit

from flask import Blueprint, render_template, redirect, url_for, flash, g, request
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import text
from werkzeug.security import generate_password_hash, check_password_hash

from webapp.forms import SignupForm, LoginForm, DemoLoadForm
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

        login_user(User(user_id, form.name.data.strip(), form.email.data.lower().strip(), 'INR'))
        return redirect(url_for('auth.onboarding'))

    return render_template('auth/signup.html', form=form)


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit('10 per minute')
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    form = LoginForm()
    if form.validate_on_submit():
        row = g.db.execute(
            text("SELECT user_id, name, email, currency, alert_sensitivity, password_hash, onboarded_at FROM dim_user WHERE email = :email"),
            {"email": form.email.data.lower().strip()}
        ).fetchone()

        if row is None or not check_password_hash(row[5], form.password.data):
            flash('Invalid email or password.', 'error')
            return render_template('auth/login.html', form=form)

        login_user(User(row[0], row[1], row[2], row[3], row[4]), remember=form.remember.data)

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
