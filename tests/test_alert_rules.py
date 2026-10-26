import pandas as pd

from webapp.services.alert_rules import evaluate


def _df(rows):
    return pd.DataFrame(rows, columns=["transaction_id", "merchant_id", "amount", "transaction_ts"])


def _hits(rows, target, median=500.0):
    df = _df(rows)
    return evaluate(df[df["transaction_id"].isin(target)], df, median)


def test_first_payment_to_a_payee_alerts_only_when_large_and_far_above_usual():
    rows = [("a", "SWIGGY", 400, "2026-01-01"), ("b", "UNKNOWN LTD", 25000, "2026-01-02"), ("c", "CAFE", 900, "2026-01-03")]
    hits = _hits(rows, ["b", "c"])
    assert "first payment ever" in hits["b"][0] and "c" not in hits


def test_risky_merchant_type_alerts_above_the_minimum_even_when_known():
    rows = [("a", "GLOBAL GIFTCARDS HK", 19999, "2026-02-01"), ("b", "STEAM GIFT CARD", 500, "2026-02-02")]
    hits = _hits(rows, ["a", "b"])
    assert any("scams or cash-outs" in r for r in hits["a"]) and "b" not in hits   # small amounts stay quiet


def test_a_known_merchant_charging_triple_its_maximum_alerts_and_normal_repeats_do_not():
    rows = [("a", "ELECTRICITY BOARD", 1500, "2026-01-01"), ("b", "ELECTRICITY BOARD", 1800, "2026-02-01"),
            ("c", "ELECTRICITY BOARD", 7200, "2026-03-01"), ("d", "ELECTRICITY BOARD", 1700, "2026-04-01")]
    hits = _hits(rows, ["b", "c", "d"])
    assert list(hits) == ["c"] and "4.0×" in hits["c"][0]


def test_only_earlier_transactions_count_as_history():
    # the big payment is the FIRST to this payee even though a later, smaller one exists
    rows = [("a", "NEW PAYEE", 30000, "2026-01-01"), ("b", "NEW PAYEE", 500, "2026-02-01")]
    assert "a" in _hits(rows, ["a"]) and "b" not in _hits(rows, ["b"])


def test_empty_window_is_fine():
    assert evaluate(_df([]), _df([]), 500.0) == {}
