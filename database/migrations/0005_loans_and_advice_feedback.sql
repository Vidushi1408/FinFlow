-- Migration 0005: loans (so the money coach can reason about EMI burden and prepayment order) and
-- per-user feedback on its suggestions (dismissed ones stay hidden for a while; useful ones rank up).
-- Same change as in database/schema.sql for fresh installs. Safe to re-run.

CREATE TABLE IF NOT EXISTS loan (
    loan_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES dim_user(user_id),
    name VARCHAR(150) NOT NULL,
    outstanding_balance DECIMAL(15, 2) NOT NULL CHECK (outstanding_balance > 0),
    interest_rate_pct DECIMAL(5, 2) NOT NULL CHECK (interest_rate_pct >= 0 AND interest_rate_pct <= 100),
    monthly_emi DECIMAL(15, 2) NOT NULL CHECK (monthly_emi > 0),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_loan_user ON loan(user_id);

CREATE TABLE IF NOT EXISTS advice_feedback (
    user_id UUID NOT NULL REFERENCES dim_user(user_id),
    advice_id VARCHAR(200) NOT NULL,
    feedback VARCHAR(20) NOT NULL CHECK (feedback IN ('USEFUL', 'DISMISSED')),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, advice_id)
);
