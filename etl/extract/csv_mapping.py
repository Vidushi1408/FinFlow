"""
Turn an arbitrary bank-statement CSV into FinFlow's standard columns
(date, merchant_id, amount, transaction_type) -- with a suggested column
mapping the user can review and correct, instead of a silent guess.
"""
import io
import re

import numpy as np
import pandas as pd

MERCHANT_ID_MAX_LEN = 50  # dim_merchant / fact_transactions.merchant_id are VARCHAR(50)

FIELDS = ("date", "description", "amount", "type", "debit", "credit", "balance")
REQUIRED_HINT = "a date column, a description column, and either one amount column or separate debit/credit columns"

# Aliases are compared after normalisation (lowercase, punctuation stripped).
# Short aliases (<=3 chars) must match a whole header; longer ones may also
# match as a substring ("withdrawal amt (dr)" -> debit).
_ALIASES = {
    "date": ["date", "transaction date", "txn date", "value date", "posting date", "tran date", "trans date", "date of transaction"],
    "description": ["description", "narration", "particulars", "merchant", "details", "transaction details",
                    "remarks", "transaction remarks", "payee", "transaction description", "chq ref no narration"],
    "debit": ["debit", "withdrawal", "withdrawals", "debit amount", "withdrawal amt", "withdrawal amount", "paid out", "dr"],
    "credit": ["credit", "deposit", "deposits", "credit amount", "deposit amt", "deposit amount", "paid in", "cr"],
    "amount": ["amount", "transaction amount", "amt", "amount inr"],
    "type": ["type", "transaction type", "dr cr", "cr dr", "debit credit", "txn type", "drcr"],
    "balance": ["balance", "closing balance", "running balance", "available balance", "closing bal", "bal", "balance inr"],
}
_RESOLUTION_ORDER = ("date", "description", "debit", "credit", "balance", "amount", "type")


class MappingError(ValueError):
    """The CSV can't be turned into transactions with the given mapping."""


def _normalise(header):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(header).lower())).strip()


def read_csv_bytes(contents):
    """Parse uploaded bytes, tolerating a BOM and non-UTF-8 (Excel) encodings."""
    last_error = None
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            df = pd.read_csv(io.BytesIO(contents), encoding=encoding, skipinitialspace=True)
            break
        except UnicodeDecodeError as e:
            last_error = e
        except Exception as e:
            raise MappingError(f"Couldn't read this file as a CSV: {e}")
    else:
        raise MappingError(f"Couldn't read this file as a CSV: {last_error}")

    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all")
    if df.empty:
        raise MappingError("The CSV has no rows.")
    return df


def suggest_mapping(columns):
    """field -> original column name (or None), each column used at most once."""
    normalised = {c: _normalise(c) for c in columns}
    taken = set()
    mapping = {field: None for field in FIELDS}

    def claim(field, predicate):
        for column, norm in normalised.items():
            if column not in taken and predicate(norm):
                mapping[field] = column
                taken.add(column)
                return True
        return False

    for field in _RESOLUTION_ORDER:
        aliases = _ALIASES[field]
        if claim(field, lambda n: n in aliases):
            continue
        long_aliases = [a for a in aliases if len(a) > 3]
        claim(field, lambda n: any(a in n for a in long_aliases))
    return mapping


def _to_number(series):
    """'1,234.50', '₹ 500', '(200.00)', '450 Dr' -> float; unparseable -> NaN."""
    def convert(value):
        if pd.isna(value):
            return np.nan
        if isinstance(value, (int, float, np.integer, np.floating)):
            return float(value)
        text = str(value).strip()
        if not text:
            return np.nan
        negative = text.startswith("(") and text.endswith(")") or bool(re.search(r"\bdr\b\.?$", text, re.I))
        cleaned = re.sub(r"[^0-9.\-]", "", text)
        if cleaned in ("", "-", "."):
            return np.nan
        try:
            number = float(cleaned)
        except ValueError:
            return np.nan
        return -abs(number) if negative else number
    return series.map(convert)


def _parse_dates(series):
    """Indian statements are day-first (05/06/2025 is 5 June); pick whichever
    reading parses more rows, preferring day-first on a tie."""
    dayfirst = pd.to_datetime(series, errors="coerce", dayfirst=True, format="mixed")
    monthfirst = pd.to_datetime(series, errors="coerce", dayfirst=False, format="mixed")
    return monthfirst if monthfirst.notna().sum() > dayfirst.notna().sum() else dayfirst


def _type_from_label(label):
    text = str(label).strip().lower()
    if re.search(r"\b(cr|credit|deposit)\b", text):
        return "CREDIT"
    if re.search(r"\b(dr|debit|withdraw\w*)\b", text):
        return "DEBIT"
    return None


