import pytest
import pandas as pd
from etl.transform.clean import (
    clean_transactions,
    categorize_transactions,
    derive_account_balances,
    compute_idempotency_key,
    detect_recurring_transactions,
    summarize_recurring_series,
    process_pipeline,
    extract_upi_payee,
    is_upi_text,
    merchant_key,
)
from etl.extract.ingest import ingest_csv_transactions, fetch_api_transactions

def test_clean_transactions():
    data = {
        'transaction_id': ['1', '2', '2', '4'],
        'amount': [-100, 200, 200, None],
        'date': ['2023-01-01', '2023-01-02', '2023-01-02', '2023-01-03']
    }
    df = pd.DataFrame(data)
    
    cleaned_df = clean_transactions(df)
    
    assert len(cleaned_df) == 2 # 1 duplicate removed, 1 None removed
    assert cleaned_df['amount'].iloc[0] == 100 # Absolute value test
    
def test_categorize_transactions_without_merchant_text_falls_back_to_other():
    """No merchant_id/description column at all -> can't be crashed, and the
    fallback is 'OTHER', never 'UNKNOWN'."""
    data = {
        'transaction_id': ['1'],
        'merchant_name': ['Unknown Store']  # not a column categorize_transactions reads
    }
    df = pd.DataFrame(data)

    cat_df = categorize_transactions(df)

    assert 'category_id' in cat_df.columns
    assert cat_df['category_id'].iloc[0] == 'OTHER'


@pytest.mark.parametrize("merchant,expected_category", [
    ("ZOMATO ONLINE ORDER", "FOOD"),
    ("SWIGGY*BANGALORE", "FOOD"),
    ("BIGBASKET GROCERY", "GROCERY"),
    ("UBER TRIP 12345", "TRANSPORT"),
    ("INDIAN OIL PETROL PUMP", "FUEL"),
    ("AMAZON.IN", "SHOPPING"),
    ("NETFLIX.COM", "ENTERTAINMENT"),
    ("APOLLO PHARMACY", "HEALTH"),
    ("AIRTEL RECHARGE", "RECHARGE"),
    ("HOUSE RENT PAYMENT", "RENT"),
    ("ELECTRICITY BILL PAYMENT", "UTILITIES"),
    ("SCHOOL FEE Q3", "EDUCATION"),
    ("PHONEPE UPI PAYMENT", "UPI"),
    ("LOAN EMI DEDUCTION", "FINANCE"),
    ("SALARY CREDIT SEPT", "INCOME"),
    ("NEFT TRANSFER TO SAVINGS", "TRANSFER"),
])
def test_categorize_transactions_matches_common_bank_statement_text(merchant, expected_category):
    df = pd.DataFrame({
        'transaction_id': ['1'],
        'merchant_id': [merchant],
        'category_id': ['UNKNOWN']
    })

    result = categorize_transactions(df)

    assert result['category_id'].iloc[0] == expected_category


def test_categorize_transactions_never_leaves_unknown():
    df = pd.DataFrame({
        'transaction_id': ['1', '2', '3'],
        'merchant_id': ['ZOMATO DELHI', 'SWIGGY*OUTLET99', 'XJQZ_RANDOM_STRING_9981'],
        'category_id': ['UNKNOWN', 'UNKNOWN', 'UNKNOWN']
    })

    result = categorize_transactions(df)

    assert not (result['category_id'] == 'UNKNOWN').any()
    assert not result['category_id'].isna().any()


def test_categorize_transactions_fuzzy_matches_near_miss_spelling():
    """A merchant string that's close to a known keyword but not an exact
    substring should still resolve via the fuzzy-match pass, not fall through
    to OTHER."""
    df = pd.DataFrame({
        'transaction_id': ['1'],
        'merchant_id': ['ZOMTO'],  # missing an 'A' -- not a substring match
        'category_id': ['UNKNOWN']
    })

    result = categorize_transactions(df)

    assert result['category_id'].iloc[0] == 'FOOD'


