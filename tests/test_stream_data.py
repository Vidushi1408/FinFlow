from datetime import datetime

import pytest

import scripts.stream_data as stream
from data_generator.generate_mock_data import MERCHANTS

NOW = datetime(2026, 9, 19, 15, 0, 0)
VALID_MERCHANT_IDS = {m["name"].replace(" ", "_").upper() for m in MERCHANTS} | {"TRANSFER"}


def test_generate_batch_uses_only_requested_accounts_and_caps_at_accounts_per_cycle(monkeypatch):
    monkeypatch.setattr(stream, "ACCOUNTS_PER_CYCLE", 3)
    accounts = [f"acc-{i}" for i in range(10)]
    df = stream.generate_batch(accounts, NOW)
    assert set(df["account_id"]) <= set(accounts)
    assert df["account_id"].nunique() == 3


def test_generate_batch_with_fewer_accounts_than_cap(monkeypatch):
    monkeypatch.setattr(stream, "ACCOUNTS_PER_CYCLE", 5)
    df = stream.generate_batch(["only-one"], NOW)
    assert set(df["account_id"]) == {"only-one"}


def test_generate_batch_rows_per_account_within_range():
    df = stream.generate_batch([f"acc-{i}" for i in range(20)], NOW)
    low, high = stream.TX_PER_ACCOUNT_RANGE
    counts = df.groupby("account_id").size()
    assert counts.between(low, high).all()


def test_generate_batch_rows_are_valid_for_the_fact_table():
    df = stream.generate_batch([f"acc-{i}" for i in range(20)], NOW)
    assert (df["amount"] > 0).all()  # fact_transactions CHECK (amount > 0)
    assert set(df["transaction_type"]) <= {"DEBIT", "CREDIT"}
    assert set(df["merchant_id"]) <= VALID_MERCHANT_IDS  # FK to dim_merchant
    assert (df["date"] == NOW.strftime("%Y-%m-%d %H:%M:%S")).all()
    assert df["transaction_id"].is_unique


def test_generate_batch_never_picks_subscription_merchants_for_spend():
    recurring_ids = {m["name"].replace(" ", "_").upper() for m in MERCHANTS if m.get("is_recurring")}
    for _ in range(20):
        df = stream.generate_batch([f"acc-{i}" for i in range(10)], NOW)
        debit_merchants = set(df.loc[df["transaction_type"] == "DEBIT", "merchant_id"])
        assert not (debit_merchants & recurring_ids)


def test_credits_are_categorized_as_transfer(monkeypatch):
    monkeypatch.setattr(stream.random, "random", lambda: 0.0)  # force every row to be a credit
    df = stream.generate_batch(["a"], NOW)
    assert (df["transaction_type"] == "CREDIT").all()
    assert (df["merchant_id"] == "TRANSFER").all()
    assert (df["category_id"] == "TRANSFER").all()
    assert df["amount"].between(500, 4000).all()


def test_upsert_balances_with_no_rows_does_not_touch_the_database(monkeypatch):
    def fail():
        raise AssertionError("should not connect")
    monkeypatch.setattr(stream, "get_connection", fail)
    stream._upsert_balances([])


def test_main_once_runs_a_single_cycle(monkeypatch):
    calls = []
    monkeypatch.setattr(stream, "run_cycle", lambda: calls.append(1))
    monkeypatch.setattr("sys.argv", ["stream_data.py", "--once"])
    stream.main()
    assert calls == [1]


def test_main_loop_survives_a_failed_cycle_and_sleeps_between_cycles(monkeypatch):
    outcomes = iter([RuntimeError("db down"), None, KeyboardInterrupt()])
    slept = []

    def fake_cycle():
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(stream, "run_cycle", fake_cycle)
    monkeypatch.setattr(stream.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr("sys.argv", ["stream_data.py", "--interval-minutes", "2"])

    with pytest.raises(KeyboardInterrupt):
        stream.main()
    assert slept == [120, 120]  # slept after the failed cycle AND after the good one
