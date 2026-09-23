# Data policy: what's stored, for how long, and what deletion actually does

This is an honest description of FinFlow's current data handling, written for
whoever is running or reviewing a deployment of it — not a legal document,
and not a substitute for one if this is ever used with real users' real
financial data at any scale. Anything below marked **Not done here** is a
real gap, stated plainly rather than glossed over.

## What's stored

| Table | Contains | Sensitivity |
|---|---|---|
| `dim_user` | Name, email, password hash, currency, alert sensitivity | Identity |
| `dim_account` | Account type, institution, account number, opening balance | Financial |
| `fact_transactions` | Every transaction: merchant, amount, category, timestamp, fraud score | Financial |
| `fact_account_balance` | Daily opening/closing balance per account | Financial |
| `fact_investments` | SIP-style investment positions | Financial |
| `user_category_budget` | Per-category monthly spending limits the user set | Financial preference |
| `category_rule` | "Always categorize X as Y" rules the user created | Behavioral |
| `goal` | Savings goals (name, target amount, deadline) | Behavioral |
| `subscription_override` | "Cancel candidate" / "not a subscription" flags the user set | Behavioral |
| `fraud_feedback` | "This was me" / "Report as suspicious" responses to alerts | Behavioral |
| `dim_merchant`, `dim_category`, `dim_date` | Shared reference data (merchant names, category list, calendar) | Not personal — never scoped to a user, never deleted per-user |

Password hashes are salted (Werkzeug's `generate_password_hash`), never
plaintext. See the README's Security notes for the rest of the security
posture (CSRF, rate limiting, session cookies, etc.) — this document is about
data lifecycle, not request-level security.

## Retention while an account is active

Data is kept indefinitely for as long as the account exists — there's no
automatic expiry of old transactions, alerts, or goals. This matches how a
real personal-finance tool needs to work: last year's spending has to still
be there for a year-over-year comparison or a tax question.

## What "Delete all my data" actually removes

The `/api/v1/settings/delete-data` endpoint (`webapp/routes/settings.py:_delete_all_user_data`)
deletes, immediately and in full, every row in every table above that's
scoped to that user or their accounts: `fraud_feedback`, `subscription_override`,
`category_rule`, `user_category_budget`, `goal`, `fact_investments`,
`fact_account_balance`, `fact_transactions`, and `dim_account`. The `dim_user`
row itself is kept (only `onboarded_at` is cleared) so the login still works
and the person can start over — this is a deliberate choice, not an
oversight: a full account deletion (removing `dim_user` too, freeing the
email address for reuse) is not implemented separately.

**This is a hard delete, not a soft/tombstone delete** — there's no "recently
deleted" recovery window in the live database. The only place deleted data
can still exist afterward is in a backup taken before the deletion request
(see below), and only until that backup is rotated out.

The same deletion path runs (against that user only) before "Reset demo
data" reseeds a fresh demo persona.

## Backups

`scripts/backup_db.py` runs `pg_dump` in custom format (compressed,
supports selective/parallel restore) and deletes backups older than a
retention window (30 days by default). It's a script you run — there's no
automated scheduler wired up in `docker-compose.yml` (see **Not done here**).

```bash
# from the host, or via `docker compose run --rm webapp python scripts/backup_db.py`
python scripts/backup_db.py                       # backup to ./backups/, keep 30 days
python scripts/backup_db.py --dir /mnt/backups --keep-days 7
python scripts/backup_db.py --keep-days 0          # back up only, never auto-delete
```

**A user who deletes their data may still have it in a backup** taken before
the deletion request, until that backup ages out of the retention window.
`pg_dump` produces a whole-database snapshot; there is no supported way to
scrub one user's rows out of an already-written backup file. This is the
same tradeoff essentially every backup system with a retention window makes
(deleted data isn't recoverable from the live system, but persists in
backups until they rotate out) — stated here explicitly rather than left
implicit, since it's the one place "delete all my data" doesn't mean what it
sounds like it means.

### Restoring

Restoring is **deliberately not scripted** — a script that can restore over
a live database is also a script that can accidentally destroy one. Restore
by hand:

```bash
# Into a NEW database (recommended: verify before pointing anything at it) --
# never restore directly over a database still in use without a fresh backup first.
createdb -h <host> -U <user> finflow_restored
pg_restore --no-owner -h <host> -U <user> -d finflow_restored backups/finflow_<timestamp>.dump

# Inspect the archive's contents without touching any database:
pg_restore --list backups/finflow_<timestamp>.dump
```

### Encryption

`scripts/backup_db.py` does not encrypt the dump file it produces. A
`.dump` file contains the same data as the live database — names, emails,
password hashes, every transaction. If backups leave the machine that
produced them (copied to another host, uploaded to object storage), that
destination needs to already provide encryption at rest (e.g. an S3 bucket
with server-side encryption enabled, or a disk with FileVault/LUKS), or the
operator should encrypt the file themselves (`gpg -c` / `age`) before moving
it. This script doesn't manage encryption keys itself — a backup script that
also handles key material is a second, harder problem, and getting it wrong
(losing the key, or storing it next to the backup) is worse than not
attempting it.

## Not done here

Being direct about what a real production deployment would still need,
beyond what's in this repo:

- **No automated backup schedule.** `scripts/backup_db.py` is run manually or
  via a cron entry / systemd timer you set up yourself
  (`0 3 * * * cd /path/to/finflow && python scripts/backup_db.py`) — there's
  no scheduler service in `docker-compose.yml`.
- **No offsite/replicated backups.** Backups land on the same disk the
  database runs on by default (`--dir` can point elsewhere, including a
  mounted network volume, but nothing pushes them to object storage
  automatically). A disk failure destroys both the live data and every local
  backup at once unless `--dir` is pointed at genuinely separate storage.
- **No legal/compliance review.** This document describes what the code
  does, not a GDPR/CCPA/DPDP-compliant data processing policy. If this is
  ever used with real users at any real scale, get an actual legal review —
  particularly around the backup-retention-vs-deletion tradeoff above, data
  residency, and a documented data processing agreement if a third party
  ever hosts the database.
- **No separate audit log of deletions/exports.** The application logs
  ("delete all data" completing, etc.) go to the same structured log stream
  as everything else, not a dedicated, tamper-evident audit trail.
