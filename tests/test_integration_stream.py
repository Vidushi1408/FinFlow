import os
import uuid
from datetime import datetime

import pandas as pd
import pytest

import etl.load.db as db_module
import scripts.stream_data as stream
from ml import fraud_model
from tests.conftest import query

pytestmark = pytest.mark.integration


def _account_totals(account_id):
    credits, debits = query(
        "SELECT COALESCE(SUM(amount) FILTER (WHERE transaction_type='CREDIT'),0), "
        "COALESCE(SUM(amount) FILTER (WHERE transaction_type='DEBIT'),0) "
        "FROM fact_transactions WHERE account_id = %s", (account_id,)
    )[0]
    return float(credits), float(debits)


def test_run_cycle_with_no_accounts_is_a_noop(pg_db, monkeypatch):
    monkeypatch.setattr(stream, "_fetch_account_ids", lambda: [])
    stream.run_cycle()  # must not raise


def test_run_cycle_inserts_categorized_transactions_and_balances(pg_db, make_user, monkeypatch):
    _, account_id = make_user()
    monkeypatch.setattr(stream, "_fetch_account_ids", lambda: [account_id])

    stream.run_cycle()

    rows = query("SELECT category_id, is_recurring, date_id FROM fact_transactions WHERE account_id = %s", (account_id,))
    assert rows, "expected at least one streamed transaction"
    assert all(cat not in ("UNKNOWN", None) for cat, _, _ in rows)
    assert all(not rec for _, rec, _ in rows)
    today_id = int(datetime.now().strftime("%Y%m%d"))
    assert all(d == today_id for _, _, d in rows)
    assert len(query("SELECT 1 FROM fact_account_balance WHERE account_id = %s AND date_id = %s", (account_id, today_id))) == 1


def test_multiple_cycles_same_day_keep_todays_balance_row_consistent(pg_db, make_user, monkeypatch):
    _, account_id = make_user()
    monkeypatch.setattr(stream, "_fetch_account_ids", lambda: [account_id])
    today_id = int(datetime.now().strftime("%Y%m%d"))

    for _ in range(3):
        stream.run_cycle()

    credits, debits = _account_totals(account_id)
    opening, closing, inflow, outflow = [float(x) for x in query(
        "SELECT opening_balance, closing_balance, inflow, outflow FROM fact_account_balance "
        "WHERE account_id = %s AND date_id = %s", (account_id, today_id))[0]]

    # The day's row must describe the WHOLE day, not just the last batch.
    assert inflow == pytest.approx(credits)
    assert outflow == pytest.approx(debits)
    assert closing == pytest.approx(opening + credits - debits)


def test_stream_never_writes_into_a_users_imported_statement_account(pg_db, make_user):
    """Regression: the stream picked random accounts from ALL accounts, and put synthetic transactions
    into real users' imported bank statements."""
    from tests.conftest import query as q
    import etl.load.db as db_module
    _, real_account = make_user()
    conn = db_module.get_connection()
    conn.cursor().execute("UPDATE dim_account SET institution = 'UPLOADED_STATEMENT' WHERE account_id = %s", (real_account,))
    conn.commit()
    conn.close()

    assert real_account not in stream._fetch_account_ids()

    for _ in range(5):
        stream.run_cycle()
    assert q("SELECT count(*) FROM fact_transactions WHERE account_id = %s", (real_account,)) == [(0,)]


# --- fraud scoring + recurring flags for streamed rows ---

def _streamed_rows(account_id):
    return query(
        "SELECT transaction_id, fraud_score, scored_at, is_fraud, is_recurring FROM fact_transactions WHERE account_id = %s",
        (account_id,))


def _fake_predict(probability):
    def predict(df, **kwargs):
        return pd.DataFrame({"fraud_score": -0.1, "fraud_probability": probability, "is_flagged": probability > 0.5}, index=df.index)
    return predict


def test_streamed_rows_get_a_fraud_score_and_scored_at_but_is_fraud_is_left_alone(pg_db, make_user, monkeypatch):
    _, account_id = make_user()
    monkeypatch.setattr(stream, "_fetch_account_ids", lambda: [account_id])
    monkeypatch.setattr(stream, "predict_fraud", _fake_predict(0.9123))

    stream.run_cycle()

    rows = _streamed_rows(account_id)
    assert rows
    for _, score, scored_at, is_fraud, _ in rows:
        assert float(score) == pytest.approx(0.9123)
        assert scored_at is not None
        assert is_fraud is False  # ground-truth label must not be overwritten by model output


def test_missing_model_leaves_rows_unscored_but_the_cycle_still_succeeds(pg_db, make_user, monkeypatch):
    _, account_id = make_user()
    monkeypatch.setattr(stream, "_fetch_account_ids", lambda: [account_id])

    def no_model(df, **kwargs):
        raise FileNotFoundError("Model or stats not found.")
    monkeypatch.setattr(stream, "predict_fraud", no_model)

    stream.run_cycle()

    rows = _streamed_rows(account_id)
    assert rows and all(score is None and scored_at is None for _, score, scored_at, _, _ in rows)
    today_id = int(datetime.now().strftime("%Y%m%d"))
    assert query("SELECT count(*) FROM fact_account_balance WHERE account_id = %s AND date_id = %s", (account_id, today_id)) == [(1,)]


