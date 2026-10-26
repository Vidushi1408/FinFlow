"""Clean up merchant names on bank statements imported BEFORE merchant ids became cleaned names.

Older imports stored the raw narration (cut to 50 characters) as merchant_id, reference number and all, so one
merchant appeared as many (every Swiggy order a different "merchant"), which weakens subscription detection and
unusual-merchant alerts for that data. New imports are already clean; this rewrites the old rows to match.

    python scripts/backfill_merchant_ids.py --dry-run     # show what would change, change nothing
    python scripts/backfill_merchant_ids.py               # back up the database, then apply
    python scripts/backfill_merchant_ids.py --no-backup   # skip the backup (not recommended)

What it touches: only transactions in imported-statement accounts (generated/demo data has catalogue merchants and
is left alone). transaction_ids do not change, so re-uploading an overlapping statement still recognises every row.
Subscription overrides follow their merchant to its new name. Everything runs in ONE transaction and is verified
(row count and total amount per account unchanged) before it commits; any mismatch rolls everything back. Safe to
re-run: rows that are already clean are skipped.
"""
import argparse
import os
import sys
from collections import defaultdict

from sqlalchemy import text

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from etl.extract.csv_mapping import MERCHANT_ID_MAX_LEN
from etl.load import db as db_module
from etl.transform.clean import merchant_key
from logging_config import logger
from webapp.services.statement_account import STATEMENT_INSTITUTION


def clean_id(merchant_id):
    return merchant_key(merchant_id)[:MERCHANT_ID_MAX_LEN]


def plan(session):
    """{old_merchant_id: new_merchant_id} for statement-account merchants whose cleaned name differs."""
    rows = session.execute(text("""
        SELECT DISTINCT f.merchant_id FROM fact_transactions f
        JOIN dim_account a ON a.account_id = f.account_id WHERE a.institution = :inst
    """), {"inst": STATEMENT_INSTITUTION}).fetchall()
    mapping = {r[0]: clean_id(r[0]) for r in rows}
    return {old: new for old, new in mapping.items() if old != new}


def _fingerprint(session):
    """(rows, total amount) per statement account, to prove nothing but merchant_id changed."""
    rows = session.execute(text("""
        SELECT f.account_id, COUNT(*), COALESCE(SUM(f.amount), 0) FROM fact_transactions f
        JOIN dim_account a ON a.account_id = f.account_id WHERE a.institution = :inst GROUP BY f.account_id
    """), {"inst": STATEMENT_INSTITUTION}).fetchall()
    return {r[0]: (int(r[1]), round(float(r[2]), 2)) for r in rows}


