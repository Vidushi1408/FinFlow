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


from webapp.services.advisor import affordability, computed_facts_for, parse_amount  # noqa: E402


def test_parse_amount_handles_indian_formats():
    assert parse_amount("can I afford ₹20,000?") == 20000
    assert parse_amount("buy a phone for 45k") == 45000
    assert parse_amount("a 1.5 lakh trip") == 150000
    assert parse_amount("how am I doing?") is None


def test_affordability_verdicts():
    p = profile(balance=100000.0, avg_monthly_spend=30000.0)
    assert "comfortably" in affordability(p, 5000)["verdict"]
    assert "below the 3-month" in affordability(p, 30000)["verdict"]
    assert "under one month" in affordability(p, 80000)["verdict"]
    assert "not affordable" in affordability(p, 150000)["verdict"]
    assert affordability(p, 20000)["months_of_surplus_to_recover"] == 1.0


def test_computed_facts_only_for_purchase_questions():
    p = profile()
    assert "affordability_check" in computed_facts_for("Can I afford a ₹20,000 purchase?", p)
    assert "affordability_check" not in computed_facts_for("Where can I cut spending?", p)


def test_figure_verification_compares_numbers_not_text():
    from webapp.services.advisor import fix_currency, grounded_fallback, unverified_figures
    ctx = '{"amount": 9000.0, "limit": 5000}'
    assert fix_currency("You spent $12,000.") == "You spent ₹12,000."
    assert unverified_figures("Dining was ₹9,000 vs ₹5,000.", ctx, "q") == []
    assert unverified_figures("You spent ₹99,999.", ctx, "q") == ["99,999"]
    assert unverified_figures("Can I afford ₹20,000?", ctx, "afford 20000") == []
    fallback = grounded_fallback([{"id": "x", "title": "T", "detail": "D"}])
    assert "T: D" in fallback and "couldn't verify" in fallback


def test_whatif_cut_computes_from_category_average():
    from webapp.services.advisor import computed_facts_for, whatif_reply
    p = profile(category_avg={"Dining": 5000.0, "Rent": 18000.0}, balance=100000.0)
    facts = computed_facts_for("What if I cut dining by 20%?", p)
    w = facts["whatif_cut"]
    assert w["category"] == "Dining" and w["monthly_saving"] == 1000.0
    assert w["balance_in_6_months"] == 100000 + (20000 + 1000) * 6
    assert "₹1,000/month" in whatif_reply(facts)
    assert "whatif_cut" not in computed_facts_for("cut my spending", p)          # no % or category
    assert "whatif_cut" not in computed_facts_for("cut groceries by 10%", p)      # unknown category


def test_whatif_understands_everyday_words_for_the_apps_categories():
    from webapp.services.advisor import computed_facts_for
    p = profile(category_avg={"FOOD": 8000.0, "GROCERY": 6000.0, "TRANSPORT": 3000.0})
    assert computed_facts_for("What if I cut dining by 20%?", p)["whatif_cut"]["category"] == "FOOD"
    assert computed_facts_for("reduce groceries by 10 percent", p)["whatif_cut"]["category"] == "GROCERY"
    assert computed_facts_for("trim cab spending by 50%", p)["whatif_cut"]["category"] == "TRANSPORT"
    assert "whatif_cut" not in computed_facts_for("cut movies by 20%", p)      # ENTERTAINMENT isn't in this user's data


def test_what_should_i_do_is_answered_from_the_ranked_rules():
    from webapp.services.advisor import computed_facts_for, priorities_reply
    p = profile(avg_monthly_spend=47000.0, savings_rate=0.06, buffer_months=1.5, balance=70000.0)
    assert computed_facts_for("What should I do first with my money?", p)["priority_question"]
    assert "priority_question" not in computed_facts_for("Where can I cut spending?", p)
    reply = priorities_reply(build_recommendations(p))
    assert reply.startswith("Here's what I'd do") and "1. " in reply and "Next step" in reply
    assert "Nothing urgent" in priorities_reply([{"id": "on-track", "title": "x", "detail": "y"}])


def test_where_to_cut_and_am_i_saving_enough_are_answered_from_the_data():
    from webapp.services.advisor import computed_facts_for, cut_spending_reply, saving_reply
    p = profile(category_avg={"RENT": 24000.0, "FOOD": 9000.0, "SHOPPING": 6000.0, "FINANCE": 13000.0},
                category_this_month={"SHOPPING": 12000.0})
    assert computed_facts_for("Where can I cut spending?", p)["cut_question"]
    assert computed_facts_for("Am I saving enough?", p)["saving_question"]
    reply = cut_spending_reply(p)
    assert "1. Food" in reply and "2. Shopping" in reply and "Rent:" not in reply and "Finance:" not in reply
    assert "Shopping is also running high" in reply
    assert "decent" in saving_reply(profile(savings_rate=0.15, avg_monthly_income=60000.0, avg_monthly_spend=51000.0))
    assert "thin" in saving_reply(profile(savings_rate=0.05, avg_monthly_income=60000.0, avg_monthly_spend=57000.0))
    assert "not saving" in saving_reply(profile(savings_rate=-0.1, avg_monthly_income=50000.0, avg_monthly_spend=55000.0))
    assert "no income" in saving_reply(profile(savings_rate=None))


def test_plain_text_strips_markdown_but_keeps_content():
    from webapp.services.advisor import plain_text
    assert plain_text("**Next step:** Check fees.\n* one\n- two\n## Title") == "Next step: Check fees.\n• one\n• two\nTitle"
