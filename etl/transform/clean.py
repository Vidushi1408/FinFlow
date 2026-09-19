import difflib
import hashlib
import re
import pandas as pd
import numpy as np

from logging_config import logger

def compute_idempotency_key(df):
    """
    Deterministic transaction_id: sha256 of (account, timestamp, amount, merchant,
    description). Re-ingesting the same source rows (e.g. re-uploading a CSV)
    always yields the same id, so loading with ON CONFLICT on transaction_id
    dedups instead of inserting duplicates.

    Two genuinely separate purchases can look identical (two 50 rupee chai
    payments on the same day, same narration). The Nth repeat of an otherwise
    identical row gets its own id (an occurrence suffix), so those aren't
    collapsed into one -- while the same file re-imported still maps each row
    to the same id as before. The first occurrence's id has no suffix, so ids
    of already-loaded data don't change.
    """
    description = df['description'] if 'description' in df.columns else df['merchant_id'].astype(str)
    # Format the timestamp explicitly: astype(str) on a datetime column drops
    # the time part only when *every* row is at midnight, which would make one
    # row's id depend on what other rows happen to be in the file.
    dates = df['date'].dt.strftime('%Y-%m-%d %H:%M:%S') if pd.api.types.is_datetime64_any_dtype(df['date']) else df['date'].astype(str)
    key = (
        df['account_id'].astype(str) + '|' +
        dates + '|' +
        df['amount'].round(2).astype(str) + '|' +
        df['merchant_id'].astype(str) + '|' +
        description.astype(str)
    )
    occurrence = key.groupby(key).cumcount()
    key = key.where(occurrence == 0, key + '|#' + occurrence.astype(str))
    return key.apply(lambda s: hashlib.sha256(s.encode('utf-8')).hexdigest())

def clean_transactions(df):
    """
    Remove duplicates, handle missing values, validate amounts/dates.
    """
    initial_len = len(df)
    
    # Remove duplicates based on transaction_id
    if 'transaction_id' in df.columns:
        df = df.drop_duplicates(subset=['transaction_id'])
    else:
        df = df.drop_duplicates()
        
    # Handle missing values
    df = df.dropna(subset=['amount', 'date'])
    
    # Validate amounts (must be positive for this schema as type handles CREDIT/DEBIT)
    df['amount'] = df['amount'].abs()
    
    # Convert date to datetime
    df['date'] = pd.to_datetime(df['date'])
    
    # Filter future dates
    df = df[df['date'] <= pd.Timestamp.now()]
    
    logger.info(f"Cleaned transactions: removed {initial_len - len(df)} rows.")
    return df

