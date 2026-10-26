"""Turns a user's own transaction history into concrete, ranked money suggestions.

The numbers and the rules live here, in plain Python, so every suggestion is reproducible and
testable; the local LLM only explains and discusses them. All figures are derived from the
caller's own accounts. This is general budgeting guidance, not regulated investment or tax advice.
"""
import re
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

EMI_BURDEN_HIGH = 0.40
EMI_BURDEN_WATCH = 0.30
HIGH_INTEREST_RATE_PCT = 12.0

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
        "subscriptions": [], "subscription_annual_cost": 0.0, "goals": [], "loans": [],
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

    for name, balance, rate, emi in db.execute(
        text("SELECT name, outstanding_balance, interest_rate_pct, monthly_emi FROM loan WHERE user_id = :uid"),
        {"uid": user_id}
    ).fetchall():
        profile["loans"].append({"name": name, "balance": float(balance), "rate": float(rate), "emi": float(emi)})
    profile["loans"].sort(key=lambda loan: loan["rate"], reverse=True)

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

    loans = p.get("loans") or []
    if loans:
        total_emi = sum(loan["emi"] for loan in loans)
        burden = total_emi / income if income > 0 else None
        if burden is not None and burden > EMI_BURDEN_HIGH:
            add("emi-burden-high", "high", "EMIs are eating your income",
                f"Your EMIs total {_inr(total_emi)}/month, {burden * 100:.0f}% of income; lenders and planners "
                f"treat above {EMI_BURDEN_HIGH * 100:.0f}% as stretched. Avoid new debt, and see below for which loan to clear first.",
                total_emi - income * EMI_BURDEN_HIGH)
        elif burden is not None and burden > EMI_BURDEN_WATCH:
            add("emi-burden-watch", "medium", "EMIs are a heavy share of income",
                f"{_inr(total_emi)}/month is {burden * 100:.0f}% of income. Keep it from rising further.")
        costly = loans[0]  # sorted by rate, highest first
        surplus = income - spend
        if costly["rate"] >= HIGH_INTEREST_RATE_PCT and (bm is None or bm >= 1):
            extra = max(min(surplus * 0.5, costly["balance"]), 0)
            tail = (f" Putting about {_inr(extra)}/month of your surplus toward it would cut the interest you pay."
                    if extra > 0 else " Check for a cheaper refinance or a small lump-sum prepayment.")
            add(f"prepay-{costly['name']}", "high" if costly["rate"] >= 18 else "medium",
                f"Clear '{costly['name']}' first",
                f"It charges {costly['rate']:.1f}% on {_inr(costly['balance'])}, your most expensive debt. "
                "Clearing the highest-rate loan first (the 'avalanche' order) saves the most interest, and "
                f"it usually beats earning less than that rate elsewhere.{tail} Check for prepayment charges first.",
                extra or None)

    for goal in p["goals"]:
        if not goal["on_track"]:
            gap = goal["required_monthly"] - (income - spend)
            add(f"goal-off-track-{goal['name']}", "medium", f"Goal '{goal['name']}' is off track",
                f"It needs {_inr(goal['required_monthly'])}/month to hit {_inr(goal['target_amount'])} by "
                f"{goal['deadline']}; you're short by about {_inr(max(gap, 0))}/month. Options: push the deadline, "
                "lower the target, or free up the gap from the categories above.", max(gap, 0))

    costly_debt = any(loan["rate"] >= HIGH_INTEREST_RATE_PCT for loan in loans)
    if rate is not None and rate >= HEALTHY_SAVINGS_RATE and (bm is None or bm >= BUFFER_TARGET_MONTHS) and not costly_debt:
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


# --- Question-aware helpers: numbers the chat model should be handed, not asked to compute -------

_AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|lakh|lakhs|lac|l|cr|crore)?\b", re.I)
_MULT = {"k": 1e3, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "l": 1e5, "cr": 1e7, "crore": 1e7}
_AFFORD_RE = re.compile(r"\b(afford|buy|purchase|spend|splurge|get a|treat)\b", re.I)


