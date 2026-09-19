from flask import Flask, render_template, g, jsonify, redirect, url_for, request, send_from_directory
from flask_login import current_user, login_required
from flask_wtf import CSRFProtect
from flask_swagger_ui import get_swaggerui_blueprint
from sqlalchemy import text
import sys
import os
import secrets

# Add parent directory to path so we can import etl, ml modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logging_config import logger, new_request_id, Timer
from webapp.database import SessionLocal
from webapp.auth import login_manager
from webapp.extensions import limiter

from webapp.routes.auth import auth_bp
from webapp.routes.spending import spending_bp
from webapp.routes.fraud import fraud_bp
from webapp.routes.cashflow import cashflow_bp
from webapp.routes.whatif import whatif_bp
from webapp.routes.upload import upload_bp
from webapp.routes.insights import insights_bp
from webapp.routes.dashboard import dashboard_bp
from webapp.routes.transactions import transactions_bp
from webapp.routes.budgets import budgets_bp
from webapp.routes.goals import goals_bp
from webapp.routes.settings import settings_bp
from webapp.routes.reports import reports_bp
from webapp.routes.model_info import model_info_bp

# Values that are public (committed in .env.example / docs / older compose files). A session cookie signed
# with one of these can be forged by anyone, so they are never used as the real key.
_PLACEHOLDER_SECRET_KEYS = {"", "change-me-to-a-random-secret", "dev-only-insecure-key-change-me"}


def load_secret_key(env=os.environ):
    """The session-signing key. In production a real, private key is mandatory. Elsewhere a missing or
    placeholder key becomes a random per-process one: sessions then reset on restart (and don't survive
    across multiple workers), which is inconvenient but safe -- unlike a publicly known key."""
    key = env.get("SECRET_KEY", "")
    if env.get("FLASK_ENV") == "production":
        if key in _PLACEHOLDER_SECRET_KEYS or len(key) < 16:
            raise RuntimeError("SECRET_KEY must be set to a private random value (16+ characters) in production. "
                               "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\"")
        return key
    if key in _PLACEHOLDER_SECRET_KEYS:
        logger.warning("SECRET_KEY is unset or a placeholder: using a random per-process key, so logins reset when the "
                       "server restarts. Set SECRET_KEY in .env to keep sessions (required with more than one worker).")
        return secrets.token_hex(32)
    return key


app = Flask(__name__)
app.secret_key = load_secret_key()
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10 MB, matches upload.py's own check
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get("FLASK_ENV") == "production"
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'
app.config['REMEMBER_COOKIE_SECURE'] = os.environ.get("FLASK_ENV") == "production"

csrf = CSRFProtect(app)
login_manager.init_app(app)
limiter.init_app(app)

