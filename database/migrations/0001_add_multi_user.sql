-- Migration 0001: multi-user support.
-- Safe to re-run: every statement is idempotent (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).
-- This is the same change already folded into database/schema.sql for fresh installs;
-- run this file directly against a database that predates dim_user.

CREATE TABLE IF NOT EXISTS dim_user (
    user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(150) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    currency CHAR(3) NOT NULL DEFAULT 'INR',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE dim_account ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES dim_user(user_id);
CREATE INDEX IF NOT EXISTS idx_dim_account_user ON dim_account(user_id);

-- NOTE: user_id is intentionally left nullable here because an existing
-- dim_account may already have rows. Before relying on user-scoped queries in
-- production, backfill dim_user + dim_account.user_id for existing accounts,
-- then add `ALTER TABLE dim_account ALTER COLUMN user_id SET NOT NULL;`
-- separately once every row has an owner.