def parse_amount(message):
    """First rupee amount mentioned ('₹20,000', '20k', '1.5 lakh'), or None."""
    for m in _AMOUNT_RE.finditer(message):
        value = float(m.group(1).replace(",", ""))
        unit = (m.group(2) or "").lower()
        has_marker = bool(re.search(r"₹|rs\.?|inr", m.group(0), re.I))
        if unit or has_marker or value >= 100:
            return value * _MULT.get(unit, 1)
    return None


def affordability(profile, amount):
    """Plain-numbers verdict on a one-off purchase, from the same profile the rules use."""
    spend, income, balance = profile["avg_monthly_spend"], profile["avg_monthly_income"], profile["balance"]
    after = balance - amount
    buffer_after = after / spend if spend > 0 else None
    monthly_surplus = income - spend
    if buffer_after is None:
        verdict = "not enough spending history to judge"
    elif after < 0:
        verdict = "not affordable from your current balance"
    elif buffer_after < 1:
        verdict = "risky: it would leave under one month of expenses"
    elif buffer_after < BUFFER_TARGET_MONTHS:
        verdict = f"possible, but it would drop your buffer below the {BUFFER_TARGET_MONTHS}-month target"
    else:
        verdict = f"comfortably affordable; your buffer stays above {BUFFER_TARGET_MONTHS} months"
    months_to_recover = (amount / monthly_surplus) if monthly_surplus > 0 else None
    return {
        "purchase": round(amount, 2), "balance_after": round(after, 2),
        "buffer_months_after": round(buffer_after, 1) if buffer_after is not None else None,
        "months_of_surplus_to_recover": round(months_to_recover, 1) if months_to_recover else None,
        "verdict": verdict,
    }


def computed_facts_for(message, profile):
    """Deterministic answers for questions the model would otherwise do arithmetic on."""
    facts = {}
    amount = parse_amount(message)
    if amount and _AFFORD_RE.search(message):
        facts["affordability_check"] = affordability(profile, amount)
    return facts


def affordability_reply(facts, recommendations):
    """Plain-language answer to "can I afford X?" built from the computed numbers alone. A small local
    model was seen contradicting its own pre-computed verdict, so this question never goes to the model."""
    a = facts["affordability_check"]
    lines = [f"For a {_inr(a['purchase'])} purchase: {a['verdict']}."]
    lines.append(f"Your balance would go from {_inr(a['balance_after'] + a['purchase'])} to {_inr(a['balance_after'])}"
                 + (f", which covers about {a['buffer_months_after']} months of your usual spending." if a["buffer_months_after"] is not None else "."))
    if a["months_of_surplus_to_recover"]:
        lines.append(f"At your current monthly surplus, you'd earn it back in about {a['months_of_surplus_to_recover']} months.")
    top = next((r for r in recommendations if r["id"] not in ("on-track", "all-clear", "need-history")), None)
    if top:
        lines.append(f"Worth keeping in mind: {top['title'].lower()}.")
    return " ".join(lines)


def _canon(number_text):
    return round(float(number_text.replace(",", "")), 2)


def unverified_figures(reply, context_text, user_message):
    """₹ amounts in `reply` that appear nowhere in the data the model was given or in the user's own message
    (compared numerically, so 9,000 matches 9000.0)."""
    known = {_canon(n) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", context_text + " " + user_message)}
    return [n for n in re.findall(r"₹\s?(\d[\d,]*(?:\.\d+)?)", reply) if _canon(n) not in known]


def grounded_fallback(recommendations):
    """Deterministic reply used when the model's answer contained figures that can't be verified."""
    top = [r for r in recommendations if r["id"] not in ("on-track", "all-clear", "need-history")][:3] or recommendations[:1]
    body = "\n".join(f"{i}. {r['title']}: {r['detail']}" for i, r in enumerate(top, 1))
    return ("I couldn't verify some numbers in my own answer, so here are the checked suggestions "
            f"from your data instead:\n{body}")


def fix_currency(reply):
    """All amounts are INR; a model that writes $12,000 means ₹12,000."""
    return re.sub(r"\$\s?(?=\d)", "₹", reply)
