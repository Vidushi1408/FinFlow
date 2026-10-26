from webapp.services.advisor import build_recommendations


def profile(**over):
    base = {
        "avg_monthly_income": 50000.0, "avg_monthly_spend": 30000.0, "savings_rate": 0.4,
        "balance": 200000.0, "buffer_months": 6.0, "months_of_history": 3,
        "category_this_month": {}, "category_avg": {}, "budgets": [],
        "subscriptions": [], "subscription_annual_cost": 0.0, "goals": [],
    }
    base.update(over)
    return base


def ids(recs):
    return [r["id"] for r in recs]


def test_no_history_asks_for_more_data():
    assert ids(build_recommendations(profile(months_of_history=0))) == ["need-history"]


def test_overspending_ranks_first_and_beats_other_advice():
    recs = build_recommendations(profile(avg_monthly_spend=60000.0, savings_rate=-0.2, buffer_months=0.5, balance=30000.0))
    assert recs[0]["id"] == "spending-exceeds-income"
    assert "emergency-buffer" in ids(recs)
    assert "put-surplus-to-work" not in ids(recs)


def test_low_savings_rate_and_thin_buffer():
    recs = build_recommendations(profile(avg_monthly_spend=47000.0, savings_rate=0.06, buffer_months=1.5, balance=70000.0))
    assert {"low-savings-rate", "emergency-buffer"} <= set(ids(recs))


def test_healthy_profile_suggests_putting_surplus_to_work_without_naming_products():
    recs = build_recommendations(profile())
    surplus = next(r for r in recs if r["id"] == "put-surplus-to-work")
    assert "SEBI" in surplus["detail"]


def test_over_budget_category_spike_and_off_track_goal():
    recs = build_recommendations(profile(
        budgets=[{"category_id": "Dining", "limit": 3000.0, "spent": 4500.0}],
        category_this_month={"Shopping": 9000.0}, category_avg={"Shopping": 4000.0},
        goals=[{"name": "Trip", "target_amount": 120000.0, "deadline": "2027-01-01", "required_monthly": 30000.0, "on_track": False}],
    ))
    assert {"budget-over-Dining", "category-spike-Shopping", "goal-off-track-Trip"} <= set(ids(recs))


def test_heavy_subscriptions_flagged():
    subs = [{"merchant_id": "StreamCo", "cadence": "monthly", "typical_amount": 500.0, "annual_cost": 72000.0}]
    recs = build_recommendations(profile(subscriptions=subs, subscription_annual_cost=72000.0))
    assert "subscription-load" in ids(recs)
