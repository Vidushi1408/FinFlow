import csv
import random
from datetime import datetime, timedelta
import uuid
import argparse
from faker import Faker
from werkzeug.security import generate_password_hash

fake = Faker()

DEMO_PASSWORD = "demo1234"  # nosec B105 - not a real secret: every generated demo/synthetic user shares this documented password

MERCHANTS = [
    {"name": "HDFC Bank", "category": "FINANCE", "mcc": "6011"},
    {"name": "Swiggy", "category": "FOOD", "mcc": "5812"},
    {"name": "Zomato", "category": "FOOD", "mcc": "5812"},
    {"name": "Amazon", "category": "SHOPPING", "mcc": "5310"},
    {"name": "Flipkart", "category": "SHOPPING", "mcc": "5310"},
    {"name": "Myntra", "category": "SHOPPING", "mcc": "5310"},
    {"name": "Uber", "category": "TRANSPORT", "mcc": "4121"},
    {"name": "Ola", "category": "TRANSPORT", "mcc": "4121"},
    {"name": "Netflix", "category": "ENTERTAINMENT", "mcc": "4814", "is_recurring": True},
    {"name": "Spotify", "category": "ENTERTAINMENT", "mcc": "4814", "is_recurring": True},
    {"name": "Hotstar", "category": "ENTERTAINMENT", "mcc": "4814", "is_recurring": True},
    {"name": "Gold's Gym", "category": "HEALTH", "mcc": "7997", "is_recurring": True},
    {"name": "Apollo Pharmacy", "category": "HEALTH", "mcc": "5912"},
    {"name": "Indian Oil", "category": "FUEL", "mcc": "5541"},
    {"name": "HP Petrol", "category": "FUEL", "mcc": "5541"},
    {"name": "Reliance Fresh", "category": "GROCERY", "mcc": "5411"},
    {"name": "BigBasket", "category": "GROCERY", "mcc": "5411"},
    {"name": "Blinkit", "category": "GROCERY", "mcc": "5411"},
    {"name": "Zepto", "category": "GROCERY", "mcc": "5411"},
    {"name": "Airtel Recharge", "category": "RECHARGE", "mcc": "4814", "is_recurring": True},
    {"name": "Jio Recharge", "category": "RECHARGE", "mcc": "4814", "is_recurring": True},
    {"name": "UPI Payment", "category": "UPI", "mcc": "6540"},
]

EMPLOYERS = [
    {"name": "TCS", "category": "INCOME", "mcc": "0000"},
    {"name": "Infosys", "category": "INCOME", "mcc": "0000"}
]

RENT_MERCHANT = {"name": "House Rent", "category": "RENT", "mcc": "6513"}
STIPEND_SOURCE = {"name": "Institute Stipend", "category": "INCOME", "mcc": "0000"}
CLIENT_NAMES = ["Acme Studio", "Pixel Works", "Bright Consulting", "Nova Labs", "Vertex Media"]
CLIENTS = [{"name": name, "category": "INCOME", "mcc": "0000"} for name in CLIENT_NAMES]