def test_categorize_transactions_fuzzy_matches_word_within_longer_string():
    """Regression test: a merchant string with extra surrounding words (order
    IDs, city names, branch codes) must still resolve via a fuzzy match on the
    individual word closest to a keyword, not the whole string at once (which
    dilutes the similarity ratio below threshold and used to fall through to
    OTHER even though a human would clearly recognize it)."""
    df = pd.DataFrame({
        'transaction_id': ['1', '2'],
        'merchant_id': ['ZOMTO DELIVERY BLR123', 'SWIGY FOOD ORDER'],
        'category_id': ['UNKNOWN', 'UNKNOWN']
    })

    result = categorize_transactions(df)

    assert list(result['category_id']) == ['FOOD', 'FOOD']


def test_categorize_transactions_from_csv_upload_never_produces_unknown():
    """End-to-end through process_pipeline, mirroring a real bank CSV upload:
    no row should ever end up as UNKNOWN."""
    df = pd.DataFrame({
        'transaction_id': [str(i) for i in range(8)],
        'account_id': ['acc-1'] * 8,
        'date': ['2026-09-01'] * 8,
        'merchant_id': [
            'ZOMATO ORDER BANGALORE', 'AMAZON.IN PURCHASE', 'UBER TRIP',
            'HOUSE RENT PAYMENT', 'AIRTEL RECHARGE', 'SALARY CREDIT SEPTEMBER',
            'ELECTRICITY BILL BESCOM', 'SOME RANDOM MERCHANT XYZ123'
        ],
        'amount': [450, 1200, 180, 15000, 499, 55000, 1800, 75],
        'transaction_type': ['DEBIT', 'DEBIT', 'DEBIT', 'DEBIT', 'DEBIT', 'CREDIT', 'DEBIT', 'DEBIT']
    })

    result = process_pipeline(df)

    assert not (result['category_id'] == 'UNKNOWN').any()
    assert not result['category_id'].isna().any()
    assert result.loc[result['merchant_id'] == 'SOME RANDOM MERCHANT XYZ123', 'category_id'].iloc[0] == 'OTHER'


def test_categorize_transactions_leaves_already_categorized_rows_alone():
    df = pd.DataFrame({
        'transaction_id': ['1'],
        'merchant_id': ['ZOMATO'],
        'category_id': ['CUSTOM_CATEGORY']
    })

    result = categorize_transactions(df)

    assert result['category_id'].iloc[0] == 'CUSTOM_CATEGORY'

def test_derive_account_balances():
    tx_data = pd.DataFrame({
        'transaction_id': [1, 2, 3, 4],
        'account_id': ['A1', 'A1', 'A2', 'A1'],
        'date_id': [20260101, 20260101, 20260101, 20260102],
        'amount': [100.0, 50.0, 200.0, 30.0],
        'transaction_type': ['CREDIT', 'DEBIT', 'DEBIT', 'DEBIT']
    })
    
    balances = derive_account_balances(tx_data)
    
    assert len(balances) == 3 # A1 on day 1, A2 on day 1, A1 on day 2
    
    a1_day1 = balances[(balances['account_id'] == 'A1') & (balances['date_id'] == 20260101)].iloc[0]
    assert a1_day1['inflow'] == 100.0
    assert a1_day1['outflow'] == 50.0
    assert a1_day1['opening_balance'] == 50000.0
    assert a1_day1['closing_balance'] == 50050.0
    
    a1_day2 = balances[(balances['account_id'] == 'A1') & (balances['date_id'] == 20260102)].iloc[0]
    assert a1_day2['inflow'] == 0.0
    assert a1_day2['outflow'] == 30.0
    assert a1_day2['opening_balance'] == 50050.0
    assert a1_day2['closing_balance'] == 50020.0
    
    a2_day1 = balances[(balances['account_id'] == 'A2') & (balances['date_id'] == 20260101)].iloc[0]
    assert a2_day1['opening_balance'] == 50000.0
    assert a2_day1['closing_balance'] == 49800.0
    assert a2_day1['inflow'] == 0.0
    assert a2_day1['outflow'] == 200.0


# --- Idempotency (deterministic transaction_id) ---

def _base_upload_df():
    return pd.DataFrame({
        'transaction_id': ['orig-1', 'orig-2'],
        'account_id': ['A1', 'A1'],
        'merchant_id': ['ZOMATO', 'AMAZON'],
        'category_id': ['UNKNOWN', 'UNKNOWN'],
        'date': ['2026-01-01 10:00:00', '2026-01-02 11:00:00'],
        'amount': [250.0, 999.0],
        'transaction_type': ['DEBIT', 'DEBIT']
    })


