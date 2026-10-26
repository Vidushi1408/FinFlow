"""Turns a user's own transaction history into concrete, ranked money suggestions.

The numbers and the rules live here, in plain Python, so every suggestion is reproducible and
testable; the local LLM only explains and discusses them. All figures are derived from the
caller's own accounts. This is general budgeting guidance, not regulated investment or tax advice.
"""
from datetime import date

import pandas as pd
from sqlalchemy import text

from etl.transform.clean import summarize_recurring_series

BUFFER_TARGET_MONTHS = 3
LOW_SAVINGS_RATE = 0.10
HEALTHY_SAVINGS_RATE = 0.20
SUBSCRIPTION_SHARE_LIMIT = 0.10
CATEGORY_SPIKE_RATIO = 1.3
CATEGORY_SPIKE_MIN_RUPEES = 500

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _month_back(today, n):
    y, m = today.year, today.month - n
    while m <= 0:
        y, m = y - 1, m + 12
    return y, m


def build_financial_profile(db, user_id, account_ids, today=None):
    """Aggregate what the rules need. Averages use the last 3 *complete* months so a half-finished
    current month doesn't read as a sudden drop in income or spending."""
    today = today or date.today()
    profile = {
        "avg_monthly_income": 0.0, "avg_monthly_spend": 0.0, "savings_rate": None,
        "balance": 0.0, "buffer_months": None, "months_of_history": 0,
        "category_this_month": {}, "category_avg": {}, "budgets": [],
        "subscriptions": [], "subscription_annual_cost": 0.0, "goals": [],
    }
    if not account_ids:
        return profile

    prior = [_month_back(today, n) for n in (1, 2, 3)]
    rows = db.execute(text("""
        SELECT d.year, d.month, f.transaction_type, f.category_id, SUM(f.amount)
        FROM fact_transactions f JOIN dim_date d ON f.date_id = d.date_id
        WHERE f.account_id = ANY(:aids) AND d.date >= :start
        GROUP BY d.year, d.month, f.transaction_type, f.category_id
    """), {"aids": account_ids, "start": date(prior[-1][0], prior[-1][1], 1)}).fetchall()

    income, spend, cat_totals, months_seen = {}, {}, {}, set()
    for y, m, ttype, cat, amount in rows:
        ym, amount = (int(y), int(m)), float(amount)
        if ym == (today.year, today.month):
            if ttype == "DEBIT":
                profile["category_this_month"][cat] = profile["category_this_month"].get(cat, 0.0) + amount
        elif ym in prior:
            months_seen.add(ym)
            if ttype == "DEBIT":
                spend[ym] = spend.get(ym, 0.0) + amount
                cat_totals[cat] = cat_totals.get(cat, 0.0) + amount
            else:
                income[ym] = income.get(ym, 0.0) + amount

    n = len(months_seen)
    profile["months_of_history"] = n
    if n:
        profile["avg_monthly_income"] = sum(income.values()) / n
        profile["avg_monthly_spend"] = sum(spend.values()) / n
        profile["category_avg"] = {c: v / n for c, v in cat_totals.items()}
        if profile["avg_monthly_income"] > 0:
            profile["savings_rate"] = (profile["avg_monthly_income"] - profile["avg_monthly_spend"]) / profile["avg_monthly_income"]

    profile["balance"] = float(db.execute(text("""
        SELECT COALESCE(SUM(latest.closing_balance), 0) FROM (
            SELECT DISTINCT ON (account_id) account_id, closing_balance
            FROM fact_account_balance WHERE account_id = ANY(:aids) ORDER BY account_id, date_id DESC
        ) latest
    """), {"aids": account_ids}).scalar() or 0)
    if profile["avg_monthly_spend"] > 0:
        profile["buffer_months"] = profile["balance"] / profile["avg_monthly_spend"]

    for cat, limit in db.execute(
        text("SELECT category_id, monthly_limit FROM user_category_budget WHERE user_id = :uid"), {"uid": user_id}
    ).fetchall():
        if float(limit) > 0:
            profile["budgets"].append({
                "category_id": cat, "limit": float(limit),
                "spent": profile["category_this_month"].get(cat, 0.0),
            })

    tx = pd.read_sql(text("""
        SELECT transaction_id, account_id, merchant_id, amount, transaction_ts AS date
        FROM fact_transactions WHERE account_id = ANY(:aids) AND transaction_type = 'DEBIT'
    """), db.connection(), params={"aids": account_ids})
    if not tx.empty:
        tx["date"] = pd.to_datetime(tx["date"])
        overrides = {
            (r[0], r[1]): r[2] for r in db.execute(
                text("SELECT account_id, merchant_id, status FROM subscription_override WHERE user_id = :uid"),
                {"uid": user_id}).fetchall()
        }
        for _, r in summarize_recurring_series(tx).iterrows():
            if overrides.get((r["account_id"], r["merchant_id"])) == "NOT_SUBSCRIPTION":
                continue
            profile["subscriptions"].append({
                "merchant_id": r["merchant_id"], "cadence": r["cadence"],
                "typical_amount": float(r["typical_amount"]), "annual_cost": float(r["annualized_cost"]),
            })
        profile["subscriptions"].sort(key=lambda s: s["annual_cost"], reverse=True)
        profile["subscription_annual_cost"] = sum(s["annual_cost"] for s in profile["subscriptions"])

    savings_per_month = profile["avg_monthly_income"] - profile["avg_monthly_spend"]
    for name, target, deadline in db.execute(
        text("SELECT name, target_amount, deadline FROM goal WHERE user_id = :uid"), {"uid": user_id}
    ).fetchall():
        months_left = max((deadline - today).days / 30.0, 1 / 30.0)
        required = float(target) / months_left
        profile["goals"].append({
            "name": name, "target_amount": float(target), "deadline": str(deadline),
            "required_monthly": required, "on_track": savings_per_month >= required,
        })
    return profile