# Keyword -> category, grouped for readability. Matching tries the longest
# keyword first so a more specific term (e.g. "APOLLO HOSPITAL") wins over a
# shorter generic one that happens to also appear (e.g. "HOSPITAL" alone would
# still resolve to HEALTH, just via a different keyword).
CATEGORY_KEYWORDS = {
    'FOOD': [
        'BAKERY', 'BAKERS', 'SWEETS', 'TIFFIN', 'JUICE', 'HOTEL', 'CANTEEN', 'BIRYANI', 'DHABA', 'CHAI',
        'ZOMATO', 'SWIGGY', 'MCDONALD', 'KFC', 'DOMINO', 'PIZZA HUT', 'PIZZA',
        'STARBUCKS', 'CAFE COFFEE DAY', 'CCD', 'BARISTA', 'CAFE', 'RESTAURANT',
        'DINING', 'EATERY', 'BURGER KING', 'SUBWAY', 'HALDIRAM', 'FAASOS',
        'BEHROUZ', 'EATSURE', 'FOOD PANDA', 'DUNKIN'
    ],
    'GROCERY': [
        'PROVISION', 'GENERAL STORE', 'FRUITS', 'VEGETABLE', 'DAIRY',
        'BIGBASKET', 'BLINKIT', 'ZEPTO', 'DMART', 'GROFERS', 'RELIANCE FRESH',
        'NATURE BASKET', 'SPENCER', 'MORE SUPERMARKET', 'STAR BAZAAR',
        'GROCERY', 'SUPERMARKET', 'KIRANA', 'JIOMART', 'INSTAMART'
    ],
    'TRANSPORT': [
        'TRAVELS', 'FASTAG',
        'UBER', 'OLA CABS', 'OLA', 'RAPIDO', 'IRCTC', 'REDBUS', 'MAKEMYTRIP',
        'GOIBIBO', 'YATRA', 'METRO RAIL', 'METRO', 'INDIGO', 'SPICEJET',
        'AIR INDIA', 'VISTARA', 'TAXI', 'CAB BOOKING', 'PARKING'
    ],
    'FUEL': [
        'INDIAN OIL', 'INDIANOIL', 'IOCL', 'HP PETROL', 'HPCL', 'BPCL',
        'SHELL', 'ESSAR', 'NAYARA', 'PETROL PUMP', 'PETROL', 'DIESEL',
        'FUEL STATION', 'GAS STATION'
    ],
    'SHOPPING': [
        'RETAIL', 'ELECTRONICS', 'FASHION', 'GARMENTS', 'MOBILES',
        'AMAZON', 'FLIPKART', 'MYNTRA', 'NYKAA', 'AJIO', 'MEESHO', 'TATA CLIQ',
        'SHOPPERS STOP', 'LIFESTYLE STORE', 'PANTALOONS', 'DECATHLON',
        'RELIANCE TRENDS', 'SHOPPING MALL', 'MALL', 'RETAIL STORE'
    ],
    'ENTERTAINMENT': [
        'APPLE MEDIA', 'APPLE SERVICES', 'ITUNES', 'GOOGLE PLAY',
        'NETFLIX', 'SPOTIFY', 'HOTSTAR', 'DISNEY', 'PRIME VIDEO', 'SONYLIV',
        'ZEE5', 'PVR CINEMAS', 'PVR', 'INOX', 'CINEPOLIS', 'BOOKMYSHOW',
        'YOUTUBE PREMIUM', 'GAMING', 'STEAM', 'PLAYSTATION', 'XBOX',
        'MOVIE TICKET', 'CINEMA'
    ],
    'HEALTH': [
        'AROGYA', 'PHARMA', 'MEDICALS', 'MEDICAL',
        'APOLLO PHARMACY', 'APOLLO HOSPITAL', 'APOLLO', 'PHARMEASY', 'NETMEDS',
        '1MG', 'TATA 1MG', 'MEDPLUS', 'FORTIS', 'MAX HEALTHCARE', 'MANIPAL',
        'HOSPITAL', 'CLINIC', 'DIAGNOSTIC', 'PATHOLOGY', 'PHARMACY',
        'MEDICAL STORE', 'MEDICOS', 'HEALTHCARE', 'DENTAL', 'DOCTOR CONSULT'
    ],
    'RECHARGE': [
        'AIRTEL RECHARGE', 'AIRTEL', 'JIO RECHARGE', 'RELIANCE JIO', 'JIO',
        'VODAFONE IDEA', 'VODAFONE', ' VI ', 'BSNL', 'TATA SKY', 'DISH TV',
        'DTH RECHARGE', 'DTH', 'MOBILE RECHARGE', 'PREPAID RECHARGE'
    ],
    'RENT': [
        'HOUSE RENT', 'RENT PAYMENT', 'RENT PAID', 'LANDLORD', 'LEASE RENT', ' RENT '
    ],
    'UTILITIES': [
        'ELECTRICITY BILL', 'ELECTRICITY', 'POWER BILL', 'WATER BILL',
        'PIPED GAS', 'GAS BILL', 'BROADBAND', 'WIFI BILL', 'INTERNET BILL',
        'ACT FIBERNET', 'SOCIETY MAINTENANCE', 'MAINTENANCE CHARGE'
    ],
    'EDUCATION': [
        'SCHOOL FEE', 'COLLEGE FEE', 'TUITION FEE', 'TUITION', 'UDEMY',
        'COURSERA', 'BYJU', 'UNACADEMY', 'UPGRAD', 'EXAM FEE', 'UNIVERSITY'
    ],
    'UPI': [
        'PAYTM', 'PHONEPE', 'GOOGLE PAY', 'GPAY', 'BHIM', 'UPI-', 'UPI/', ' UPI '
    ],
    'FINANCE': [
        'CRED CLUB', 'CRED.CLUB',
        'MUTUAL FUND', 'SIP INSTALLMENT', ' SIP ', 'ZERODHA', 'GROWW', 'UPSTOX',
        'ICICI DIRECT', 'LIC PREMIUM', ' LIC ', 'INSURANCE PREMIUM', 'INSURANCE',
        'LOAN EMI', ' EMI ', 'CREDIT CARD BILL', 'CREDIT CARD PAYMENT', 'NPS CONTRIBUTION'
    ],
    'INCOME': [
        'SALARY CREDIT', 'SALARY', 'STIPEND', 'PAYROLL', 'WAGES', 'FREELANCE PAYMENT',
        'CONSULTING FEE', 'INTEREST CREDIT', 'INTEREST PAID', 'DIVIDEND', 'REFUND',
        'CASHBACK', 'BONUS PAYMENT', 'REIMBURSEMENT'
    ],
    'TRANSFER': [
        'NEFT', 'IMPS', 'RTGS', 'FUND TRANSFER', 'TRANSFER TO', 'TRANSFER FROM',
        'ATM WDL', 'ATM WITHDRAWAL', 'SELF TRANSFER', 'ACCOUNT TRANSFER'
    ],
}