def test_compute_idempotency_key_is_deterministic():
    df = _base_upload_df()
    df['date'] = pd.to_datetime(df['date'])

    key1 = compute_idempotency_key(df)
    key2 = compute_idempotency_key(df)

    assert list(key1) == list(key2)
    assert key1.nunique() == 2  # different rows -> different hashes


def test_compute_idempotency_key_changes_with_content():
    df = _base_upload_df()
    df['date'] = pd.to_datetime(df['date'])
    key_before = compute_idempotency_key(df)

    df.loc[0, 'amount'] = 251.0
    key_after = compute_idempotency_key(df)

    assert key_before.iloc[0] != key_after.iloc[0]


def test_process_pipeline_reingest_is_idempotent():
    """Re-running process_pipeline on the exact same source rows (simulating a
    re-uploaded CSV) must produce the same transaction_ids, so an ON CONFLICT
    upsert in etl.load.db.load_data creates no duplicate rows."""
    df1 = _base_upload_df()
    df2 = _base_upload_df()  # fresh copy, random source ids reused as-is

    result1 = process_pipeline(df1)
    result2 = process_pipeline(df2)

    assert set(result1['transaction_id']) == set(result2['transaction_id'])
    assert len(result1) == len(result2) == 2


# --- Recurring detection: cadence + amount stability ---

def _monthly_series(account_id, merchant_id, category_id, amount, n=6, jitter=0.0):
    base = pd.Timestamp('2026-01-01')
    rows = []
    for i in range(n):
        rows.append({
            'transaction_id': f'{merchant_id}-{i}',
            'account_id': account_id,
            'merchant_id': merchant_id,
            'category_id': category_id,
            'date': base + pd.Timedelta(days=30 * i),
            'amount': amount + jitter * (1 if i % 2 == 0 else -1),
            'transaction_type': 'DEBIT'
        })
    return rows


def _weekly_series(account_id, merchant_id, category_id, amounts):
    base = pd.Timestamp('2026-01-01')
    rows = []
    for i, amt in enumerate(amounts):
        rows.append({
            'transaction_id': f'{merchant_id}-{i}',
            'account_id': account_id,
            'merchant_id': merchant_id,
            'category_id': category_id,
            'date': base + pd.Timedelta(days=7 * i),
            'amount': amt,
            'transaction_type': 'DEBIT'
        })
    return rows


def test_detect_recurring_flags_true_subscription():
    rows = _monthly_series('A1', 'NETFLIX', 'ENTERTAINMENT', amount=499.0, n=6, jitter=0.0)
    df = pd.DataFrame(rows)

    result = detect_recurring_transactions(df.copy())

    assert result['is_recurring'].all()


def test_detect_recurring_does_not_flag_irregular_grocery_store():
    # Same store every week, but the basket total varies a lot -- not a subscription.
    amounts = [1200.0, 340.0, 2100.0, 560.0, 1800.0, 400.0]
    df = pd.DataFrame(_weekly_series('A1', 'BIGBASKET', 'GROCERY', amounts))

    result = detect_recurring_transactions(df.copy())

    assert not result['is_recurring'].any()


def test_detect_recurring_does_not_flag_one_off_merchant():
    df = pd.DataFrame([{
        'transaction_id': 'once-1',
        'account_id': 'A1',
        'merchant_id': 'RANDOM_STORE',
        'category_id': 'SHOPPING',
        'date': pd.Timestamp('2026-03-01'),
        'amount': 4500.0,
        'transaction_type': 'DEBIT'
    }])

    result = detect_recurring_transactions(df.copy())

    assert not result['is_recurring'].any()


def test_summarize_recurring_series_reports_cadence_and_cost():
    rows = _monthly_series('A1', 'NETFLIX', 'ENTERTAINMENT', amount=500.0, n=6, jitter=0.0)
    df = pd.DataFrame(rows)

    summary = summarize_recurring_series(df)

    assert len(summary) == 1
    series = summary.iloc[0]
    assert series['cadence'] == 'monthly'
    assert series['typical_amount'] == pytest.approx(500.0)
    assert series['annualized_cost'] == pytest.approx(500.0 * 12, rel=0.05)
    assert series['next_expected_date'] > df['date'].max()