def _inr(x):
    return f"₹{x:,.0f}"


def build_recommendations(p):
    """Ranked list of {id, priority, title, detail, monthly_impact}. Pure function of the profile."""
    recs = []

    def add(rid, priority, title, detail, impact=None):
        recs.append({"id": rid, "priority": priority, "title": title, "detail": detail,
                     "monthly_impact": round(impact, 2) if impact else None})

    if p["months_of_history"] == 0:
        return [{"id": "need-history", "priority": "low", "title": "Not enough history yet",
                 "detail": "Add a full month of transactions and FinFlow can start suggesting specific changes.",
                 "monthly_impact": None}]

    income, spend, rate = p["avg_monthly_income"], p["avg_monthly_spend"], p["savings_rate"]

    if rate is not None and rate < 0:
        add("spending-exceeds-income", "high", "You're spending more than you earn",
            f"Over the last {p['months_of_history']} full month(s) you averaged {_inr(spend)} out "
            f"against {_inr(income)} in. Closing a {_inr(spend - income)}/month gap comes before any saving or investing.",
            spend - income)
    elif rate is not None and rate < LOW_SAVINGS_RATE:
        target = income * LOW_SAVINGS_RATE
        add("low-savings-rate", "high", "Your savings rate is thin",
            f"You're keeping about {rate * 100:.0f}% of income. Aiming for at least 10% means setting aside "
            f"{_inr(target)}/month; automating it on payday works better than saving what's left over.",
            target - (income - spend))

    bm = p["buffer_months"]
    if bm is not None and bm < BUFFER_TARGET_MONTHS:
        shortfall = spend * BUFFER_TARGET_MONTHS - p["balance"]
        add("emergency-buffer", "high" if bm < 1 else "medium", "Build an emergency buffer first",
            f"Your balance covers about {max(bm, 0):.1f} months of spending. A common target is "
            f"{BUFFER_TARGET_MONTHS} months ({_inr(spend * BUFFER_TARGET_MONTHS)}), so {_inr(shortfall)} to go. "
            "Keep it somewhere safe and easy to withdraw from.")

    for b in p["budgets"]:
        if b["spent"] >= b["limit"]:
            add(f"budget-over-{b['category_id']}", "medium", f"Over budget on {b['category_id']}",
                f"{_inr(b['spent'])} spent against a {_inr(b['limit'])} limit this month. "
                "Either trim it for the rest of the month or raise the limit if it was set unrealistically.",
                b["spent"] - b["limit"])

    spikes = []
    for cat, now in p["category_this_month"].items():
        avg = p["category_avg"].get(cat, 0.0)
        if avg > 0 and now > avg * CATEGORY_SPIKE_RATIO and now - avg > CATEGORY_SPIKE_MIN_RUPEES:
            spikes.append((now - avg, cat, now, avg))
    for extra, cat, now, avg in sorted(spikes, reverse=True)[:2]:
        add(f"category-spike-{cat}", "medium", f"{cat} is running high",
            f"{_inr(now)} so far this month versus a usual {_inr(avg)}. If it isn't a one-off, "
            "that's the quickest place to claw money back.", extra)

    income_base = income if income > 0 else spend
    if p["subscriptions"] and income_base > 0:
        share = (p["subscription_annual_cost"] / 12) / income_base
        top = p["subscriptions"][0]
        if share > SUBSCRIPTION_SHARE_LIMIT:
            add("subscription-load", "medium", "Subscriptions take a big slice",
                f"Recurring payments total about {_inr(p['subscription_annual_cost'] / 12)}/month "
                f"({share * 100:.0f}% of income). The biggest is {top['merchant_id']} at "
                f"{_inr(top['annual_cost'])}/year; cancel whatever you haven't used in a month.",
                p["subscription_annual_cost"] / 12 * 0.25)
        elif top["annual_cost"] >= 6000:
            add("subscription-review", "low", f"Review {top['merchant_id']}",
                f"It costs about {_inr(top['annual_cost'])}/year. Worth confirming you still use it.",
                top["annual_cost"] / 12)

    for goal in p["goals"]:
        if not goal["on_track"]:
            gap = goal["required_monthly"] - (income - spend)
            add(f"goal-off-track-{goal['name']}", "medium", f"Goal '{goal['name']}' is off track",
                f"It needs {_inr(goal['required_monthly'])}/month to hit {_inr(goal['target_amount'])} by "
                f"{goal['deadline']}; you're short by about {_inr(max(gap, 0))}/month. Options: push the deadline, "
                "lower the target, or free up the gap from the categories above.", max(gap, 0))

    if rate is not None and rate >= HEALTHY_SAVINGS_RATE and (bm is None or bm >= BUFFER_TARGET_MONTHS):
        surplus = income - spend
        add("put-surplus-to-work", "low", "You have room to put surplus to work",
            f"You keep about {rate * 100:.0f}% of income ({_inr(surplus)}/month) and your buffer is covered. "
            "Consider directing it at a goal, or learning about long-term options such as index funds, PPF "
            "or fixed deposits, matched to your time horizon and risk comfort. A SEBI-registered adviser "
            "can give personalised advice.", surplus)

    if not recs:
        add("on-track", "low", "Nothing urgent",
            "Savings, buffer, budgets and goals all look healthy. Keep the routine going.")

    recs.sort(key=lambda r: (PRIORITY_ORDER[r["priority"]], -(r["monthly_impact"] or 0)))
    return recs
