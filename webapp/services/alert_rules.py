"""Plain-rule alert triggers that don't depend on the fraud model's score.

The model sees amounts and merchant frequency; it can miss a charge that is obviously worth a look to a
person (a first-ever payee for tens of thousands of rupees, a gift-card or forex merchant, a bill that
suddenly triples). These rules catch those, explain themselves in words, and are conservative on amount so
small first-time purchases stay quiet. A rule hit always becomes an alert (it is not hidden by the user's
personalised cutoff); "This was me" still silences it per merchant like any other alert.
"""
import re

import pandas as pd

from etl.transform.clean import merchant_key

RISKY_NAME = re.compile(
    r"gift ?cards?|forex|crypto|bitcoin|binance|casino|betting|lottery|western union|\bintl\b|international|instant ?loan|quick ?cash",
    re.I)
RISKY_MIN_AMOUNT = 3000.0          # a risky-looking name only matters above this
NEW_PAYEE_MIN_AMOUNT = 15000.0     # first-ever payee: alert only for large amounts...
NEW_PAYEE_MEDIAN_MULT = 5.0        # ...that are also far above this user's usual transaction
SURGE_MULT = 3.0                   # known merchant: this many times the most ever paid there before
SURGE_MIN_AMOUNT = 5000.0


def evaluate(window_df, history_df, median_amount):
    """{transaction_id: [reasons]} for rows of `window_df` that trip a rule.
    `history_df` is the user's wider debit history (it may include `window_df`'s rows), with columns
    transaction_id, merchant_id, amount, transaction_ts. Only transactions BEFORE a row count as its history."""
    if window_df.empty:
        return {}
    hist = history_df[["transaction_id", "merchant_id", "amount", "transaction_ts"]].copy()
    hist["_key"] = hist["merchant_id"].map(merchant_key)
    hist["transaction_ts"] = pd.to_datetime(hist["transaction_ts"])
    by_key = {k: g.sort_values("transaction_ts") for k, g in hist.groupby("_key")}

    hits = {}
    for _, row in window_df.iterrows():
        amount, key = float(row["amount"]), merchant_key(row["merchant_id"])
        ts = pd.to_datetime(row["transaction_ts"])
        prior = by_key.get(key)
        prior = prior[(prior["transaction_ts"] < ts) & (prior["transaction_id"] != row["transaction_id"])] if prior is not None else None
        reasons = []

        if amount >= RISKY_MIN_AMOUNT and RISKY_NAME.search(str(row["merchant_id"])):
            reasons.append("merchant type often used in scams or cash-outs (gift cards, forex, crypto, instant loans)")
        if (prior is None or prior.empty) and amount >= NEW_PAYEE_MIN_AMOUNT and amount >= NEW_PAYEE_MEDIAN_MULT * max(median_amount, 1.0):
            reasons.append("first payment ever to this payee, and a large one")
        elif prior is not None and not prior.empty:
            most_before = float(prior["amount"].max())
            if amount >= SURGE_MIN_AMOUNT and amount >= SURGE_MULT * most_before:
                reasons.append(f"{amount / most_before:.1f}× the most you've paid this merchant before")
        if reasons:
            hits[row["transaction_id"]] = reasons
    return hits