# Longest keyword first, so a more specific phrase is tried before a shorter
# one that might also (coincidentally) be a substring of the transaction text.
_SORTED_KEYWORDS = sorted(
    ((keyword, category) for category, keywords in CATEGORY_KEYWORDS.items() for keyword in keywords),
    key=lambda pair: -len(pair[0])
)

FALLBACK_CATEGORY = 'OTHER'  # used only when nothing -- exact or fuzzy -- matches
_FUZZY_MATCH_THRESHOLD = 0.75
_FUZZY_MATCH_ROW_CAP = 5000  # safety cap so a huge, mostly-unmatched upload doesn't hang

# Per-word index for fuzzy matching: a whole merchant string ("ZOMTO DELIVERY
# BLR123") is rarely a close match to a keyword as a whole, but one of its
# words often is close to one of a keyword's words. First occurrence wins for
# a word that appears in more than one keyword (rare, and _SORTED_KEYWORDS is
# already longest-first so that's the more specific one).
_WORD_KEYWORDS = {}
for _keyword, _category in _SORTED_KEYWORDS:
    for _word in _keyword.split():
        if len(_word) >= 4:
            _WORD_KEYWORDS.setdefault(_word, _category)
_WORD_LIST = list(_WORD_KEYWORDS.keys())


_UPI_NOISE = re.compile(r'^(\d{6,}|DR|CR|P2A|P2M|P2P|PAY|UPI|COLLECT|REQ)$')


def is_upi_text(text):
    t = str(text).upper().strip()
    return t.startswith(('UPI-', 'UPI/')) or '/UPI/' in t


def extract_upi_payee(text):
    """
    Pull the payee name out of a UPI narration, ignoring the parts that are
    noise for categorization (the payer's VPA/bank, IFSC, reference numbers):
      UPI-CRED CLUB-CRED.CLUB@AXISB-UTIB0000110-6213...  -> CRED CLUB
      UPI/6213.../DR/RAHUL SHARMA/HDFC/...               -> RAHUL SHARMA
    Returns None when it isn't a UPI narration or no payee name is present.
    """
    t = str(text).upper().strip()
    if t.startswith('UPI-'):
        payee = t[4:].split('-')[0].strip()
        return payee if re.search(r'[A-Z]{2}', payee) and '@' not in payee else None
    if t.startswith('UPI/') or '/UPI/' in t:
        for part in t.split('UPI/', 1)[1].split('/'):
            part = part.strip()
            if part and not _UPI_NOISE.match(part) and '@' not in part and re.search(r'[A-Z]{2}', part):
                return part
    return None


def merchant_key(text):
    """
    A stable identity for "the same merchant", so one categorization rule
    covers every payment to them: the payee for UPI narrations, otherwise the
    text with reference-number-like tokens (anything containing a digit) removed.
    """
    t = str(text).upper().strip()
    if is_upi_text(t):
        payee = extract_upi_payee(t)
        if payee:
            return payee
    stripped = ' '.join(tok for tok in t.split() if not any(ch.isdigit() for ch in tok))
    return stripped or t


