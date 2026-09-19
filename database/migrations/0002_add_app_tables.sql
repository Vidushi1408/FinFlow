-- Migration 0002: tables needed by the Flask web app (budgets, category rules,
-- fraud feedback, subscription overrides, goals) plus dim_user settings columns.
-- Safe to re-run: every statement is idempotent.

ALTER TABLE dim_user ADD COLUMN IF NOT EXISTS alert_sensitivity NUMERIC(3,2) NOT NULL DEFAULT 0.80;
ALTER TABLE dim_user ADD COLUMN IF NOT EXISTS onboarded_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS user_category_budget (
    user_id UUID NOT NULL REFERENCES dim_user(user_id),
    category_id VARCHAR(50) NOT NULL REFERENCES dim_category(category_id),
    monthly_limit DECIMAL(15, 2) NOT NULL CHECK (monthly_limit >= 0),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, category_id)
);

CREATE TABLE IF NOT EXISTS category_rule (
    rule_id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES dim_user(user_id),
    merchant_id VARCHAR(50) NOT NULL,
    category_id VARCHAR(50) NOT NULL REFERENCES dim_category(category_id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, merchant_id)
);

CREATE TABLE IF NOT EXISTS fraud_feedback (
    feedback_id SERIAL PRIMARY KEY,
    transaction_id VARCHAR(50) NOT NULL REFERENCES fact_transactions(transaction_id),
    user_id UUID NOT NULL REFERENCES dim_user(user_id),
    feedback VARCHAR(20) NOT NULL CHECK (feedback IN ('CONFIRMED_ME', 'SUSPICIOUS')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (transaction_id, user_id)
);

CREATE TABLE IF NOT EXISTS subscription_override (
    user_id UUID NOT NULL REFERENCES dim_user(user_id),
    account_id VARCHAR(50) NOT NULL REFERENCES dim_account(account_id),
    merchant_id VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL CHECK (status IN ('CANCEL_CANDIDATE', 'NOT_SUBSCRIPTION')),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, account_id, merchant_id)
);

CREATE TABLE IF NOT EXISTS goal (
    goal_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES dim_user(user_id),
    name VARCHAR(150) NOT NULL,
    target_amount DECIMAL(15, 2) NOT NULL CHECK (target_amount > 0),
    deadline DATE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_goal_user ON goal(user_id);