def test_a_scoring_crash_does_not_lose_the_inserted_rows_or_skip_recurring_refresh(pg_db, make_user, monkeypatch):
    _, account_id = make_user()
    monkeypatch.setattr(stream, "_fetch_account_ids", lambda: [account_id])
    monkeypatch.setattr(stream, "predict_fraud", lambda df, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    refreshed = []
    monkeypatch.setattr(stream, "_refresh_recurring_flags", lambda ids: refreshed.append(list(ids)) or 0)

    stream.run_cycle()

    assert _streamed_rows(account_id)
    assert refreshed == [[account_id]]


@pytest.mark.skipif(not (os.path.exists(fraud_model.MODEL_PATH) and os.path.exists(fraud_model.STATS_PATH)), reason="fraud model not trained in this checkout")
def test_with_the_real_trained_model_every_streamed_row_gets_a_probability(pg_db, make_user, monkeypatch):
    _, account_id = make_user()
    monkeypatch.setattr(stream, "_fetch_account_ids", lambda: [account_id])

    stream.run_cycle()

    rows = _streamed_rows(account_id)
    assert rows and all(score is not None and 0.0 <= float(score) <= 1.0 and scored_at is not None for _, score, scored_at, _, _ in rows)


# recurring flags

def _add_tx(account_id, merchant_id, day, amount, tx_type="DEBIT", recurring=False):
    conn = db_module.get_connection()
    try:
        tx_id = uuid.uuid4().hex
        conn.cursor().execute(
            "INSERT INTO fact_transactions (transaction_id, account_id, merchant_id, category_id, date_id, transaction_ts, amount, transaction_type, is_recurring) "
            "VALUES (%s,%s,%s,'ENTERTAINMENT',%s,%s,%s,%s,%s)",
            (tx_id, account_id, merchant_id, int(day.replace("-", "")), f"{day} 10:00:00", amount, tx_type, recurring))
        conn.commit()
        return tx_id
    finally:
        conn.close()


def _flag(tx_id):
    return query("SELECT is_recurring FROM fact_transactions WHERE transaction_id = %s", (tx_id,))[0][0]


MONTHLY = ["2026-05-05", "2026-06-04", "2026-07-04"]  # 30-day gaps


def test_refresh_flags_a_stable_monthly_series_as_recurring(pg_db, make_user):
    _, account_id = make_user()
    ids = [_add_tx(account_id, "NETFLIX", d, 499.0) for d in MONTHLY]

    changed = stream._refresh_recurring_flags([account_id])

    assert changed == 3 and all(_flag(i) for i in ids)
    assert stream._refresh_recurring_flags([account_id]) == 0   # nothing changed the second time: no needless writes


def test_a_new_row_that_continues_the_series_is_flagged_recurring(pg_db, make_user):
    _, account_id = make_user()
    for d in MONTHLY:
        _add_tx(account_id, "NETFLIX", d, 499.0)
    stream._refresh_recurring_flags([account_id])

    renewal = _add_tx(account_id, "NETFLIX", "2026-08-03", 499.0)   # streamed row arrives unflagged
    assert _flag(renewal) is False
    stream._refresh_recurring_flags([account_id])

    assert _flag(renewal) is True


def test_a_new_row_that_breaks_the_amount_stability_unflags_the_whole_series(pg_db, make_user):
    """Cadence/stability is judged over the whole history: one wildly different charge means it is no
    longer a fixed subscription, and the OLDER rows must stop being flagged too."""
    _, account_id = make_user()
    ids = [_add_tx(account_id, "NETFLIX", d, 499.0) for d in MONTHLY]
    stream._refresh_recurring_flags([account_id])
    assert all(_flag(i) for i in ids)

    _add_tx(account_id, "NETFLIX", "2026-08-03", 4999.0)
    stream._refresh_recurring_flags([account_id])

    assert not any(_flag(i) for i in ids)


def test_refresh_only_touches_the_given_accounts(pg_db, make_user):
    _, mine = make_user()
    _, other = make_user()
    mine_ids = [_add_tx(mine, "NETFLIX", d, 499.0) for d in MONTHLY]
    other_ids = [_add_tx(other, "NETFLIX", d, 499.0) for d in MONTHLY]

    stream._refresh_recurring_flags([mine])

    assert all(_flag(i) for i in mine_ids)
    assert not any(_flag(i) for i in other_ids)


def test_run_cycle_extends_a_real_series_end_to_end(pg_db, make_user, monkeypatch):
    _, account_id = make_user()
    for d in MONTHLY:
        _add_tx(account_id, "NETFLIX", d, 499.0)
    stream._refresh_recurring_flags([account_id])

    monkeypatch.setattr(stream, "_fetch_account_ids", lambda: [account_id])
    monkeypatch.setattr(stream, "predict_fraud", _fake_predict(0.1))
    monkeypatch.setattr(stream, "generate_batch", lambda ids, now: pd.DataFrame([{
        "transaction_id": "x", "account_id": account_id, "merchant_id": "NETFLIX", "category_id": "ENTERTAINMENT",
        "date": now.strftime("%Y-%m-%d %H:%M:%S"), "amount": 499.0, "transaction_type": "DEBIT", "is_fraud": False, "is_recurring": False}]))

    stream.run_cycle(now=datetime(2026, 8, 3, 10, 0, 0))

    latest = query("SELECT is_recurring, fraud_score FROM fact_transactions WHERE account_id = %s ORDER BY transaction_ts DESC LIMIT 1", (account_id,))[0]
    assert latest[0] is True and float(latest[1]) == pytest.approx(0.1)
