-- Migration 0006: track whether a user has confirmed their email address.
-- Everyone who already exists is treated as verified (they predate the feature; locking or nagging them
-- would punish existing accounts, including the seeded demo users). Only new signups start unverified.
-- Safe to re-run: the backfill only touches rows where the column was just created (still NULL) AND
-- were created before this migration's first run, tracked via the column's existence check.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'dim_user' AND column_name = 'email_verified_at') THEN
        ALTER TABLE dim_user ADD COLUMN email_verified_at TIMESTAMP;
        UPDATE dim_user SET email_verified_at = CURRENT_TIMESTAMP;
    END IF;
END $$;
