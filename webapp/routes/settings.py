from flask import Blueprint, jsonify, request, g
from flask_login import login_required, current_user
from sqlalchemy import text
from webapp.db_utils import user_account_ids
from logging_config import logger
from webapp.errors import error_response

settings_bp = Blueprint('settings', __name__, url_prefix='/api/v1/settings')


@settings_bp.route('/accounts', methods=['GET'])
@login_required
def list_accounts():
    rows = g.db.execute(
        text("""
            SELECT a.account_id, a.account_type, a.institution, a.account_number, a.opened_date,
                   COALESCE(b.closing_balance, 0) as latest_balance
            FROM dim_account a
            LEFT JOIN LATERAL (
                SELECT closing_balance FROM fact_account_balance
                WHERE account_id = a.account_id ORDER BY date_id DESC LIMIT 1
            ) b ON true
            WHERE a.user_id = :uid
            ORDER BY a.opened_date
        """),
        {"uid": current_user.id}
    ).fetchall()

    accounts = [{
        "account_id": r[0],
        "account_type": r[1],
        "institution": r[2],
        "account_number_masked": f"••••{str(r[3])[-4:]}" if r[3] else None,
        "opened_date": str(r[4]),
        "latest_balance": float(r[5])
    } for r in rows]

    return jsonify({"accounts": accounts})


@settings_bp.route('/profile', methods=['POST'])
@login_required
def update_profile():
    req = request.get_json(silent=True) or {}
    name = (req.get('name') or '').strip()
    currency = req.get('currency', 'INR')
    alert_sensitivity = req.get('alert_sensitivity')

    if not name:
        return error_response("name is required", 400)
    if currency not in ('INR', 'USD', 'EUR'):
        return error_response("Unsupported currency", 400)
    try:
        alert_sensitivity = float(alert_sensitivity)
    except (TypeError, ValueError):
        return error_response("alert_sensitivity must be a number", 400)
    if not (0 <= alert_sensitivity <= 1):
        return error_response("alert_sensitivity must be between 0 and 1", 400)

    g.db.execute(
        text("""
            UPDATE dim_user SET name = :name, currency = :currency, alert_sensitivity = :sens
            WHERE user_id = :uid
        """),
        {"name": name, "currency": currency, "sens": alert_sensitivity, "uid": current_user.id}
    )
    g.db.commit()
    return jsonify({"name": name, "currency": currency, "alert_sensitivity": alert_sensitivity})


def _delete_all_user_data(user_id):
    """Delete every financial record for a user, in FK-safe order, but keep
    the dim_user row itself so the login still works afterward."""
    account_ids = user_account_ids(user_id)

    g.db.execute(text("DELETE FROM fraud_feedback WHERE user_id = :uid"), {"uid": user_id})
    g.db.execute(text("DELETE FROM subscription_override WHERE user_id = :uid"), {"uid": user_id})
    g.db.execute(text("DELETE FROM category_rule WHERE user_id = :uid"), {"uid": user_id})
    g.db.execute(text("DELETE FROM user_category_budget WHERE user_id = :uid"), {"uid": user_id})
    g.db.execute(text("DELETE FROM goal WHERE user_id = :uid"), {"uid": user_id})

    if account_ids:
        g.db.execute(text("DELETE FROM fact_investments WHERE account_id = ANY(:aids)"), {"aids": account_ids})
        g.db.execute(text("DELETE FROM fact_account_balance WHERE account_id = ANY(:aids)"), {"aids": account_ids})
        g.db.execute(text("DELETE FROM fact_transactions WHERE account_id = ANY(:aids)"), {"aids": account_ids})
        g.db.execute(text("DELETE FROM dim_account WHERE account_id = ANY(:aids)"), {"aids": account_ids})

    g.db.execute(text("UPDATE dim_user SET onboarded_at = NULL WHERE user_id = :uid"), {"uid": user_id})
    g.db.commit()


@settings_bp.route('/delete-data', methods=['POST'])
@login_required
def delete_all_data():
    req = request.get_json(silent=True) or {}
    if req.get('confirm') != 'DELETE':
        return error_response("Send {\"confirm\": \"DELETE\"} to confirm this irreversible action.", 400)

    _delete_all_user_data(current_user.id)
    return jsonify({"message": "All your data has been deleted."})


@settings_bp.route('/reset-demo', methods=['POST'])
@login_required
def reset_demo_data():
    from data_generator.generate_mock_data import PERSONAS
    from webapp.services.demo_seed import seed_demo_data_for_user

    persona = (request.get_json(silent=True) or {}).get('persona', 'salaried')
    # Validate BEFORE deleting anything: a bad request must not wipe the user's data and then fail.
    if persona not in PERSONAS:
        return error_response(f"persona must be one of: {', '.join(sorted(PERSONAS))}", 400)

    _delete_all_user_data(current_user.id)

    try:
        seed_demo_data_for_user(current_user.id, persona)
    except Exception:
        logger.exception("Could not reload demo data")
        return error_response("Could not reload demo data. Please try again.", 500)

    g.db.execute(text("UPDATE dim_user SET onboarded_at = CURRENT_TIMESTAMP WHERE user_id = :uid"), {"uid": current_user.id})
    g.db.commit()
    return jsonify({"message": "Demo data reset."})