def test_recurring_cv_threshold_is_configurable():
    # Amount varies ~10%: passes a loose threshold, fails a strict one.
    rows = _monthly_series('A1', 'GYM', 'HEALTH', amount=1000.0, n=6, jitter=80.0)
    df = pd.DataFrame(rows)

    loose = detect_recurring_transactions(df.copy(), cv_threshold=0.20)
    strict = detect_recurring_transactions(df.copy(), cv_threshold=0.01)

    assert loose['is_recurring'].all()
    assert not strict['is_recurring'].any()


# --- CSV ingestion ---

def test_ingest_csv_transactions_loads_existing_file(tmp_path):
    csv_path = tmp_path / "transactions.csv"
    csv_path.write_text("transaction_id,amount\n1,100.0\n2,200.0\n")

    df = ingest_csv_transactions(str(csv_path))

    assert df is not None
    assert len(df) == 2
    assert list(df.columns) == ["transaction_id", "amount"]


def test_ingest_csv_transactions_raises_for_missing_file(tmp_path):
    missing_path = tmp_path / "does_not_exist.csv"
    with pytest.raises(FileNotFoundError):
        ingest_csv_transactions(str(missing_path))


def test_ingest_csv_transactions_returns_none_for_malformed_csv(tmp_path):
    bad_path = tmp_path / "empty.csv"
    bad_path.write_text("")  # pandas raises EmptyDataError on a truly empty file

    result = ingest_csv_transactions(str(bad_path))

    assert result is None


def test_fetch_api_transactions_returns_empty_dataframe():
    result = fetch_api_transactions({"provider": "mock-bank"})
    assert isinstance(result, pd.DataFrame)
    assert result.empty


# --- UPI payee extraction / merchant identity ---


@pytest.mark.parametrize("narration,payee", [
    ("UPI-CRED CLUB-CRED.CLUB@AXISB-UTIB0000110", "CRED CLUB"),
    ("UPI-RAHUL SHARMA-RAHUL123@OKSBI-SBIN0001234-621311111111-UPI", "RAHUL SHARMA"),
    ("UPI-HEMANTH GOWDA Y", "HEMANTH GOWDA Y"),
    ("upi-diya", "DIYA"),
    ("UPI/621345678901/DR/RAHUL SHARMA/HDFC/rahul@oksbi", "RAHUL SHARMA"),
    ("UPI/P2M/621345678901/ZOMATO/HDFC", "ZOMATO"),
    ("IMPS/UPI/621345678901/CR/DIYA SHAH/SBIN", "DIYA SHAH"),
])
def test_extract_upi_payee(narration, payee):
    assert extract_upi_payee(narration) == payee


@pytest.mark.parametrize("narration", [
    "ZOMATO ORDER 123",                # not UPI at all
    "UPI-9742829892-2@YBL-HDFC000",    # payee is a phone number: no name to extract
    "UPI-",
    "UPI/621345678901/DR",
])
def test_extract_upi_payee_returns_none_when_there_is_no_name(narration):
    assert extract_upi_payee(narration) is None


def test_is_upi_text():
    assert is_upi_text("UPI-CRED CLUB-X") and is_upi_text("UPI/123/DR/X") and is_upi_text("IMPS/UPI/1")
    assert not is_upi_text("ZOMATO") and not is_upi_text("PAYMENT UPIX")


def test_merchant_key_is_stable_across_reference_numbers():
    a = merchant_key("UPI-RAHUL SHARMA-RAHUL123@OKSBI-SBIN0001234-621311111111-UPI")
    b = merchant_key("UPI-RAHUL SHARMA-RAHUL123@OKSBI-SBIN0001234-999999999999-UPI")
    assert a == b == "RAHUL SHARMA"
    assert merchant_key("ZOMATO ORDER 88123 BLR") == merchant_key("ZOMATO ORDER 99871 BLR") == "ZOMATO ORDER BLR"


def test_merchant_key_falls_back_to_the_text_when_nothing_is_left():
    assert merchant_key("12345 67890") == "12345 67890"


