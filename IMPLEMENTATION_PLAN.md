# FinFlow — Implementation Plan to Production Quality

Baseline: the skeleton is complete end-to-end (generator → ETL → Postgres star schema →
ML → FastAPI → Streamlit), but several pieces are structurally wrong rather than merely
unfinished, and 4 of the 10 spec deliverables are missing entirely. This plan fixes the
correctness bugs first, then closes deliverable gaps.

---

## Part 0 — Critical defects (fix before anything else)

### 0.1 Transaction time is destroyed by the ETL, silently breaking fraud detection
`fact_transactions` stores only `date_id` (a day). `etl/transform/clean.py:process_pipeline`
computes `date_id` and the timestamp is never persisted. `ml/fraud_model.extract_features`
then derives `transaction_hour` from `dim_date.date` (a DATE) — **it is always 0 for every
row**. The generator deliberately injects fraud at 01:00–04:00 and that entire signal is
discarded. One of four model features is a constant.

Fix: add `transaction_ts TIMESTAMP NOT NULL` to `fact_transactions`; carry it through
`process_pipeline`, `etl_pipeline.run_pipeline`'s column projection, and every query that
feeds the models.

### 0.2 SQL injection in `/api/what-if/budget`
`api/routers/whatif.py:24-28` interpolates user-supplied category keys straight into SQL via
f-string. Violates the spec's own security constraint. Replace with
`WHERE category_id = ANY(%s)` and pass a list parameter. Also delete the one-item/many-item
tuple branch — it exists only to work around the string formatting.

### 0.3 `.env` with the DB password is untracked but will be committed
There is no `.gitignore`. A first `git add .` commits `.env`, the 13 MB `transactions.csv`,
both `.pkl` models, every `__pycache__`, and the whole `venv/` (thousands of files).
Add `.gitignore` + `.env.example` before the first commit.

### 0.4 Recurring detection flags essentially everything
`detect_recurring_transactions` marks a transaction recurring if that (account, merchant)
pair appears ≥3 times ever. With 100k random transactions across 10 accounts × 14 merchants,
all 140 pairs qualify — so ~100% of rows are flagged, and `/api/insights/subscriptions`
reports the entire spend as subscriptions. Real detection needs **cadence + amount
stability**: group by (account, merchant), require ≥3 occurrences, a median inter-arrival
gap in a monthly/weekly band (e.g. 26–35 or 6–8 days) with low variance, and amount
coefficient of variation < ~0.15. Only rows in a qualifying series get flagged.

### 0.5 Fraud alerts are circular
`api/routers/fraud.py` queries `WHERE f.is_fraud = true` — the ground-truth label — then
scores those rows with the model. The endpoint can never surface an unlabelled fraud, which
is the entire point. Score recent transactions regardless of label; keep `is_fraud` strictly
as the evaluation label.

### 0.6 Fraud "probability" is batch-relative, so the threshold is meaningless
`predict_fraud` min-max normalises `decision_function` output **over the rows in the current
request**. The worst row in any batch always scores 1.0 and the best always 0.0, so
`threshold=0.8` means something different on every call. Calibrate once at training time:
persist the training score distribution (or fit a sigmoid / store percentiles) and map new
scores through that fixed transform.

Same class of bug in `extract_features`: `amount_deviation` and `merchant_freq` are computed
from the scoring batch, so a transaction's features change depending on what it is scored
alongside. Persist per-merchant mean/std/count at training time and join them at inference.

---

## Part 1 — Data model and data generation

1. **Schema additions** (`database/schema.sql`):
   - `fact_transactions.transaction_ts TIMESTAMP NOT NULL` (see 0.1)
   - `fact_transactions.currency CHAR(3) NOT NULL DEFAULT 'INR'` — the spec requires
     multi-currency support; there is currently no currency column anywhere.
   - `fact_transactions.fraud_score NUMERIC(5,4)` + `scored_at TIMESTAMP` so scores are
     persisted by a batch job instead of recomputed per request (needed for the <500 ms budget).
   - `dim_category.budget_amount` is created but never populated — seed real budgets, or the
     "budget vs actual" dashboard deliverable is impossible.
   - Composite index `(date_id, transaction_type)` and `(account_id, date_id)`; a
     `mv_monthly_category_spend` materialized view for the summary endpoint.
   - `CHECK (amount > 0)`, `NOT NULL` on the FK columns.
2. **Populate `fact_account_balance` and `fact_investments`.** Both tables exist and are
   never written to — so `/what-if/budget` falls back to a hardcoded `current_balance = 50000`
   (`whatif.py:47`) and `/what-if/investments` ignores the database entirely. Add a balance
   derivation step to the ETL (opening/closing/inflow/outflow per account-day) and generate
   a small holdings set.