def apply_mapping(df, mapping):
    """
    Returns (standard_df, issues). standard_df has date, merchant_id, amount
    (positive) and transaction_type. issues is a list of {"level", "message"}
    the UI shows the user. Raises MappingError if nothing usable results.
    """
    mapping = {k: (v or None) for k, v in (mapping or {}).items()}
    for field, column in mapping.items():
        if column is not None and column not in df.columns:
            raise MappingError(f"Column '{column}' (mapped to {field}) isn't in the file.")

    has_split = mapping.get("debit") or mapping.get("credit")
    if not mapping.get("date") or not mapping.get("description") or not (mapping.get("amount") or has_split):
        raise MappingError(f"We need {REQUIRED_HINT}.")

    issues = []
    out = pd.DataFrame(index=df.index)

    out["date"] = _parse_dates(df[mapping["date"]])
    description = df[mapping["description"]].fillna("").astype(str).str.strip()
    blank_description = description == ""
    if blank_description.any():
        description = description.where(~blank_description, "NO DESCRIPTION")
        issues.append({"level": "info", "message": f"{int(blank_description.sum())} row(s) had no description and were labelled NO DESCRIPTION."})
    out["description"] = description
    out["merchant_id"] = description.str.slice(0, MERCHANT_ID_MAX_LEN)
    truncated = int((description.str.len() > MERCHANT_ID_MAX_LEN).sum())
    if truncated:
        issues.append({"level": "info", "message": f"{truncated} description(s) longer than {MERCHANT_ID_MAX_LEN} characters were shortened."})

    if has_split:
        debit = _to_number(df[mapping["debit"]]).abs() if mapping.get("debit") else pd.Series(np.nan, index=df.index)
        credit = _to_number(df[mapping["credit"]]).abs() if mapping.get("credit") else pd.Series(np.nan, index=df.index)
        is_debit = debit.fillna(0) > 0
        is_credit = credit.fillna(0) > 0
        out["amount"] = np.where(is_debit, debit, np.where(is_credit, credit, np.nan))
        out["transaction_type"] = np.where(is_debit, "DEBIT", np.where(is_credit, "CREDIT", None))
    else:
        raw_amount = _to_number(df[mapping["amount"]])
        out["amount"] = raw_amount.abs()
        inferred = np.where(raw_amount < 0, "DEBIT", "CREDIT" if (raw_amount < 0).any() else "DEBIT")
        transaction_type = pd.Series(inferred, index=df.index, dtype=object)
        if mapping.get("type"):
            labelled = df[mapping["type"]].map(_type_from_label)
            transaction_type = labelled.where(labelled.notna(), transaction_type)
        out["transaction_type"] = transaction_type

    out["balance"] = _to_number(df[mapping["balance"]]) if mapping.get("balance") else np.nan

    # Position in chronological order, for ordering rows within the same day.
    # Statements are usually oldest-first, but some banks export newest-first.
    valid_dates = out["date"].dropna()
    newest_first = len(valid_dates) > 1 and valid_dates.iloc[0] > valid_dates.iloc[-1]
    out["seq"] = np.arange(len(out))[::-1] if newest_first else np.arange(len(out))

    total = len(out)
    bad_date = out["date"].isna()
    bad_amount = out["amount"].isna() | (out["amount"] == 0)
    if bad_date.sum() > total / 2:
        raise MappingError(f"Most values in the '{mapping['date']}' column couldn't be read as dates. Pick a different date column.")
    if bad_amount.sum() > total / 2:
        raise MappingError("Most amounts couldn't be read as numbers. Check which column holds the amount.")

    skipped = int((bad_date | bad_amount).sum())
    if skipped:
        issues.append({"level": "warning", "message": f"{skipped} of {total} row(s) were skipped (missing/invalid date or amount)."})
    out = out[~(bad_date | bad_amount)].reset_index(drop=True)
    if out.empty:
        raise MappingError("No usable rows were found in this file.")

    future = int((out["date"] > pd.Timestamp.now()).sum())
    if future:
        issues.append({"level": "warning", "message": f"{future} row(s) are dated in the future and will be ignored."})

    return out, issues


def estimate_opening_balance(df):
    """
    The balance just before the earliest row, inferred from the statement's own
    running-balance column: for every row, (balance after it) minus (net of all
    rows up to and including it) should be the same number -- the opening
    balance. Returns that number if the rows agree, otherwise None (no balance
    column, or one that doesn't reconcile with the amounts, in which case
    guessing would just be inventing a number again).
    Expects date, amount, transaction_type, balance and seq columns.
    """
    if "balance" not in df.columns or df["balance"].notna().sum() == 0:
        return None
    ordered = df.sort_values(["date", "seq"], kind="stable")
    signed = np.where(ordered["transaction_type"] == "CREDIT", ordered["amount"], -ordered["amount"])
    estimates = (ordered["balance"] - pd.Series(signed, index=ordered.index).cumsum()).dropna().round(2)
    if estimates.empty:
        return None
    most_common = estimates.mode().iloc[0]
    if (estimates == most_common).mean() < 0.6:
        return None
    return float(most_common)
