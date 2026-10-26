from webapp.services.recurring import FIXED_COMMITMENT_CATEGORIES, dominant_category, recurring_kind
import pandas as pd


def test_rent_emi_and_bills_are_fixed_commitments_the_rest_subscriptions():
    for category in ("RENT", "FINANCE", "UTILITIES", "TRANSFER", "INCOME"):
        assert recurring_kind(category) == "fixed_commitment"
    for category in ("ENTERTAINMENT", "RECHARGE", "SHOPPING", "OTHER", None):
        assert recurring_kind(category) == "subscription"
    assert "RENT" in FIXED_COMMITMENT_CATEGORIES


def test_dominant_category_is_the_most_common_per_merchant():
    df = pd.DataFrame({"account_id": ["a"] * 4, "merchant_id": ["X", "X", "X", "Y"],
                       "category_id": ["FOOD", "FOOD", "OTHER", "RENT"]})
    assert dominant_category(df) == {("a", "X"): "FOOD", ("a", "Y"): "RENT"}
    assert dominant_category(df.drop(columns="category_id")) == {}
    assert dominant_category(None) == {}