def categorize_transactions(df):
    """
    Auto-assign categories from merchant/description text so uploaded
    statements don't end up sitting in an "UNKNOWN" bucket. Three passes,
    each only touching rows the previous pass couldn't resolve:
      1. Keyword substring match (longest keyword wins) against a curated,
         category-grouped list covering common Indian bank-statement text.
      2. Fuzzy string match against the same keyword list, for merchant text
         that's a near-miss (extra branch codes, slight misspellings, etc.).
      3. FALLBACK_CATEGORY ("OTHER") for genuinely unrecognizable text --
         never "UNKNOWN": a transaction with no signal at all is still a real
         transaction the user can recategorize, not a system failure state.
    """
    if 'category_id' not in df.columns:
        # Leave unset (not FALLBACK_CATEGORY yet) so the mask below actually
        # routes these rows through the matching passes instead of skipping them.
        df['category_id'] = None

    mask = df['category_id'].isna() | df['category_id'].astype(str).str.upper().isin(['UNKNOWN', '', 'NAN', 'NONE'])
    if not mask.any():
        return df

    if 'description' in df.columns:
        source_col = 'description'
    elif 'merchant_id' in df.columns:
        source_col = 'merchant_id'
    else:
        # No text to categorize from at all -- fall back rather than crash.
        df.loc[mask, 'category_id'] = FALLBACK_CATEGORY
        return df

    full_text = df.loc[mask, source_col].astype(str).str.upper()

    # UPI narrations carry the payer's VPA, bank codes and reference numbers
    # around the payee; match on the payee alone so that noise can't trigger
    # (or hide) a keyword. Falls back to the whole text if no payee is found.
    is_upi = full_text.map(is_upi_text)
    payees = full_text.map(lambda t: extract_upi_payee(t) or '')
    text = full_text.where(~(is_upi & (payees != '')), payees)

    resolved = pd.Series(False, index=text.index)
    assigned = pd.Series(FALLBACK_CATEGORY, index=text.index)

    # Pass 1: substring keyword match
    for keyword, category in _SORTED_KEYWORDS:
        remaining = ~resolved
        if not remaining.any():
            break
        hit = remaining & text.str.contains(re.escape(keyword), na=False)
        if hit.any():
            assigned.loc[hit] = category
            resolved.loc[hit] = True

    # A UPI payment whose payee we don't recognise is still a UPI payment.
    # (No fuzzy matching here: people's names are too easy to mis-match.)
    unrecognised_upi = is_upi & ~resolved
    assigned.loc[unrecognised_upi] = 'UPI'
    resolved.loc[unrecognised_upi] = True

    # Pass 2: fuzzy match, word by word (a whole merchant string like "ZOMTO
    # DELIVERY BLR123" is rarely close to a keyword as a whole, but one of its
    # words often is close to one of a keyword's words). Bounded so a large
    # unmatched batch can't make an upload hang.
    unresolved_idx = text.index[~resolved]
    if 0 < len(unresolved_idx) <= _FUZZY_MATCH_ROW_CAP:
        for idx in unresolved_idx:
            tokens = [t for t in re.split(r'[^A-Z0-9]+', text.loc[idx]) if len(t) >= 4]
            best_category, best_score = None, 0.0
            for token in tokens:
                close = difflib.get_close_matches(token, _WORD_LIST, n=1, cutoff=_FUZZY_MATCH_THRESHOLD)
                if not close:
                    continue
                score = difflib.SequenceMatcher(None, token, close[0]).ratio()
                if score > best_score:
                    best_score, best_category = score, _WORD_KEYWORDS[close[0]]
            if best_category:
                assigned.loc[idx] = best_category

    df.loc[mask, 'category_id'] = assigned
    return df

def _recurring_series_stats(df, cv_threshold=0.15):
    """
    Group transactions by (account, merchant) and compute cadence + amount-stability
    stats. A series only qualifies as recurring if it has >=3 occurrences, a median
    inter-arrival gap that falls in a weekly/monthly/annual band with low variance,
    and an amount coefficient of variation below `cv_threshold`.
    """
    df_sorted = df.sort_values(by=['account_id', 'merchant_id', 'date']).copy()
    df_sorted['gap'] = df_sorted.groupby(['account_id', 'merchant_id'])['date'].diff().dt.days

    stats = df_sorted.groupby(['account_id', 'merchant_id']).agg(
        tx_count=('transaction_id', 'count'),
        median_gap=('gap', 'median'),
        gap_std=('gap', 'std'),
        mean_amount=('amount', 'mean'),
        std_amount=('amount', 'std'),
        last_date=('date', 'max')
    ).reset_index()

    stats['amount_cv'] = (stats['std_amount'] / stats['mean_amount']).fillna(0)

    is_weekly = (stats['median_gap'] >= 6) & (stats['median_gap'] <= 8)
    is_monthly = (stats['median_gap'] >= 26) & (stats['median_gap'] <= 35)
    is_annual = (stats['median_gap'] >= 350) & (stats['median_gap'] <= 380)

    stats['cadence'] = np.select(
        [is_weekly, is_monthly, is_annual],
        ['weekly', 'monthly', 'annual'],
        default=None
    )

    qualifies = (
        (stats['tx_count'] >= 3) &
        (stats['amount_cv'] < cv_threshold) &
        (stats['gap_std'] <= 3) &
        stats['cadence'].notna()
    )
    return stats, qualifies

