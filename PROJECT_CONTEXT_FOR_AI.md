# FinFlow Project Context & Overview

## 1. Project Objective
FinFlow is a comprehensive personal finance data warehouse and intelligence platform. Its goal is to ingest bank transaction data, detect fraudulent activities, predict future cash flow, and enable interactive budget optimization through a normalized data warehouse and a RESTful API.

## 2. System Architecture & Tech Stack
The application is structured into several core layers:
- **Database:** PostgreSQL utilizing a normalized Star Schema.
- **Backend/API:** Python, Flask (with Jinja2 templates for the web app), pandas, psycopg2.
- **Machine Learning:** scikit-learn (IsolationForest for fraud, LinearRegression for cash flow forecasting).
- **Deployment:** Docker & Docker Compose.

**Data Flow:**
Data Generator (Mock CSVs) -> ETL Pipeline -> PostgreSQL Data Warehouse -> ML Models -> Flask Web App & API -> User Dashboard.

## 3. Core Components

### A. Data Warehouse (PostgreSQL)
A star schema designed to handle up to 5 years of history and 100K+ transactions.
- **Dimension Tables:** `dim_account`, `dim_merchant`, `dim_category`, `dim_date`.
- **Fact Tables:** `fact_transactions`, `fact_account_balance`, `fact_investments`.

### B. ETL Pipeline
- **Extract:** Ingests CSV transaction exports (and mock API data).
- **Transform:** Cleans data, categorizes transactions based on merchant keywords, and identifies recurring subscriptions.
- **Load:** Upserts data into the PostgreSQL warehouse, maintaining referential integrity.

### C. Machine Learning Models
- **Fraud Detection:** Uses `IsolationForest` to flag anomalous transactions based on features like transaction amount, time, and merchant frequency.
- **Cash Flow Prediction:** Uses `LinearRegression` with rolling average features to predict daily spending for the next 30 days.

### D. REST API & Web App
A Flask-based backend serving both HTML templates and JSON API endpoints.
Key endpoints include:
- `GET /api/spending/summary`: Category breakdown and total spent.
- `GET /api/fraud/alerts`: Flagged transactions based on fraud probability.
- `GET /api/insights/subscriptions`: Recurring merchants and optimization recommendations.
- `POST /api/what-if/budget`: Simulates budget adjustments to predict future balances.
- `GET /api/cash-flow/forecast`: 30-day spending forecast.

## 4. Current State & Known Issues for Improvement
An `IMPLEMENTATION_PLAN.md` exists which outlines several critical defects and areas for improvement that should be prioritized:

**Critical Defects:**
1. **Timestamp Loss:** The ETL pipeline drops the actual transaction time (`transaction_ts`) and only saves the date. This silently breaks the fraud detection model which relies on `transaction_hour` (currently always 0).
2. **SQL Injection Vulnerability:** The `/api/what-if/budget` endpoint interpolates user-supplied category keys directly into SQL strings.
3. **Flawed Recurring Detection:** The current logic flags any merchant with ≥3 transactions as a subscription, which leads to almost all transactions being flagged. It needs to check cadence/frequency and amount stability.
4. **Circular Fraud Alerts:** The API currently queries for rows where `is_fraud = true` (the ground truth label) and then scores them, rather than scoring unlabelled data.
5. **Dynamic Scoring Thresholds:** Fraud probability is min-max normalized per batch during inference, meaning the threshold changes meaning on every API call. It must be calibrated at training time.

**Data & Feature Gaps:**
- The system only generates DEBIT transactions, meaning there is no income data.
- The `fact_account_balance` and `fact_investments` tables are empty and not populated by the ETL, breaking endpoints like `/what-if/budget` (which uses a hardcoded fallback balance).
- Missing `.gitignore` which could lead to committing large CSVs and environment variables (DB passwords).

## 5. Next Steps for AI Agent
When reviewing this project, please focus on:
1. Fixing the critical architectural bugs in the ETL and ML pipelines (e.g., retaining timestamps, fixing inference normalization).
2. Patching security vulnerabilities (SQL injection).
3. Expanding the data generator to include CREDIT (income) transactions and populate balance/investment tables.
4. Improving the recurring transaction detection logic.