@pytest.mark.parametrize("narration,expected", [
    ("UPI-CRED CLUB-CRED.CLUB@AXISB-UTIB0000110", "FINANCE"),
    ("UPI-APPLE MEDIA SERVICES-APPLESERVICES.B", "ENTERTAINMENT"),
    ("UPI-AROGYA", "HEALTH"),
    ("UPI-ZOMATO-ZOMATO@HDFCBANK-HDFC0000001-621345678901", "FOOD"),
    ("UPI-RAHUL SHARMA-RAHUL123@OKSBI-SBIN0001234-621311111111", "UPI"),       # a person: stays generic UPI
    ("UPI-SMILE AURA-0790424A0071272.BQR@KOTAK", "UPI"),                        # unknown business: stays generic UPI
    ("UPI-9742829892-2@YBL-HDFC000", "UPI"),                                    # no payee name at all
    ("UPI/621345678901/DR/SWIGGY/HDFC/x@ybl", "FOOD"),
])
def test_upi_narrations_categorize_by_payee(narration, expected):
    df = pd.DataFrame({"description": [narration], "merchant_id": [narration[:50]]})
    assert categorize_transactions(df)["category_id"].iloc[0] == expected


def test_upi_vpa_noise_cannot_trigger_a_keyword():
    """Regression: a person whose VPA happens to contain a merchant name was categorized as that
    merchant, because the whole narration (VPA included) was searched for keywords."""
    df = pd.DataFrame({"description": ["UPI-RAHUL SHARMA-ZOMATO.FAN@OKSBI-SBIN0001234-621311111111"]})
    assert categorize_transactions(df)["category_id"].iloc[0] == "UPI"


def test_upi_persons_are_not_fuzzy_matched_to_keywords():
    # "SWIGGI" is one edit from SWIGGY; fine for a plain merchant string, but a UPI payee is a
    # registered name, so guessing from near-misses would mostly mislabel people.
    df = pd.DataFrame({"description": ["UPI-SWIGGI KUMAR-SWIGGI@OKSBI-SBIN0001234"]})
    assert categorize_transactions(df)["category_id"].iloc[0] == "UPI"


def test_non_upi_text_is_still_matched_on_the_whole_string():
    df = pd.DataFrame({"description": ["ATM WDL SBI ATM KORAMANGALA BLR", "UBER INDIA SYSTEMS PVT LTD BLR"]})
    assert list(categorize_transactions(df)["category_id"]) == ["TRANSFER", "TRANSPORT"]


# --- identical-looking transactions must stay distinct ---

def _same_day_chai(n):
    return pd.DataFrame({
        'account_id': ['a'] * n,
        'date': pd.to_datetime(['2026-08-01'] * n),
        'amount': [50.0] * n,
        'merchant_id': ['CHAI POINT'] * n,
        'description': ['UPI-CHAI POINT-CHAI@OKSBI'] * n,
    })


def test_identical_rows_get_distinct_ids_but_the_same_ids_every_time():
    """Two genuine 50 rupee chai payments on the same day are two transactions, not one duplicate."""
    keys = compute_idempotency_key(_same_day_chai(3))
    assert keys.nunique() == 3
    assert list(keys) == list(compute_idempotency_key(_same_day_chai(3)))  # a re-import maps row-for-row


def test_first_occurrence_keeps_the_same_id_as_a_single_row():
    """Ids of data loaded before this change must not shift."""
    assert compute_idempotency_key(_same_day_chai(3)).iloc[0] == compute_idempotency_key(_same_day_chai(1)).iloc[0]


def test_reimporting_a_file_with_repeated_rows_reproduces_the_same_ids():
    first = set(compute_idempotency_key(_same_day_chai(2)))
    again = set(compute_idempotency_key(_same_day_chai(2)))
    assert first == again and len(first) == 2


def test_idempotency_key_does_not_depend_on_other_rows_times():
    """Regression: str() of a datetime column omits the time only if EVERY row is at midnight,
    so a row's id used to change depending on which other rows were in the file."""
    midnight_only = pd.DataFrame({'account_id': ['a'], 'date': pd.to_datetime(['2026-08-01']), 'amount': [10.0], 'merchant_id': ['X']})
    with_a_timed_row = pd.DataFrame({'account_id': ['a', 'a'], 'date': pd.Series([pd.Timestamp('2026-08-01'), pd.Timestamp('2026-08-02 13:45:00')]),
                                     'amount': [10.0, 5.0], 'merchant_id': ['X', 'Y']})
    assert compute_idempotency_key(midnight_only).iloc[0] == compute_idempotency_key(with_a_timed_row).iloc[0]
