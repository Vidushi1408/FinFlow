-- Migration 0003: widen transaction_id from VARCHAR(50) to VARCHAR(64).
-- etl.transform.clean.compute_idempotency_key (Phase 1) replaced the random
-- UUID transaction_id with a sha256 hex digest (64 chars) so re-ingesting the
-- same source rows is idempotent -- but the original column was sized for a
-- UUID (36 chars) and silently truncated/rejected inserts. Widening a VARCHAR
-- is always safe and never truncates existing data. Safe to re-run.

ALTER TABLE fact_transactions ALTER COLUMN transaction_id TYPE VARCHAR(64);
ALTER TABLE fraud_feedback ALTER COLUMN transaction_id TYPE VARCHAR(64);
