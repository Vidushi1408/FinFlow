"""Generate a realistic, synthetic 12-month bank statement for trying FinFlow's CSV import.

    python scripts/generate_sample_statement.py                       # -> sample_data/sample_statement_1yr.csv
    python scripts/generate_sample_statement.py --end 2026-10-26 --seed 11 --out my.csv

The layout copies the column names and formats of a typical Indian bank export (day-first dates, separate
Withdrawal/Deposit columns, a running Closing Balance, UPI narrations). It describes one fictional salaried
person: monthly salary, rent, EMI, SIP, utilities, subscriptions, food delivery, groceries, travel,
shopping, friends' UPI payments, ATM cash, a few one-off large purchases, and a handful of out-of-pattern
charges so the Alerts page has something to find. Everything is invented; no real person's data.
"""
import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

OPENING_BALANCE = 85000.00

SUBSCRIPTIONS = [  # (narration merchant, amount, day of month)
    ("NETFLIX.COM", 649.00, 4), ("SPOTIFY INDIA", 119.00, 9), ("AMAZON PRIME", 299.00, 14),
    ("JIO FIBER BROADBAND", 999.00, 6), ("AIRTEL POSTPAID", 599.00, 11),
]
FRIENDS = ["RIYA SHARMA", "ARJUN MEHTA", "KARAN VERMA", "NEHA GUPTA", "SAMEER KHAN"]
FOOD = ["SWIGGY", "ZOMATO", "DOMINOS PIZZA", "STARBUCKS COFFEE", "BARBEQUE NATION"]
GROCERY = ["BIGBASKET", "BLINKIT", "ZEPTO", "DMART", "RELIANCE FRESH"]
SHOPPING = ["AMAZON PAY", "FLIPKART", "MYNTRA", "NYKAA", "DECATHLON"]
TRAVEL = ["UBER INDIA", "OLA CABS", "REDBUS", "IRCTC", "MAKEMYTRIP"]
MERCHANT_RANGE = {  # merchant -> (low, high) typical spend
    "SWIGGY": (180, 650), "ZOMATO": (200, 700), "DOMINOS PIZZA": (350, 900), "STARBUCKS COFFEE": (250, 600),
    "BARBEQUE NATION": (1200, 2800), "BIGBASKET": (600, 2400), "BLINKIT": (150, 700), "ZEPTO": (150, 650),
    "DMART": (900, 3200), "RELIANCE FRESH": (400, 1800), "AMAZON PAY": (300, 3500), "FLIPKART": (400, 4200),
    "MYNTRA": (700, 4500), "NYKAA": (500, 2200), "DECATHLON": (800, 5000), "UBER INDIA": (120, 520),
    "OLA CABS": (110, 480), "REDBUS": (450, 1600), "IRCTC": (350, 2400), "MAKEMYTRIP": (2500, 9000),
}


def _month_starts(start, end):
    d = date(start.year, start.month, 1)
    while d <= end:
        yield d
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _clamp_day(year, month, day):
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return min(day, (nxt - timedelta(days=1)).day)


