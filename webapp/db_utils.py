from flask import g
from sqlalchemy import text


def user_account_ids(user_id):
    """All account_ids owned by this user, for scoping every fact_transactions query."""
    rows = g.db.execute(
        text("SELECT account_id FROM dim_account WHERE user_id = :uid"),
        {"uid": user_id}
    ).fetchall()
    return [r[0] for r in rows]