# Persona definitions drive income pattern, spend range, and category mix so the
# generated data reads like real young-earner behaviour rather than uniform noise.
PERSONAS = {
    "student": {
        "account_type": "SAVINGS",
        "income_label": "Stipend",
        "income_range": (8000.0, 18000.0),
        "income_days": [1],
        "income_irregular": False,
        "spend_range": (10.0, 1500.0),
        "target_spend_ratio": (0.80, 1.05),  # stipend gets spent close to fully, sometimes overspent
        "weekend_multiplier": 1.6,
        "night_owl": True,  # students skew toward late-evening spending
        "category_weights": {
            "FOOD": 4, "ENTERTAINMENT": 2, "TRANSPORT": 2, "SHOPPING": 1,
            "GROCERY": 1, "RECHARGE": 2, "UPI": 3, "HEALTH": 1
        },
        "starting_balance": (3000.0, 15000.0),
    },
    "salaried": {
        "account_type": "CHECKING",
        "income_label": "Salary",
        "income_range": (45000.0, 150000.0),
        "income_days": [1, 28, 30],  # payday varies by employer; exactly one is picked per account
        "income_irregular": False,
        "spend_range": (50.0, 8000.0),
        "target_spend_ratio": (0.45, 0.70),  # saves a meaningful chunk of salary
        "weekend_multiplier": 1.3,
        "night_owl": False,
        "category_weights": {
            "GROCERY": 2, "FOOD": 2, "TRANSPORT": 1, "SHOPPING": 2,
            "RENT": 1, "FUEL": 1, "UPI": 2, "HEALTH": 1, "RECHARGE": 1
        },
        "starting_balance": (30000.0, 150000.0),
    },
    "freelancer": {
        "account_type": "CHECKING",
        "income_label": "Freelance Payment",
        "income_range": (5000.0, 60000.0),
        "income_days": [],
        "income_irregular": True,  # payouts land on no fixed schedule
        "spend_range": (30.0, 6000.0),
        "target_spend_ratio": (0.60, 0.85),  # moderate savings, but income is inconsistent
        "weekend_multiplier": 1.2,
        "night_owl": True,
        "category_weights": {
            "FOOD": 2, "SHOPPING": 2, "TRANSPORT": 2, "GROCERY": 1,
            "UPI": 3, "FUEL": 1, "RECHARGE": 1
        },
        "starting_balance": (5000.0, 50000.0),
    },
}


def generate_users(num_users, persona_cycle=("student", "salaried", "freelancer")):
    """Create dim_user rows, cycling through personas so every persona type is represented."""
    users = []
    for i in range(num_users):
        persona = persona_cycle[i % len(persona_cycle)]
        name = fake.name()
        users.append({
            "user_id": str(uuid.uuid4()),
            "name": name,
            "email": f"{name.lower().replace(' ', '.')}.{i}@example.com",
            "password_hash": generate_password_hash(DEMO_PASSWORD),
            "currency": "INR",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "persona": persona,  # kept in-memory only; not a dim_user column
        })
    return users


def generate_accounts(users, start_date):
    """One primary account per user, typed and seeded to match their persona."""
    accounts = []
    for user in users:
        persona = PERSONAS[user["persona"]]
        low, high = persona["starting_balance"]
        accounts.append({
            "account_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "account_type": persona["account_type"],
            "institution": random.choice(["HDFC", "SBI", "ICICI", "Axis"]),
            "account_number": fake.bban(),
            "credit_limit": random.choice([50000, 100000, 200000, ""]),  # empty string instead of None
            "opened_date": (start_date - timedelta(days=random.randint(100, 1000))).date(),
            "starting_balance": round(random.uniform(low, high), 2),
            "persona": user["persona"],  # in-memory only
        })
    return accounts


def _weighted_merchant(category_weights):
    categories = list(category_weights.keys())
    weights = list(category_weights.values())
    chosen_category = random.choices(categories, weights=weights, k=1)[0]
    # Exclude merchants already modeled as fixed-amount monthly subscriptions
    # (see recurring_subs below) -- otherwise the same merchant gets both a
    # real ~500/month subscription charge AND random one-off amounts mixed
    # in from here, which wrecks the amount-stability signal recurring
    # detection relies on (a "subscription" whose amount swings from 190 to
    # 1300 doesn't look recurring to anyone, human or algorithm).
    candidates = [m for m in MERCHANTS if m["category"] == chosen_category and not m.get("is_recurring")]
    if not candidates:
        candidates = [m for m in MERCHANTS if not m.get("is_recurring")]
    return random.choice(candidates)


def _spend_datetime(day, persona):
    if persona["night_owl"]:
        hour = random.choices(
            population=[random.randint(11, 15), random.randint(18, 23), random.randint(0, 2)],
            weights=[0.3, 0.5, 0.2], k=1
        )[0]
    else:
        hour = random.choices(
            population=[random.randint(8, 11), random.randint(12, 17), random.randint(18, 21)],
            weights=[0.25, 0.35, 0.4], k=1
        )[0]
    return day.replace(hour=hour % 24, minute=random.randint(0, 59), second=random.randint(0, 59))


