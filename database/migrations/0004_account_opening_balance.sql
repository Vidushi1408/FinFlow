-- Migration 0004: record each account's opening balance (the balance just
-- before its earliest transaction).
-- Uploaded bank statements used to be given an invented 50,000 starting
-- balance, so the balance shown was fiction. Storing the real (or, when
-- unknown, zero) opening balance lets the balance history be rebuilt
-- correctly whenever more transactions are imported into the same account.
-- Existing rows default to 0; generated/demo accounts keep the balance rows
-- they were seeded with and never rebuild from this column. Safe to re-run.

ALTER TABLE dim_account ADD COLUMN IF NOT EXISTS opening_balance DECIMAL(15, 2) NOT NULL DEFAULT 0;
