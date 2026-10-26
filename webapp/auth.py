from flask import g
from flask_login import LoginManager, UserMixin
from sqlalchemy import text

login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Please log in to continue.'
login_manager.login_message_category = 'error'


class User(UserMixin):
    """Thin wrapper around a dim_user row for Flask-Login."""

    def __init__(self, user_id, name, email, currency, alert_sensitivity=0.8, email_verified=True):
        self.id = str(user_id)
        self.name = name
        self.email = email
        self.currency = currency
        self.alert_sensitivity = float(alert_sensitivity)
        self.email_verified = bool(email_verified)


@login_manager.user_loader
def load_user(user_id):
    row = g.db.execute(
        text("SELECT user_id, name, email, currency, alert_sensitivity, email_verified_at FROM dim_user WHERE user_id = :uid"),
        {"uid": user_id}
    ).fetchone()
    if row is None:
        return None
    return User(row[0], row[1], row[2], row[3], row[4], email_verified=row[5] is not None)