def generate_transactions(accounts, num_transactions, start_date, end_date):
    transactions = []
    total_days = (end_date - start_date).days
    months_span = max(total_days / 30.0, 1.0)

    recurring_subs = []
    recurring_incomes = []
    recurring_rent = []

    for acc in accounts:
        persona = PERSONAS[acc["persona"]]

        # Subscriptions (persona-agnostic set, weighted toward entertainment/recharge)
        sub_candidates = [m for m in MERCHANTS if m.get("is_recurring")]
        for merchant in random.sample(sub_candidates, k=random.randint(1, 3)):
            recurring_subs.append({
                "account_id": acc["account_id"],
                "merchant": merchant,
                "amount": random.uniform(149.0, 1999.0),
                "day_of_month": random.randint(1, 28)
            })

        # Income: consistent payday for student/salaried, irregular payouts for freelancer
        if persona["income_irregular"]:
            num_payouts = random.randint(6, 14)
            for _ in range(num_payouts):
                recurring_incomes.append({
                    "account_id": acc["account_id"],
                    "label": random.choice(CLIENT_NAMES),
                    "amount": random.uniform(*persona["income_range"]),
                    "offset_day": random.randint(0, total_days),
                    "irregular": True
                })
        else:
            # income_days lists alternative typical paydays (e.g. an employer might
            # pay on the 1st, 28th, or 30th) -- pick ONE, not one recurring income
            # per listed day, or the account would be paid multiple salaries a month.
            recurring_incomes.append({
                "account_id": acc["account_id"],
                "label": random.choice(EMPLOYERS)["name"] if acc["persona"] == "salaried" else "Institute Stipend",
                "amount": random.uniform(*persona["income_range"]),
                "day_of_month": random.choice(persona["income_days"]),
                "irregular": False
            })

        # Rent, only for the two personas realistically paying it monthly
        if acc["persona"] in ("salaried", "freelancer") and random.random() < 0.7:
            recurring_rent.append({
                "account_id": acc["account_id"],
                "amount": random.uniform(8000.0, 25000.0),
                "day_of_month": random.randint(1, 5)
            })

    # Size day-to-day discretionary spending off each account's actual income
    # rather than a flat amount range, so totals stay realistic regardless of
    # how many transactions get generated: monthly_income * target_spend_ratio,
    # minus what's already committed to rent/subscriptions.
    monthly_income_by_account = {}
    for inc in recurring_incomes:
        contribution = (inc["amount"] / months_span) if inc["irregular"] else inc["amount"]
        monthly_income_by_account[inc["account_id"]] = monthly_income_by_account.get(inc["account_id"], 0.0) + contribution

    committed_monthly_by_account = {}
    for sub in recurring_subs:
        committed_monthly_by_account[sub["account_id"]] = committed_monthly_by_account.get(sub["account_id"], 0.0) + sub["amount"]
    for rent in recurring_rent:
        committed_monthly_by_account[rent["account_id"]] = committed_monthly_by_account.get(rent["account_id"], 0.0) + rent["amount"]

    discretionary_monthly_by_account = {}
    for acc in accounts:
        persona = PERSONAS[acc["persona"]]
        monthly_income = monthly_income_by_account.get(acc["account_id"], sum(persona["income_range"]) / 2)
        ratio = random.uniform(*persona["target_spend_ratio"])
        committed = committed_monthly_by_account.get(acc["account_id"], 0.0)
        # Floor at 5% of income so there's always some discretionary spend even
        # if rent/subscriptions alone already consume the whole target ratio.
        discretionary_monthly_by_account[acc["account_id"]] = max(
            monthly_income * ratio - committed, monthly_income * 0.05
        )

    expected_tx_per_month_per_account = max((num_transactions / len(accounts)) / months_span, 1.0)

    # Regular day-to-day spending, persona-weighted by category, time-of-day, and weekday/weekend
    for _ in range(num_transactions):
        account = random.choice(accounts)
        persona = PERSONAS[account["persona"]]

        day = start_date + timedelta(days=random.randint(0, total_days))
        date = _spend_datetime(day, persona)
        is_weekend = date.weekday() >= 5

        merchant = _weighted_merchant(persona["category_weights"])

        # Expected amount so that (tx/month * mean amount) lands on the account's
        # discretionary budget; uniform(0.3, 1.7) has mean 1.0 so it doesn't bias
        # the total while still giving day-to-day variance (coffee vs. a big buy).
        per_tx_mean = discretionary_monthly_by_account[account["account_id"]] / expected_tx_per_month_per_account
        low_cap, high_cap = persona["spend_range"]
        amount = per_tx_mean * random.uniform(0.3, 1.7)
        amount = min(max(amount, low_cap), high_cap * 2)  # persona floor, with room for occasional bigger buys
        if is_weekend:
            amount *= persona["weekend_multiplier"]

        # Occasional refund/transfer credit unrelated to salary/stipend
        if random.random() < 0.04:
            merchant = {"name": "Transfer", "category": "TRANSFER", "mcc": "0000"}
            amount = random.uniform(500.0, 5000.0)
            tx_type = "CREDIT"
        else:
            tx_type = "DEBIT"

        transactions.append({
            "transaction_id": str(uuid.uuid4()),
            "account_id": account["account_id"],
            "merchant_id": merchant["name"].replace(" ", "_").upper(),
            "category_id": merchant["category"],
            "date": date.strftime("%Y-%m-%d %H:%M:%S"),
            "amount": round(amount, 2),
            "transaction_type": tx_type,
            "is_fraud": False,
            "is_recurring": False
        })

    # Recurring subscriptions (DEBIT)
    for sub in recurring_subs:
        current_date = start_date
        while current_date <= end_date:
            if current_date.day == sub["day_of_month"]:
                transactions.append({
                    "transaction_id": str(uuid.uuid4()),
                    "account_id": sub["account_id"],
                    "merchant_id": sub["merchant"]["name"].replace(" ", "_").upper(),
                    "category_id": sub["merchant"]["category"],
                    "date": current_date.strftime("%Y-%m-%d 10:00:00"),
                    "amount": round(sub["amount"], 2),
                    "transaction_type": "DEBIT",
                    "is_fraud": False,
                    "is_recurring": True
                })
            current_date += timedelta(days=1)

    # Recurring rent (DEBIT)
    for rent in recurring_rent:
        current_date = start_date
        while current_date <= end_date:
            if current_date.day == rent["day_of_month"]:
                transactions.append({
                    "transaction_id": str(uuid.uuid4()),
                    "account_id": rent["account_id"],
                    "merchant_id": RENT_MERCHANT["name"].replace(" ", "_").upper(),
                    "category_id": RENT_MERCHANT["category"],
                    "date": current_date.strftime("%Y-%m-%d 09:00:00"),
                    "amount": round(rent["amount"], 2),
                    "transaction_type": "DEBIT",
                    "is_fraud": False,
                    "is_recurring": True
                })
            current_date += timedelta(days=1)

    # Income: regular payday or irregular freelance payouts (CREDIT)
    for inc in recurring_incomes:
        if inc["irregular"]:
            pay_date = start_date + timedelta(days=inc["offset_day"])
            if pay_date <= end_date:
                transactions.append({
                    "transaction_id": str(uuid.uuid4()),
                    "account_id": inc["account_id"],
                    "merchant_id": inc["label"].replace(" ", "_").upper(),
                    "category_id": "INCOME",
                    "date": pay_date.strftime("%Y-%m-%d 11:00:00"),
                    "amount": round(inc["amount"], 2),
                    "transaction_type": "CREDIT",
                    "is_fraud": False,
                    "is_recurring": False
                })
        else:
            current_date = start_date
            while current_date <= end_date:
                if current_date.day == inc["day_of_month"]:
                    transactions.append({
                        "transaction_id": str(uuid.uuid4()),
                        "account_id": inc["account_id"],
                        "merchant_id": inc["label"].replace(" ", "_").upper(),
                        "category_id": "INCOME",
                        "date": current_date.strftime("%Y-%m-%d 08:00:00"),
                        "amount": round(inc["amount"], 2),
                        "transaction_type": "CREDIT",
                        "is_fraud": False,
                        "is_recurring": True
                    })
                current_date += timedelta(days=1)

    # Fraud: a small, FIXED number of anomalous events per account, not a rate
    # applied to every transaction. Real fraud is rare in absolute terms --
    # scaling it with transaction volume (as a percentage) made "fraud" the
    # single largest driver of total spend once density got realistic, since
    # each event is 20k-100k. A few genuine anomalies among lots of normal
    # activity is also what makes the class-imbalance fraud detection task real.
    for acc in accounts:
        persona = PERSONAS[acc["persona"]]
        for _ in range(random.randint(2, 5)):
            fraud_merchant = _weighted_merchant(persona["category_weights"])
            date = start_date + timedelta(days=random.randint(0, total_days), hours=random.randint(1, 4))
            if random.random() < 0.4:
                # Unusually large purchase, scaled to what's plausible for this
                # persona's card rather than a flat range -- a student's account
                # shouldn't see the same fraud ticket size as a salaried one.
                amount = persona["spend_range"][1] * random.uniform(3, 12)
            else:
                amount = random.uniform(1.0, 5.0)  # card-testing micro-charge
            transactions.append({
                "transaction_id": str(uuid.uuid4()),
                "account_id": acc["account_id"],
                "merchant_id": fraud_merchant["name"].replace(" ", "_").upper(),
                "category_id": fraud_merchant["category"],
                "date": date.strftime("%Y-%m-%d %H:%M:%S"),
                "amount": round(amount, 2),
                "transaction_type": "DEBIT",
                "is_fraud": True,
                "is_recurring": False
            })

    transactions.sort(key=lambda x: x["date"])
    return transactions


