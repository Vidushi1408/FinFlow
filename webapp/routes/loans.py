from flask import Blueprint, jsonify, request, g
from flask_login import login_required, current_user
from sqlalchemy import text

from webapp.errors import error_response

loans_bp = Blueprint('loans', __name__, url_prefix='/api/v1/loans')


@loans_bp.route('', methods=['GET'])
@login_required
def list_loans():
    rows = g.db.execute(
        text("""SELECT loan_id, name, outstanding_balance, interest_rate_pct, monthly_emi
                FROM loan WHERE user_id = :uid ORDER BY interest_rate_pct DESC"""),
        {"uid": current_user.id}
    ).fetchall()
    loans = [{
        "loan_id": str(r[0]), "name": r[1], "outstanding_balance": float(r[2]),
        "interest_rate_pct": float(r[3]), "monthly_emi": float(r[4]),
    } for r in rows]
    return jsonify({"loans": loans, "total_monthly_emi": round(sum(loan["monthly_emi"] for loan in loans), 2)})


@loans_bp.route('', methods=['POST'])
@login_required
def create_loan():
    req = request.get_json(silent=True) or {}
    name = (req.get('name') or '').strip()
    if not name or len(name) > 150:
        return error_response("name is required (150 characters max)", 400)
    try:
        balance = float(req.get('outstanding_balance'))
        rate = float(req.get('interest_rate_pct'))
        emi = float(req.get('monthly_emi'))
    except (TypeError, ValueError):
        return error_response("outstanding_balance, interest_rate_pct and monthly_emi must be numbers", 400)
    if balance <= 0 or emi <= 0:
        return error_response("outstanding_balance and monthly_emi must be > 0", 400)
    if not 0 <= rate <= 100:
        return error_response("interest_rate_pct must be between 0 and 100", 400)

    g.db.execute(
        text("""INSERT INTO loan (user_id, name, outstanding_balance, interest_rate_pct, monthly_emi)
                VALUES (:uid, :name, :balance, :rate, :emi)"""),
        {"uid": current_user.id, "name": name, "balance": balance, "rate": rate, "emi": emi}
    )
    g.db.commit()
    return jsonify({"message": "Loan added"}), 201


@loans_bp.route('/<loan_id>', methods=['DELETE'])
@login_required
def delete_loan(loan_id):
    try:
        result = g.db.execute(
            text("DELETE FROM loan WHERE loan_id = :lid AND user_id = :uid"),
            {"lid": loan_id, "uid": current_user.id}
        )
    except Exception:  # a malformed UUID is a 404, not a 500
        g.db.rollback()
        return error_response("Loan not found", 404)
    g.db.commit()
    if result.rowcount == 0:
        return error_response("Loan not found", 404)
    return jsonify({"message": "Loan deleted"})