# Register Blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(spending_bp)
app.register_blueprint(fraud_bp)
app.register_blueprint(cashflow_bp)
app.register_blueprint(whatif_bp)
app.register_blueprint(upload_bp)
app.register_blueprint(insights_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(transactions_bp)
app.register_blueprint(budgets_bp)
app.register_blueprint(goals_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(reports_bp)
app.register_blueprint(model_info_bp)

DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'docs')

@app.route('/docs/openapi.json')
def openapi_spec():
    return send_from_directory(DOCS_DIR, 'openapi.json')

app.register_blueprint(get_swaggerui_blueprint(
    '/api/docs', '/docs/openapi.json', config={'app_name': 'FinFlow API'}
))

# DB Session Management + request logging

@app.before_request
def before_request():
    g.db = SessionLocal()
    g.request_id = new_request_id()
    g.timer = Timer().__enter__()

@app.teardown_request
def teardown_request(exception):
    db = getattr(g, 'db', None)
    if db is not None:
        if exception is not None:
            db.rollback()
        db.close()

@app.after_request
def add_security_headers(response):
    # A Content-Security-Policy is deliberately not set: the pages use inline scripts and CDN assets.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.after_request
def log_request(response):
    timer = getattr(g, 'timer', None)
    duration_ms = None
    if timer is not None:
        timer.__exit__(None, None, None)
        duration_ms = timer.elapsed_ms
    logger.info(
        f"{request.method} {request.path} {response.status_code} {duration_ms}ms",
        extra={
            "request_id": getattr(g, 'request_id', None),
            "method": request.method,
            "path": request.path,
            "status": response.status_code,
            "duration_ms": duration_ms
        }
    )
    return response

@app.context_processor
def inject_alert_badge():
    if not current_user.is_authenticated:
        return {"unread_alert_count": 0}
    try:
        from webapp.db_utils import user_account_ids
        from ml.fraud_model import predict_fraud
        import pandas as pd
        from sqlalchemy import text

        account_ids = user_account_ids(current_user.id)
        if not account_ids:
            return {"unread_alert_count": 0}
        df = pd.read_sql(
            text("""
                SELECT transaction_id, merchant_id, amount, transaction_type, transaction_ts
                FROM fact_transactions
                WHERE account_id = ANY(:aids) AND transaction_type = 'DEBIT'
                ORDER BY transaction_ts DESC LIMIT 200
            """), g.db.connection(), params={"aids": account_ids}
        )
        if df.empty:
            return {"unread_alert_count": 0}
        scored = predict_fraud(df)
        return {"unread_alert_count": int(scored['is_flagged'].sum())}
    except Exception:
        return {"unread_alert_count": 0}

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": {"code": 404, "message": "Not found"}}), 404

@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": {"code": 500, "message": "Internal server error"}}), 500

@app.route("/health")
def health():
    try:
        g.db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    fraud_model_ok = os.path.exists(os.path.join(os.path.dirname(__file__), '..', 'models', 'fraud_model.pkl'))
    cashflow_model_ok = os.path.exists(os.path.join(os.path.dirname(__file__), '..', 'models', 'cashflow_model.pkl'))

    healthy = db_ok  # models being untrained yet is degraded, not down
    return jsonify({
        "status": "ok" if (db_ok and fraud_model_ok and cashflow_model_ok) else ("degraded" if db_ok else "down"),
        "database": db_ok,
        "models": {"fraud": fraud_model_ok, "cashflow": cashflow_model_ok}
    }), 200 if healthy else 503

# Routes for rendering HTML pages
# 'index' is the endpoint name used throughout templates for the "/" route (dashboard when
# logged in, landing page otherwise), kept from the pre-auth version of this app.
@app.route("/", endpoint="index")
def landing_or_dashboard():
    if not current_user.is_authenticated:
        return render_template("landing.html")
    return render_template("dashboard.html", active_page="dashboard")

@app.route("/transactions")
@login_required
def transactions_page():
    return render_template("transactions.html", active_page="transactions")

@app.route("/budgets")
@login_required
def budgets_page():
    return render_template("budgets.html", active_page="budgets")

@app.route("/subscriptions")
@login_required
def subscriptions_page():
    return render_template("subscriptions.html", active_page="subscriptions")

@app.route("/alerts")
@login_required
def alerts_page():
    return render_template("alerts.html", active_page="alerts")

@app.route("/fraud")
@login_required
def fraud_page():
    return redirect(url_for('alerts_page'))

@app.route("/whatif")
@login_required
def whatif_page():
    return render_template("whatif.html", active_page="forecast")

@app.route("/cashflow")
@login_required
def cashflow_page():
    return redirect(url_for('whatif_page'))

@app.route("/goals")
@login_required
def goals_page():
    return render_template("goals.html", active_page="goals")

@app.route("/reports")
@login_required
def reports_page():
    return render_template("reports.html", active_page="reports")

@app.route("/settings")
@login_required
def settings_page():
    return render_template("settings.html", active_page="settings")

@app.route("/about-models")
@login_required
def about_models_page():
    return render_template("about_models.html", active_page="about-models")

@app.route("/upload")
@login_required
def upload_page():
    return redirect(url_for('transactions_page'))

if __name__ == "__main__":
    # The Werkzeug debugger allows remote code execution, so it is only on when explicitly developing,
    # and the server listens on localhost unless HOST says otherwise.
    app.run(debug=os.environ.get("FLASK_ENV") == "development", host=os.environ.get("HOST", "127.0.0.1"), port=5000)