def save_csv(data, filename, fieldnames):
    with open(filename, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        # Drop any in-memory-only keys (e.g. "persona") that aren't part of the CSV schema
        rows = [{k: row.get(k) for k in fieldnames} for row in data]
        writer.writerows(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate synthetic financial data.")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    parser.add_argument("--users", type=int, default=9, help="Number of demo users (one account each)")
    parser.add_argument(
        "--transactions", type=int, default=None,
        help="Target number of base (non-recurring) transactions. Defaults to "
             "~2.5/day per account for the given history, which keeps spend "
             "realistic relative to income -- an arbitrarily large count will "
             "still overspend since per-transaction amounts have a sane floor."
    )
    parser.add_argument("--history_months", type=int, default=12, help="Months of history to generate")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
        Faker.seed(args.seed)

    if args.transactions is None:
        TX_PER_ACCOUNT_PER_MONTH = 75  # ~2.5/day, a believable mix of UPI/food/etc.
        args.transactions = int(args.users * args.history_months * TX_PER_ACCOUNT_PER_MONTH)

    END_DATE = datetime.now()
    START_DATE = END_DATE - timedelta(days=30 * args.history_months)

    print(f"Generating data from {START_DATE.date()} to {END_DATE.date()}...")

    print("Generating users...")
    users = generate_users(args.users)
    save_csv(users, "users.csv", ["user_id", "name", "email", "password_hash", "currency", "created_at"])

    print("Generating accounts...")
    accounts = generate_accounts(users, START_DATE)
    save_csv(accounts, "accounts.csv", [
        "account_id", "user_id", "account_type", "institution", "account_number",
        "credit_limit", "opened_date", "starting_balance"
    ])

    print("Generating merchants...")
    merchant_data = []
    all_merchants = MERCHANTS + EMPLOYERS + CLIENTS + [
        RENT_MERCHANT, STIPEND_SOURCE, {"name": "Transfer", "category": "TRANSFER", "mcc": "0000"}
    ]
    seen_ids = set()
    for m in all_merchants:
        merchant_id = m["name"].replace(" ", "_").upper()
        if merchant_id in seen_ids:
            continue
        seen_ids.add(merchant_id)
        merchant_data.append({
            "merchant_id": merchant_id,
            "merchant_name": m["name"],
            "category": m["category"],
            "mcc_code": m["mcc"]
        })
    save_csv(merchant_data, "merchants.csv", ["merchant_id", "merchant_name", "category", "mcc_code"])

    print("Generating transactions...")
    transactions = generate_transactions(accounts, args.transactions, START_DATE, END_DATE)
    save_csv(transactions, "transactions.csv", ["transaction_id", "account_id", "merchant_id", "category_id", "date", "amount", "transaction_type", "is_fraud", "is_recurring"])

    print(f"Generated {len(users)} users, {len(accounts)} accounts, and {len(transactions)} transactions.")
    print(f"Demo login password for every generated user: {DEMO_PASSWORD}")
