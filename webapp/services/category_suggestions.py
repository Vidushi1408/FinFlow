"""Suggest a category for a payee the keyword list couldn't place, from payees the user HAS already placed.

Two sources, both the user's own: their "always categorise X as Y" rules, and the categories already on
their imported transactions. A new payee whose name closely matches one of those (SWIGGY INSTAMART vs SWIGGY,
a re-spelled shop) inherits that category. Person-to-person UPI, cash and income categories are not
propagated by name similarity, since two different people can have similar names."""
import difflib

from sqlalchemy import text

from etl.transform.clean import merchant_key

UNRESOLVED = ("UPI", "OTHER")
NOT_PROPAGATED = frozenset({"UPI", "OTHER", "TRANSFER", "INCOME"})
SIMILARITY_CUTOFF = 0.86
MAX_KNOWN = 5000


def known_categories(db, user_id, account_ids):
    """{merchant_key: category} from this user's rules and already-categorised transactions."""
    known = {}
    if account_ids:
        rows = db.execute(text("""
            SELECT merchant_id, category_id FROM fact_transactions
            WHERE account_id = ANY(:aids) AND category_id <> ALL(:skip)
            GROUP BY merchant_id, category_id ORDER BY COUNT(*) DESC LIMIT :lim
        """), {"aids": account_ids, "skip": list(NOT_PROPAGATED), "lim": MAX_KNOWN}).fetchall()
        for row in rows:
            known.setdefault(merchant_key(row[0]), row[1])
    for row in db.execute(
        text("SELECT merchant_id, category_id FROM category_rule WHERE user_id = :uid"), {"uid": user_id}
    ).fetchall():
        if row[1] not in NOT_PROPAGATED:
            known[merchant_key(row[0])] = row[1]          # an explicit rule beats an inferred one
    return known


def closest_category(key, known):
    """(category, matched_key) for the most similar known payee, or None. Exact keys are not 'suggestions'."""
    if not key or key in known:
        return None
    match = difflib.get_close_matches(key, list(known), n=1, cutoff=SIMILARITY_CUTOFF)
    return (known[match[0]], match[0]) if match else None