def generate(end, seed):
    rng = random.Random(seed)
    start = end - timedelta(days=364)
    rows = []  # (date, narration, ref, withdrawal, deposit)
    ref_counter = [524000000000 + rng.randrange(10**8)]

    def ref():
        ref_counter[0] += rng.randrange(1, 900)
        return str(ref_counter[0])

    def upi(merchant, amount, d, kind="P2M"):
        slug = merchant.lower().replace(" ", "")
        rows.append((d, f"UPI-{merchant}-{slug}@okhdfcbank-HDFC0000001-{ref()}-{kind}", ref(), round(amount, 2), 0.0))

    for m in _month_starts(start, end):
        def on(day):
            return date(m.year, m.month, _clamp_day(m.year, m.month, day))

        # income
        raise_factor = 1.08 if m >= date(start.year + (start.month > 4), 4, 1) else 1.0   # April appraisal
        salary = round(78000 * raise_factor, 2)
        rows.append((on(1), "NEFT CR-ACME TECHNOLOGIES PVT LTD-SALARY", ref(), 0.0, salary))
        if m.month in (3, 9):
            rows.append((on(20), "NEFT CR-ACME TECHNOLOGIES PVT LTD-PERFORMANCE BONUS", ref(), 0.0, round(rng.uniform(18000, 32000), 2)))
        if rng.random() < 0.3:
            rows.append((on(rng.randint(8, 25)), "NEFT CR-FREELANCE CLIENT-INVOICE", ref(), 0.0, round(rng.uniform(4000, 15000), 2)))
        if m.month in (3, 12):
            rows.append((on(28), "INT.PD:SAVINGS A/C INTEREST", ref(), 0.0, round(rng.uniform(310, 540), 2)))

        # fixed outgoings
        rows.append((on(2), "NEFT DR-LANDLORD R K PROPERTIES-RENT", ref(), 24000.0, 0.0))
        rows.append((on(5), "ACH D- HDFC BANK LTD-LOAN EMI 7788991", ref(), 8450.0, 0.0))
        rows.append((on(7), "ACH D- NIPPON INDIA MUTUAL FUND-SIP", ref(), 5000.0, 0.0))
        for name, amt, day in SUBSCRIPTIONS:
            rows.append((on(day), f"UPI-{name}-autopay@okhdfcbank-HDFC0000001-{ref()}-Subscription", ref(), amt, 0.0))
        rows.append((on(12), "BBPS-BSES RAJDHANI POWER LTD-ELECTRICITY", ref(), round(rng.uniform(850, 2300) * (1.5 if m.month in (5, 6, 7) else 1.0), 2), 0.0))
        rows.append((on(15), "BBPS-INDRAPRASTHA GAS-PNG BILL", ref(), round(rng.uniform(300, 650), 2), 0.0))

        # variable spending
        days_in_month = _clamp_day(m.year, m.month, 31)
        for _ in range(rng.randint(11, 17)):
            merchant = rng.choice(FOOD)
            lo, hi = MERCHANT_RANGE[merchant]
            upi(merchant, rng.uniform(lo, hi), on(rng.randint(1, days_in_month)))
        for _ in range(rng.randint(4, 7)):
            merchant = rng.choice(GROCERY)
            lo, hi = MERCHANT_RANGE[merchant]
            upi(merchant, rng.uniform(lo, hi), on(rng.randint(1, days_in_month)))
        for _ in range(rng.randint(5, 10)):
            merchant = rng.choice(TRAVEL[:2])  # day-to-day cabs
            lo, hi = MERCHANT_RANGE[merchant]
            upi(merchant, rng.uniform(lo, hi), on(rng.randint(1, days_in_month)))
        for _ in range(rng.randint(1, 3)):
            merchant = rng.choice(SHOPPING)
            lo, hi = MERCHANT_RANGE[merchant]
            upi(merchant, rng.uniform(lo, hi), on(rng.randint(1, days_in_month)))
        for _ in range(rng.randint(2, 5)):
            friend = rng.choice(FRIENDS)
            d = on(rng.randint(1, days_in_month))
            if rng.random() < 0.7:
                upi(friend, rng.choice([200, 250, 300, 500, 750, 1000, 1500]), d, kind="P2P")
            else:  # a friend pays you back
                rows.append((d, f"UPI-{friend}-{friend.lower().replace(' ', '')}@okicici-ICIC0000001-{ref()}-P2P", ref(), 0.0, float(rng.choice([300, 500, 800, 1200]))))
        if rng.random() < 0.8:
            rows.append((on(rng.randint(3, 26)), f"ATW-512345XXXXXX1234-HDFC BANK ATM-{rng.randint(1000, 9999)}", ref(), float(rng.choice([2000, 3000, 5000])), 0.0))
        if m.month in (4, 10):  # a trip each half-year
            upi("MAKEMYTRIP", rng.uniform(7000, 14000), on(rng.randint(8, 20)))
            upi("IRCTC", rng.uniform(1500, 3200), on(rng.randint(8, 20)))
    # one-off big-ticket purchases and a few out-of-pattern charges (for the Alerts page to find)
    def at(offset_days):
        return min(end, start + timedelta(days=offset_days))
    upi("CROMA ELECTRONICS", 54990.00, at(110))                                   # laptop
    upi("APPLE INDIA STORE", 79900.00, at(235))                                   # phone
    rows.append((at(150), "POS 4812XXXXXX7731 UNKNOWN INTL MERCHANT LTD", ref(), 38450.00, 0.0))   # odd, big, new
    rows.append((at(205), f"UPI-QUICKCASH FINSERV-quickcash@ybl-YESB0000001-{ref()}-P2P", ref(), 25000.00, 0.0))  # odd
    rows.append((at(300), "POS 4812XXXXXX7731 GLOBAL GIFTCARDS HK", ref(), 19999.00, 0.0))        # odd

    rows = [r for r in rows if start <= r[0] <= end]
    rows.sort(key=lambda r: (r[0], r[1]))

    balance = OPENING_BALANCE
    out = []
    for d, narration, reference, withdrawal, deposit in rows:
        balance = round(balance - withdrawal + deposit, 2)
        out.append((d, narration, reference, withdrawal, deposit, balance))
    return out


def write_csv(rows, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def inr(x):  # Indian digit grouping, as banks print it
        s = f"{x:,.2f}"
        whole, frac = s.split(".")
        digits = whole.replace(",", "")
        if len(digits) > 3:
            head, tail = digits[:-3], digits[-3:]
            groups = []
            while len(head) > 2:
                groups.insert(0, head[-2:])
                head = head[:-2]
            if head:
                groups.insert(0, head)
            whole = ",".join(groups + [tail])
        return f"{whole}.{frac}"

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"])
        for d, narration, reference, withdrawal, deposit, balance in rows:
            ds = d.strftime("%d/%m/%y")
            w.writerow([ds, narration, reference, ds, inr(withdrawal) if withdrawal else "", inr(deposit) if deposit else "", inr(balance)])
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--end", default=None, help="last statement date, YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "sample_data" / "sample_statement_1yr.csv"))
    args = parser.parse_args()
    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    rows = generate(end, args.seed)
    path = write_csv(rows, args.out)
    debits = sum(r[3] for r in rows)
    credits = sum(r[4] for r in rows)
    print(f"Wrote {len(rows)} transactions to {path}")
    print(f"Period {rows[0][0]} to {rows[-1][0]}; opening ₹{OPENING_BALANCE:,.2f}, closing ₹{rows[-1][5]:,.2f}; "
          f"debits ₹{debits:,.2f}, credits ₹{credits:,.2f}")


if __name__ == "__main__":
    main()
