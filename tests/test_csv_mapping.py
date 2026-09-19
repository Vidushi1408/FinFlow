
import pandas as pd
import pytest

from etl.extract.csv_mapping import MappingError, apply_mapping, estimate_opening_balance, read_csv_bytes, suggest_mapping


def _df(csv_text):
    return read_csv_bytes(csv_text.encode())


def test_suggest_mapping_for_simple_statement():
    m = suggest_mapping(["Date", "Description", "Amount", "Type"])
    assert m == {"date": "Date", "description": "Description", "amount": "Amount", "type": "Type", "debit": None, "credit": None, "balance": None}


def test_suggest_mapping_for_indian_bank_debit_credit_columns():
    m = suggest_mapping(["Txn Date", "Value Date", "Narration", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"])
    assert m["date"] == "Txn Date"           # not "Value Date"
    assert m["description"] == "Narration"
    assert m["debit"] == "Withdrawal Amt."
    assert m["credit"] == "Deposit Amt."
    assert m["amount"] is None               # "Withdrawal Amt." must not also be claimed as the amount


def test_suggest_mapping_never_reuses_a_column():
    m = suggest_mapping(["Date", "Amount"])
    used = [c for c in m.values() if c]
    assert len(used) == len(set(used))


def test_suggest_mapping_unrecognised_headers_gives_none():
    assert suggest_mapping(["foo", "bar"]) == {f: None for f in ("date", "description", "amount", "type", "debit", "credit", "balance")}


def test_simple_signed_amounts_infer_debit_and_credit():
    df = _df("Date,Description,Amount\n2025-01-05,ZOMATO,-250\n2025-01-06,SALARY,50000\n")
    out, issues = apply_mapping(df, suggest_mapping(df.columns))
    assert list(out["transaction_type"]) == ["DEBIT", "CREDIT"]
    assert list(out["amount"]) == [250.0, 50000.0]
    assert issues == []


def test_all_positive_amounts_default_to_debit():
    df = _df("Date,Description,Amount\n2025-01-05,ZOMATO,250\n2025-01-06,UBER,100\n")
    out, _ = apply_mapping(df, suggest_mapping(df.columns))
    assert set(out["transaction_type"]) == {"DEBIT"}


def test_separate_debit_credit_columns():
    df = _df('Date,Narration,Withdrawal Amt.,Deposit Amt.\n05/01/2025,ZOMATO,"1,250.50",\n06/01/2025,SALARY,,"50,000.00"\n')
    out, _ = apply_mapping(df, suggest_mapping(df.columns))
    assert list(out["transaction_type"]) == ["DEBIT", "CREDIT"]
    assert list(out["amount"]) == [1250.5, 50000.0]


def test_explicit_type_column_beats_sign_inference():
    df = _df("Date,Description,Amount,Type\n2025-01-05,REFUND,100,Credit\n2025-01-06,SHOP,100,Debit\n")
    out, _ = apply_mapping(df, suggest_mapping(df.columns))
    assert list(out["transaction_type"]) == ["CREDIT", "DEBIT"]


@pytest.mark.parametrize("raw,expected", [("₹ 1,234.50", 1234.5), ("(200.00)", 200.0), ("450 Dr", 450.0), ("1200", 1200.0)])
def test_amount_formats_are_parsed(raw, expected):
    df = pd.DataFrame({"Date": ["2025-01-05"], "Description": ["X"], "Amount": [raw]})
    out, _ = apply_mapping(df, suggest_mapping(df.columns))
    assert out["amount"].iloc[0] == expected


def test_dates_are_read_day_first_for_ambiguous_indian_format():
    df = _df("Date,Description,Amount\n05/06/2025,X,10\n07/06/2025,Y,10\n")
    out, _ = apply_mapping(df, suggest_mapping(df.columns))
    assert out["date"].iloc[0] == pd.Timestamp("2025-06-05")  # 5 June, not 6 May


def test_dates_fall_back_to_month_first_when_only_that_parses():
    df = _df("Date,Description,Amount\n06/25/2025,X,10\n06/26/2025,Y,10\n")
    out, _ = apply_mapping(df, suggest_mapping(df.columns))
    assert out["date"].iloc[0] == pd.Timestamp("2025-06-25")


def test_long_descriptions_are_shortened_to_fit_the_database_column():
    long_text = "UPI-" + "A" * 80
    df = pd.DataFrame({"Date": ["2025-01-05"], "Description": [long_text], "Amount": [10]})
    out, issues = apply_mapping(df, suggest_mapping(df.columns))
    assert len(out["merchant_id"].iloc[0]) == 50
    assert out["description"].iloc[0] == long_text  # full text kept for categorization
    assert any("shortened" in i["message"] for i in issues)


def test_bad_rows_are_skipped_with_a_warning():
    df = _df("Date,Description,Amount\n2025-01-05,A,10\nnot-a-date,B,10\n2025-01-07,C,\n2025-01-08,D,5\n")
    out, issues = apply_mapping(df, suggest_mapping(df.columns))
    assert list(out["merchant_id"]) == ["A", "D"]
    assert any("skipped" in i["message"] for i in issues)


def test_future_dates_are_flagged():
    df = pd.DataFrame({"Date": ["2099-01-01", "2025-01-05"], "Description": ["A", "B"], "Amount": [1, 2]})
    _, issues = apply_mapping(df, suggest_mapping(df.columns))
    assert any("future" in i["message"] for i in issues)


def test_mostly_unparseable_dates_is_an_error():
    df = pd.DataFrame({"Date": ["x", "y", "z"], "Description": list("abc"), "Amount": [1, 2, 3]})
    with pytest.raises(MappingError, match="dates"):
        apply_mapping(df, suggest_mapping(df.columns))


def test_mostly_unparseable_amounts_is_an_error():
    df = pd.DataFrame({"Date": ["2025-01-05"] * 3, "Description": list("abc"), "Amount": ["x", "y", "z"]})
    with pytest.raises(MappingError, match="amounts"):
        apply_mapping(df, suggest_mapping(df.columns))


def test_missing_required_mapping_is_an_error():
    df = pd.DataFrame({"a": [1], "b": [2]})
    with pytest.raises(MappingError, match="We need"):
        apply_mapping(df, {"date": "a"})


def test_mapping_to_nonexistent_column_is_an_error():
    df = pd.DataFrame({"a": [1]})
    with pytest.raises(MappingError, match="isn't in the file"):
        apply_mapping(df, {"date": "nope", "description": "a", "amount": "a"})


def test_user_can_override_the_suggested_mapping():
    df = pd.DataFrame({"When": ["2025-01-05"], "What": ["ZOMATO"], "How much": [99]})
    assert suggest_mapping(df.columns)["date"] is None
    out, _ = apply_mapping(df, {"date": "When", "description": "What", "amount": "How much"})
    assert out["amount"].iloc[0] == 99


def test_read_csv_handles_bom_and_latin1():
    assert list(read_csv_bytes("﻿Date,Description,Amount\n2025-01-05,X,1\n".encode("utf-8")).columns)[0] == "Date"
    latin = "Date,Description,Amount\n2025-01-05,Caf\xe9,1\n".encode("latin-1")
    assert read_csv_bytes(latin)["Description"].iloc[0] == "Café"


def test_read_csv_rejects_empty_and_garbage():
    with pytest.raises(MappingError):
        read_csv_bytes(b"Date,Description,Amount\n")
    with pytest.raises(MappingError):
        read_csv_bytes(b"")


def test_blank_descriptions_get_a_placeholder_instead_of_nan():
    df = pd.DataFrame({"Date": ["2025-01-05", "2025-01-06"], "Description": ["ZOMATO", None], "Amount": [10, 20]})
    out, issues = apply_mapping(df, suggest_mapping(df.columns))
    assert list(out["merchant_id"]) == ["ZOMATO", "NO DESCRIPTION"]
    assert any("no description" in i["message"] for i in issues)


# --- balance column + opening balance estimation ---

STATEMENT = (
    "Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
    "01/08/2026,ZOMATO,250.00,,9750.00\n"
    "02/08/2026,SALARY,,5000.00,14750.00\n"
    "03/08/2026,UBER,150.00,,14600.00\n"
)


def _statement_std(csv_text=STATEMENT):
    df = read_csv_bytes(csv_text.encode())
    out, _ = apply_mapping(df, suggest_mapping(df.columns))
    return out


def test_balance_column_is_detected_and_not_confused_with_amount():
    m = suggest_mapping(["Date", "Narration", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"])
    assert m["balance"] == "Closing Balance" and m["amount"] is None
    assert suggest_mapping(["Date", "Description", "Amount", "Balance"])["balance"] == "Balance"
    assert suggest_mapping(["Date", "Description", "Balance Amount"])["balance"] == "Balance Amount"


def test_opening_balance_is_inferred_from_the_running_balance():
    # first row: 9750 after paying 250  ->  opening was 10000
    assert estimate_opening_balance(_statement_std()) == 10000.0


def test_opening_balance_handles_newest_first_exports():
    newest_first = (
        "Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
        "03/08/2026,UBER,150.00,,14600.00\n"
        "02/08/2026,SALARY,,5000.00,14750.00\n"
        "01/08/2026,ZOMATO,250.00,,9750.00\n"
    )
    assert estimate_opening_balance(_statement_std(newest_first)) == 10000.0


def test_opening_balance_with_several_rows_on_the_same_day():
    same_day = (
        "Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
        "01/08/2026,A,100.00,,9900.00\n"
        "01/08/2026,B,200.00,,9700.00\n"
        "01/08/2026,C,,50.00,9750.00\n"
    )
    assert estimate_opening_balance(_statement_std(same_day)) == 10000.0


def test_opening_balance_is_none_without_a_balance_column():
    df = pd.DataFrame({"Date": ["2026-08-01"], "Description": ["X"], "Amount": [-5]})
    out, _ = apply_mapping(df, suggest_mapping(df.columns))
    assert estimate_opening_balance(out) is None


def test_opening_balance_is_none_when_balances_do_not_reconcile_with_amounts():
    """A 'balance' column that doesn't add up (e.g. it's really something else) must not be trusted."""
    junk = (
        "Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
        "01/08/2026,A,100.00,,5.00\n"
        "02/08/2026,B,100.00,,999.00\n"
        "03/08/2026,C,100.00,,42.00\n"
    )
    assert estimate_opening_balance(_statement_std(junk)) is None


def test_opening_balance_tolerates_rows_missing_a_balance():
    partial = (
        "Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
        "01/08/2026,A,250.00,,9750.00\n"
        "02/08/2026,B,100.00,,\n"
        "03/08/2026,C,150.00,,9500.00\n"
    )
    assert estimate_opening_balance(_statement_std(partial)) == 10000.0


def test_balance_formats_with_commas_and_cr_dr_suffix_are_parsed():
    df = pd.DataFrame({"Date": ["2026-08-01"], "Description": ["X"], "Amount": [-250], "Balance": ["9,750.00 Cr"]})
    out, _ = apply_mapping(df, suggest_mapping(df.columns))
    assert out["balance"].iloc[0] == 9750.0
