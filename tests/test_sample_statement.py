from datetime import date

import pytest

from etl.extract.csv_mapping import apply_mapping, estimate_opening_balance, read_csv_bytes, suggest_mapping
from scripts.generate_sample_statement import OPENING_BALANCE, generate, write_csv

END = date(2026, 10, 26)


def test_generator_is_deterministic_and_spans_a_year():
    a, b = generate(END, 7), generate(END, 7)
    assert a == b and generate(END, 8) != a
    assert (a[-1][0] - a[0][0]).days >= 330 and a[0][0] >= date(2025, 10, 27)
    assert len({(d.year, d.month) for d, *_ in a}) >= 12   # a 365-day window touches 13 calendar months


def test_running_balance_reconciles_and_imports_through_the_real_mapper(tmp_path):
    rows = generate(END, 7)
    balance = OPENING_BALANCE
    for _, _, _, withdrawal, deposit, closing in rows:
        balance = round(balance - withdrawal + deposit, 2)
        assert closing == balance and (withdrawal == 0) != (deposit == 0)

    path = write_csv(rows, tmp_path / "s.csv")
    df = read_csv_bytes(path.read_bytes())
    mapping = suggest_mapping(list(df.columns))
    std, issues = apply_mapping(df, mapping)
    assert len(std) == len(rows) and not [i for i in issues if i["level"] == "error"]
    assert estimate_opening_balance(std) == pytest.approx(OPENING_BALANCE)
