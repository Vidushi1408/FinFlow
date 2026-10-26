"""Telling subscriptions apart from fixed commitments among recurring payments."""

# Recurring payments that are obligations or bills, not subscriptions you could simply cancel. Rent, loan
# EMIs, SIPs and insurance (FINANCE), utility bills, and money moved between your own accounts.
FIXED_COMMITMENT_CATEGORIES = frozenset({"RENT", "FINANCE", "UTILITIES", "TRANSFER", "INCOME"})


def recurring_kind(category_id):
    return "fixed_commitment" if category_id in FIXED_COMMITMENT_CATEGORIES else "subscription"


def dominant_category(tx_df):
    """{(account_id, merchant_id): most common category} from a transactions frame, or {} if it has no
    category_id column."""
    if tx_df is None or tx_df.empty or "category_id" not in tx_df.columns:
        return {}
    grouped = tx_df.dropna(subset=["category_id"]).groupby(["account_id", "merchant_id"])["category_id"]
    return {key: series.mode().iloc[0] for key, series in grouped}


def subscriptions_only(recurring_df, tx_df):
    """Rows of summarize_recurring_series' output that are real subscriptions (not rent, EMIs, SIPs, bills)."""
    if recurring_df is None or recurring_df.empty:
        return recurring_df
    categories = dominant_category(tx_df)
    keep = [recurring_kind(categories.get((r["account_id"], r["merchant_id"]))) == "subscription"
            for _, r in recurring_df.iterrows()]
    return recurring_df[keep]