3. **Generator realism** (`data_generator/generate_mock_data.py`):
   - Only `DEBIT` rows are ever produced — there is no income, so "cash flow" is really
     "spending". Add salary/refund CREDIT transactions.
   - Data is anchored to calendar 2023; relative to today every "last 90 days" view is empty.
     Anchor `END_DATE` to `today` and make history length a config knob (the spec wants up
     to 5 years).
   - `credit_limit: None` → empty CSV cell → NaN → inserted into a `DECIMAL` column as NaN
     rather than NULL. Coerce to `None` on load.
   - Fraud is 20k–100k against a 10–5000 baseline: trivially separable. Add subtler fraud
     (card-testing micro-charges, geographic/merchant-atypical) so precision/recall are
     informative.
   - Make it seedable (`--seed`) for reproducible tests.
4. **`is_holiday` is hardcoded `False`** in `etl/load/db.load_dates`. Use the `holidays`
   package for the relevant locale.

---

## Part 2 — ETL hardening

1. **Logging.** The spec requires logging throughout; every module uses bare `print()`.
   Add a `finflow/logging_config.py` with structured logging, log levels, and row counts
   per stage.
2. **Configuration.** File paths (`'accounts.csv'`), date ranges, batch sizes, and thresholds
   are hardcoded across the generator, ETL, and loader. Move to `config.yaml` + env overrides.
3. **Validation stage — missing entirely.** Spec section 3 asks for outlier flagging
   (>99th percentile), future-date checks, and currency consistency. `clean_transactions`
   silently *drops* bad rows with no record. Build `etl/validate/checks.py` that returns a
   report instead of discarding evidence.
