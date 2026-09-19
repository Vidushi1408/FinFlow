-- FinFlow Database Schema
-- Dimension Tables

CREATE TABLE IF NOT EXISTS dim_user (
    user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(150) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    currency CHAR(3) NOT NULL DEFAULT 'INR',
    alert_sensitivity NUMERIC(3,2) NOT NULL DEFAULT 0.80 CHECK (alert_sensitivity BETWEEN 0 AND 1),
    onboarded_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE dim_user ADD COLUMN IF NOT EXISTS alert_sensitivity NUMERIC(3,2) NOT NULL DEFAULT 0.80;
ALTER TABLE dim_user ADD COLUMN IF NOT EXISTS onboarded_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS dim_account (
    account_id VARCHAR(50) PRIMARY KEY,
    user_id UUID REFERENCES dim_user(user_id),
    account_type VARCHAR(50) NOT NULL,
    institution VARCHAR(100) NOT NULL,
    account_number VARCHAR(20) NOT NULL,
    credit_limit DECIMAL(15, 2),
    opened_date DATE NOT NULL
);

-- Idempotent upgrade path for a database created before multi-user support existed.
ALTER TABLE dim_account ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES dim_user(user_id);
CREATE INDEX IF NOT EXISTS idx_dim_account_user ON dim_account(user_id);
-- Balance just before the account's earliest transaction; see migrations/0004.
ALTER TABLE dim_account ADD COLUMN IF NOT EXISTS opening_balance DECIMAL(15, 2) NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS dim_merchant (
    merchant_id VARCHAR(50) PRIMARY KEY,
    merchant_name VARCHAR(150) NOT NULL,
    category VARCHAR(100) NOT NULL,
    mcc_code VARCHAR(4)
);

CREATE TABLE IF NOT EXISTS dim_category (
    category_id VARCHAR(50) PRIMARY KEY,
    category_name VARCHAR(100) NOT NULL UNIQUE,
    budget_amount DECIMAL(15, 2) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dim_date (
    date_id INT PRIMARY KEY, -- Format: YYYYMMDD
    date DATE NOT NULL UNIQUE,
    year INT NOT NULL,
    month INT NOT NULL,
    quarter INT NOT NULL,
    day_of_week INT NOT NULL,
    is_holiday BOOLEAN DEFAULT FALSE
);

-- Fact Tables

CREATE TABLE IF NOT EXISTS fact_transactions (
    -- 64 chars: transaction_id is a sha256 hex digest (etl.transform.clean.compute_idempotency_key)
    transaction_id VARCHAR(64) PRIMARY KEY,
    account_id VARCHAR(50) NOT NULL REFERENCES dim_account(account_id),
    merchant_id VARCHAR(50) NOT NULL REFERENCES dim_merchant(merchant_id),
    category_id VARCHAR(50) NOT NULL REFERENCES dim_category(category_id),
    date_id INT NOT NULL REFERENCES dim_date(date_id),
    transaction_ts TIMESTAMP NOT NULL,
    amount DECIMAL(15, 2) NOT NULL CHECK (amount > 0),
    currency CHAR(3) NOT NULL DEFAULT 'INR',
    transaction_type VARCHAR(20) NOT NULL CHECK (transaction_type IN ('DEBIT', 'CREDIT')),
    is_fraud BOOLEAN DEFAULT FALSE,
    is_recurring BOOLEAN DEFAULT FALSE,
    fraud_score NUMERIC(5,4),  -- calibrated 0-1 anomaly probability from ml.fraud_model.predict_fraud (NOT a label; is_fraud is the ground truth)
    scored_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for performance on fact tables
CREATE INDEX IF NOT EXISTS idx_fact_trans_date ON fact_transactions(date_id);
CREATE INDEX IF NOT EXISTS idx_fact_trans_account ON fact_transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_fact_trans_merchant ON fact_transactions(merchant_id);
CREATE INDEX IF NOT EXISTS idx_fact_trans_category ON fact_transactions(category_id);
CREATE INDEX IF NOT EXISTS idx_fact_trans_date_type ON fact_transactions(date_id, transaction_type);
CREATE INDEX IF NOT EXISTS idx_fact_trans_acc_date ON fact_transactions(account_id, date_id);

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_monthly_category_spend AS
SELECT 
    d.year,
    d.month,
    f.category_id,
    SUM(f.amount) as total_spent,
    COUNT(*) as transaction_count
FROM fact_transactions f
JOIN dim_date d ON f.date_id = d.date_id
WHERE f.transaction_type = 'DEBIT'
GROUP BY d.year, d.month, f.category_id;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_monthly_cat_spend ON mv_monthly_category_spend(year, month, category_id);

CREATE TABLE IF NOT EXISTS fact_account_balance (
    balance_id SERIAL PRIMARY KEY,
    account_id VARCHAR(50) REFERENCES dim_account(account_id),
    date_id INT REFERENCES dim_date(date_id),
    opening_balance DECIMAL(15, 2) NOT NULL,
    closing_balance DECIMAL(15, 2) NOT NULL,
    inflow DECIMAL(15, 2) DEFAULT 0,
    outflow DECIMAL(15, 2) DEFAULT 0,
    UNIQUE(account_id, date_id)
);

CREATE TABLE IF NOT EXISTS fact_investments (
    investment_id VARCHAR(50) PRIMARY KEY,
    account_id VARCHAR(50) REFERENCES dim_account(account_id),
    date_id INT REFERENCES dim_date(date_id),
    symbol VARCHAR(20) NOT NULL,
    quantity DECIMAL(15, 6) NOT NULL,
    cost_price DECIMAL(15, 2) NOT NULL,
    current_price DECIMAL(15, 2),
    gain_loss DECIMAL(15, 2)
);

CREATE INDEX IF NOT EXISTS idx_fact_inv_account ON fact_investments(account_id);
CREATE INDEX IF NOT EXISTS idx_fact_inv_date ON fact_investments(date_id);

-- Per-user monthly budget limits (replaces the old global dim_category.budget_amount
-- for anything user-facing; that column stays only as a generic seed default).
CREATE TABLE IF NOT EXISTS user_category_budget (
    user_id UUID NOT NULL REFERENCES dim_user(user_id),
    category_id VARCHAR(50) NOT NULL REFERENCES dim_category(category_id),
    monthly_limit DECIMAL(15, 2) NOT NULL CHECK (monthly_limit >= 0),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, category_id)
);

-- "Always categorize this merchant as X" rules created from inline edits.
CREATE TABLE IF NOT EXISTS category_rule (
    rule_id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES dim_user(user_id),
    merchant_id VARCHAR(50) NOT NULL,
    category_id VARCHAR(50) NOT NULL REFERENCES dim_category(category_id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, merchant_id)
);

-- User feedback on fraud alerts ("This was me" / "Report as suspicious"), for
-- future retraining and to stop re-alerting on confirmed-safe transactions.
CREATE TABLE IF NOT EXISTS fraud_feedback (
    feedback_id SERIAL PRIMARY KEY,
    transaction_id VARCHAR(64) NOT NULL REFERENCES fact_transactions(transaction_id),
    user_id UUID NOT NULL REFERENCES dim_user(user_id),
    feedback VARCHAR(20) NOT NULL CHECK (feedback IN ('CONFIRMED_ME', 'SUSPICIOUS')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (transaction_id, user_id)
);

-- User overrides on the auto-detected recurring series ("cancel candidate" / "not a subscription").
CREATE TABLE IF NOT EXISTS subscription_override (
    user_id UUID NOT NULL REFERENCES dim_user(user_id),
    account_id VARCHAR(50) NOT NULL REFERENCES dim_account(account_id),
    merchant_id VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL CHECK (status IN ('CANCEL_CANDIDATE', 'NOT_SUBSCRIPTION')),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, account_id, merchant_id)
);

-- Savings goals.
CREATE TABLE IF NOT EXISTS goal (
    goal_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES dim_user(user_id),
    name VARCHAR(150) NOT NULL,
    target_amount DECIMAL(15, 2) NOT NULL CHECK (target_amount > 0),
    deadline DATE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_goal_user ON goal(user_id);

-- Idempotent upgrade path for a database created before transaction_id became a
-- sha256 hex digest (64 chars) instead of a UUID (36 chars); widening a VARCHAR
-- is always safe and never truncates existing data.
ALTER TABLE fact_transactions ALTER COLUMN transaction_id TYPE VARCHAR(64);
ALTER TABLE fraud_feedback ALTER COLUMN transaction_id TYPE VARCHAR(64);