def run(session, dry_run=False):
    """Apply (or, with dry_run, only describe) the backfill using `session`. The caller commits/rolls back
    only via this function: it commits on success and rolls back on any verification failure."""
    mapping = plan(session)
    before = _fingerprint(session)
    distinct_before = session.execute(text("""
        SELECT COUNT(DISTINCT f.merchant_id) FROM fact_transactions f
        JOIN dim_account a ON a.account_id = f.account_id WHERE a.institution = :inst
    """), {"inst": STATEMENT_INSTITUTION}).scalar()
    summary = {
        "merchants_to_rewrite": len(mapping), "distinct_merchants_before": int(distinct_before or 0),
        "distinct_merchants_after": len({mapping.get(m, m) for m in _all_merchants(session)}),
        "rows_affected": 0, "dry_run": dry_run,
        "examples": list(mapping.items())[:5],
    }
    if not mapping:
        return summary
    if dry_run:
        summary["rows_affected"] = _count_rows(session, list(mapping))
        return summary

    by_new = defaultdict(list)
    for old, new in mapping.items():
        by_new[new].append(old)

    for new, olds in by_new.items():
        top_category = session.execute(text("""
            SELECT f.category_id FROM fact_transactions f JOIN dim_account a ON a.account_id = f.account_id
            WHERE a.institution = :inst AND f.merchant_id = ANY(:olds)
            GROUP BY f.category_id ORDER BY COUNT(*) DESC LIMIT 1
        """), {"inst": STATEMENT_INSTITUTION, "olds": olds}).scalar()
        session.execute(text("""
            INSERT INTO dim_merchant (merchant_id, merchant_name, category) VALUES (:id, :id, :cat)
            ON CONFLICT (merchant_id) DO NOTHING
        """), {"id": new, "cat": top_category or "OTHER"})
        result = session.execute(text("""
            UPDATE fact_transactions SET merchant_id = :new
            WHERE merchant_id = ANY(:olds) AND account_id IN (SELECT account_id FROM dim_account WHERE institution = :inst)
        """), {"new": new, "olds": olds, "inst": STATEMENT_INSTITUTION})
        summary["rows_affected"] += result.rowcount

        # Subscription overrides follow their merchant. Several old ids can collapse into one new id, so keep
        # the most recently changed status per (user, account) and drop the rest.
        overrides = session.execute(text("""
            SELECT user_id, account_id, merchant_id, status, updated_at FROM subscription_override
            WHERE merchant_id = ANY(:olds)
        """), {"olds": olds}).fetchall()
        latest = {}
        for user_id, account_id, merchant_id, status, updated_at in overrides:
            key = (user_id, account_id)
            if key not in latest or (updated_at and updated_at >= latest[key][1]):
                latest[key] = (status, updated_at)
        session.execute(text("DELETE FROM subscription_override WHERE merchant_id = ANY(:olds)"), {"olds": olds})
        for (user_id, account_id), (status, _) in latest.items():
            session.execute(text("""
                INSERT INTO subscription_override (user_id, account_id, merchant_id, status) VALUES (:u, :a, :m, :s)
                ON CONFLICT (user_id, account_id, merchant_id) DO UPDATE SET status = EXCLUDED.status
            """), {"u": user_id, "a": account_id, "m": new, "s": status})

    # Old catalogue rows nobody references any more.
    session.execute(text("""
        DELETE FROM dim_merchant m WHERE m.merchant_id = ANY(:olds)
        AND NOT EXISTS (SELECT 1 FROM fact_transactions f WHERE f.merchant_id = m.merchant_id)
    """), {"olds": list(mapping)})

    after = _fingerprint(session)
    if after != before:
        session.rollback()
        raise RuntimeError(f"Verification failed, nothing was changed. Before {before}, after {after}.")
    session.commit()
    return summary


def _all_merchants(session):
    return [r[0] for r in session.execute(text("""
        SELECT DISTINCT f.merchant_id FROM fact_transactions f JOIN dim_account a ON a.account_id = f.account_id
        WHERE a.institution = :inst
    """), {"inst": STATEMENT_INSTITUTION}).fetchall()]


def _count_rows(session, olds):
    return int(session.execute(text("""
        SELECT COUNT(*) FROM fact_transactions f JOIN dim_account a ON a.account_id = f.account_id
        WHERE a.institution = :inst AND f.merchant_id = ANY(:olds)
    """), {"inst": STATEMENT_INSTITUTION, "olds": olds}).scalar() or 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report what would change and change nothing")
    parser.add_argument("--no-backup", action="store_true", help="skip the pg_dump backup normally taken first")
    args = parser.parse_args()

    from sqlalchemy.orm import sessionmaker
    session = sessionmaker(bind=db_module.get_engine())()
    try:
        if not args.dry_run and not args.no_backup:
            from scripts.backup_db import BackupError, create_backup
            try:
                path = create_backup()
            except BackupError as e:
                print(f"Backup failed, so nothing was changed: {e}\nFix that, or pass --no-backup if you accept the risk.")
                return 2
            print(f"Backup written to {path}")
        summary = run(session, dry_run=args.dry_run)
    finally:
        session.close()

    verb = "Would rewrite" if args.dry_run else "Rewrote"
    print(f"{verb} {summary['merchants_to_rewrite']} merchant name(s) across {summary['rows_affected']} transaction(s); "
          f"distinct merchants {summary['distinct_merchants_before']} -> {summary['distinct_merchants_after']}.")
    for old, new in summary["examples"]:
        print(f"  {old!r} -> {new!r}")
    if not summary["merchants_to_rewrite"]:
        print("Nothing to do: all imported merchant names are already clean.")
    logger.info("backfill_merchant_ids finished: %s", {k: v for k, v in summary.items() if k != "examples"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