4. **Data quality report (deliverable #10).** Emit JSON + Markdown: row counts per table,
   null rates, FK orphans, duplicate counts, outliers, rejected rows with reasons.
5. **`categorize_transactions` is a stub** — it only defaults to `'UNKNOWN'`. The spec wants
   keyword/MCC-based assignment from merchant descriptions. Implement a rule table
   (MCC → category, then merchant-name regex fallback), which also makes the unit test real.
6. **Referential integrity.** `etl_pipeline.py` loads facts without verifying that every
   `merchant_id`/`category_id`/`date_id` exists in its dimension; a miss aborts the whole
   batch with an FK error that's caught and printed as a one-liner. Pre-validate and
   quarantine orphans.
7. **Error handling.** `load_data` catches everything and prints — the pipeline reports
   success even when a stage failed. Raise, and let the orchestrator decide.
8. **Packaging.** There are **zero `__init__.py` files**; imports currently work only via
   implicit namespace packages and only when run from the repo root. Add `__init__.py`
   throughout plus a `pyproject.toml` so `pytest` and `uvicorn` work from anywhere.

---

## Part 3 — ML correctness and evaluation

1. Fix feature leakage and score calibration (0.6) — persist a feature-store artifact
   (per-merchant stats + score calibration) alongside the model, pickled together with a
   version string.
2. **Fraud evaluation is missing** (deliverable #3 explicitly asks for precision/recall).
   Ground truth exists in `is_fraud` and is never used for scoring the model. Add a
   train/holdout split and report precision, recall, F1, PR-AUC, and a confusion matrix at
   the operating threshold; write to `ml/reports/`.
3. **Cash flow model.**
   - No train/test split and no metrics. Add time-series split + MAE/RMSE/MAPE.
   - The recursive forecast feeds predictions back into the rolling features, so a
     LinearRegression on `[rolling_7d, rolling_14d, dow]` converges to a flat line within
     days. Add day-of-week one-hot, a trend term, and month-of-year; validate the 30-day
     shape against a seasonal-naive baseline.
   - **Confidence intervals are in the spec response but absent from the model and from
     `CashFlowForecastResponse`.** Derive them from residual quantiles and widen with horizon.
   - It sums all `amount` regardless of `transaction_type`; once CREDIT rows exist this
     silently mixes income into spending. Filter explicitly.
4. **Model loading.** `predict_fraud`/`predict_cashflow` unpickle from disk on *every* API
   call. Load once in a FastAPI lifespan handler and hold in app state.
5. Replace `pickle` with `joblib` and record a training manifest (rows, date range, params,
   metrics, git SHA).

---

## Part 4 — API

1. **Connection handling.** `api/dependencies.get_db` opens a brand-new psycopg2 connection
   per request. Use a pooled SQLAlchemy engine (`QueuePool`) — this is the single biggest
   lever on the <500 ms constraint.
2. `pd.read_sql(query, raw_psycopg2_conn)` in `fraud.py` and `cashflow.py` is unsupported by
   pandas and emits a warning; route through the SQLAlchemy engine.
3. **Input validation.** `spending/summary` does `month.split('-')` with no validation — any
   malformed value is a 500. Use a `Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")` and bound
   `days`, `threshold`, and the new `limit`/`offset` params.
4. **`/what-if/budget` math is wrong.** It uses `AVG(amount)` — the mean of a single
   transaction — as the monthly spend for a category (`whatif.py:33-37`). It must be
   `SUM(amount) / number_of_months` over the observed window, and the 6-month projection
   should start from a real balance from `fact_account_balance`, not `50000`.
5. `/what-if/investments` hardcodes 7%/3% returns and doesn't validate that the allocation
   sums to 1. Add a validator, make rates configurable, and return a range rather than a
   point estimate.
6. `/insights/subscriptions` multiplies the per-transaction average by 12 for annual cost
   regardless of actual cadence, and returns no `optimization recommendations` — which the
   spec lists as part of the response. Use the detected cadence from 0.4, and add
   recommendations (unused/duplicate/price-increased subscriptions).
7. Add: `/health` (DB + model readiness), CORS, a request-timing middleware, a global
   exception handler returning RFC-7807-style errors, and pagination on list endpoints
   (`/fraud/alerts` has a hardcoded `LIMIT 100`).
8. Export `openapi.json` to `docs/` as the Swagger deliverable.

---

## Part 5 — Testing (deliverable #9)

Current state: two tests, both on `clean_transactions`/`categorize_transactions`, and
`categorize_transactions` is a stub so its test asserts the stub. No ML tests, no load tests,
no API tests. Build out:

- `tests/test_transform.py` — dedup, missing values, future dates, outliers, categorisation
  rules, and **recurring detection** (the highest-risk logic).
- `tests/test_ml.py` — feature determinism (same row scores the same regardless of batch —
  this is the regression test for 0.6), calibration bounds in [0,1], metric thresholds on a
  fixed seeded dataset, and a <5-minute training-time assertion.
- `tests/test_api.py` — `TestClient` against a seeded test database, covering every endpoint,
  the validation errors, and the SQL-injection payload from 0.2 as a regression test.
- `tests/conftest.py` with a seeded-dataframe fixture and a transactional DB fixture.
- Target ~80% coverage on `etl/` and `ml/`.

---

## Part 6 — Infrastructure (deliverables #7, #8)

Nothing here exists yet.

1. `Dockerfile` (multi-stage, non-root) + `docker-compose.yml` (Postgres 16 with a healthcheck,
   API, dashboard, and a one-shot ETL job depending on DB health).
2. `.github/workflows/ci.yml` — lint (ruff), format check (black), type check (mypy), pytest
   with a Postgres service container, and a smoke run of the full pipeline on a small dataset.
3. Pin `requirements.txt` exactly (it is all `>=` today) and split `requirements-dev.txt`.
   Note: the venv is Python **3.14**, which is ahead of some scientific wheels — pin to 3.12
   in Docker/CI for reproducibility.
4. `Makefile`: `make setup | data | etl | train | api | dashboard | test | docker-up`.

---

## Part 7 — Dashboard and documentation

1. Missing visuals from deliverable #6: **budget vs actual waterfall** (needs
   `dim_category.budget_amount` from Part 1) and a proper **subscription audit table** with
   recommendations.
2. Dashboard formats INR merchant amounts with `$`; fix currency formatting and drive it from
   the new `currency` column.
3. Replace the manual "Month (YYYY-MM)" text input with a date picker bounded by the actual
   data range, and add caching (`st.cache_data`) so every rerender doesn't re-hit the API.
4. Docs: architecture diagram, ERD, setup guide (already good in README), and the
   **example queries** file the spec asks for. Update the README with the Docker path and a
   note that Streamlit substitutes for Power BI.

---

## Suggested order

| Phase | Content | Why first |
|---|---|---|
| 1 | Part 0 entirely (0.1–0.6) | Security + the bugs that make the ML meaningless |
| 2 | Part 1 schema/generator, Part 2 packaging + logging | Everything downstream depends on the schema and on imports working |
| 3 | Part 3 ML + metrics | Now the features are real |
| 4 | Part 4 API | Now the data and models are trustworthy |
| 5 | Part 5 tests | Lock in phases 1–4 |
| 6 | Parts 6–7 infra, dashboard, docs | Presentation layer |

Phases 1–2 require re-running the generator and a full ETL reload (the schema changes are
not backward compatible with the existing 100k rows).
