"""Every fixture statement must auto-map, import with the right debit/credit split, and (when it has a
running balance) reconcile an opening balance. See tests/fixtures/bank_statements/README.md for the caveat."""
from pathlib import Path

import pytest

from etl.extract.csv_mapping import apply_mapping, estimate_opening_balance, read_csv_bytes, suggest_mapping

FIXTURES = Path(__file__).parent / "fixtures" / "bank_statements"
# name -> (rows, debits, credits, total debit, has running balance)
EXPECTED = {
    "hdfc_style.csv": (4, 3, 1, 6789.50, True),
    "icici_style.csv": (3, 2, 1, 1789.50, True),
    "sbi_style.csv": (3, 2, 1, 1789.50, True),
    "axis_style.csv": (3, 2, 1, 1789.50, True),
    "upi_app_style.csv": (3, 2, 1, 1789.50, False),
    "signed_amount_generic.csv": (3, 2, 1, 1789.50, False),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_statement_imports_with_correct_split(name):
    rows, debits, credits, total_debit, has_balance = EXPECTED[name]
    df = read_csv_bytes((FIXTURES / name).read_bytes())
    mapping = suggest_mapping(list(df.columns))
    assert mapping["date"] and mapping["description"], f"{name}: date/description not detected: {mapping}"

    std, issues = apply_mapping(df, mapping)
    assert len(std) == rows, (name, issues)
    assert (std["transaction_type"] == "DEBIT").sum() == debits
    assert (std["transaction_type"] == "CREDIT").sum() == credits
    assert std.loc[std["transaction_type"] == "DEBIT", "amount"].sum() == pytest.approx(total_debit)
    assert not [i for i in issues if i["level"] == "error"]
    assert std["date"].dt.year.eq(2025).all() and std["date"].dt.month.eq(9).all()

    assert bool(mapping["balance"]) == has_balance


def test_hdfc_style_balance_reconciles_to_the_known_opening_balance():
    df = read_csv_bytes((FIXTURES / "hdfc_style.csv").read_bytes())
    mapping = suggest_mapping(list(df.columns))
    std, _ = apply_mapping(df, mapping)
    assert estimate_opening_balance(std) == pytest.approx(50000.00)


def test_iso_dates_are_year_month_day_not_day_first():
    df = read_csv_bytes(b"Date,Description,Amount\n2025-09-01,X,-10\n2025-09-13,Y,-10\n")
    std, _ = apply_mapping(df, suggest_mapping(list(df.columns)))
    assert list(std["date"].dt.strftime("%Y-%m-%d")) == ["2025-09-01", "2025-09-13"]