def detect_recurring_transactions(df, cv_threshold=0.15):
    """
    Identify subscriptions appearing 3+ times with stable cadence and amount.
    """
    stats, qualifies = _recurring_series_stats(df, cv_threshold)
    recurring = stats[qualifies]

    merged = df.merge(recurring[['account_id', 'merchant_id']], on=['account_id', 'merchant_id'], how='left', indicator=True)
    df['is_recurring'] = np.where(merged['_merge'] == 'both', True, False)

    logger.info(f"Flagged {df['is_recurring'].sum()} transactions as recurring.")
    return df

def summarize_recurring_series(df, cv_threshold=0.15):
    """
    Per-(account, merchant) detail for qualifying recurring series: cadence,
    typical amount, next expected date, and annualized cost.
    """
    stats, qualifies = _recurring_series_stats(df, cv_threshold)
    recurring = stats[qualifies].copy()

    recurring['next_expected_date'] = recurring['last_date'] + pd.to_timedelta(recurring['median_gap'], unit='D')
    recurring['annual_occurrences'] = 365.0 / recurring['median_gap']
    recurring['annualized_cost'] = recurring['mean_amount'] * recurring['annual_occurrences']

    return recurring[[
        'account_id', 'merchant_id', 'cadence', 'tx_count',
        'mean_amount', 'median_gap', 'next_expected_date', 'annualized_cost'
    ]].rename(columns={'mean_amount': 'typical_amount', 'median_gap': 'typical_gap_days'}).reset_index(drop=True)

def process_pipeline(df):
    df = clean_transactions(df)
    df = categorize_transactions(df)

    # Replace the source transaction_id with a deterministic hash so re-ingesting
    # the same rows (e.g. re-uploading a CSV) is idempotent at load time.
    df['transaction_id'] = compute_idempotency_key(df)
    df = df.drop_duplicates(subset=['transaction_id'])

    df = detect_recurring_transactions(df)

    # Keep the original timestamp for ML and analytics
    df['transaction_ts'] = df['date']

    # Create date_id (YYYYMMDD) for fact table
    df['date_id'] = df['date'].dt.strftime('%Y%m%d').astype(int)

    # Add currency if missing
    if 'currency' not in df.columns:
        df['currency'] = 'INR'

    return df

def derive_account_balances(df, starting_balances=None):
    """
    Derive daily account balances (opening, closing, inflow, outflow) from transactions.
    `starting_balances` maps account_id -> opening balance before the first
    transaction in `df`; accounts not present default to 50000.0.
    """
    starting_balances = starting_balances or {}

    # Compute inflow and outflow directly
    df['inflow_amt'] = np.where(df['transaction_type'] == 'CREDIT', df['amount'], 0)
    df['outflow_amt'] = np.where(df['transaction_type'] == 'DEBIT', df['amount'], 0)

    daily = df.groupby(['account_id', 'date_id']).agg(
        inflow=('inflow_amt', 'sum'),
        outflow=('outflow_amt', 'sum')
    ).reset_index()

    daily = daily.sort_values(by=['account_id', 'date_id'])

    balances = []
    for account_id, group in daily.groupby('account_id'):
        running_balance = float(starting_balances.get(account_id, 50000.0))
        for _, row in group.iterrows():
            opening = running_balance
            closing = opening + row['inflow'] - row['outflow']
            running_balance = closing
            
            balances.append({
                'account_id': account_id,
                'date_id': row['date_id'],
                'opening_balance': opening,
                'closing_balance': closing,
                'inflow': row['inflow'],
                'outflow': row['outflow']
            })
            
    # Clean up temp columns
    df.drop(columns=['inflow_amt', 'outflow_amt'], inplace=True)
    return pd.DataFrame(balances)
