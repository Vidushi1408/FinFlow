import pandas as pd

from etl.load.db import load_data


def load_reference_catalog():
    """Load the merchants and categories the data generator (and the seeded app) use, so foreign keys
    from fact_transactions are satisfied. Idempotent."""
    from data_generator.generate_mock_data import MERCHANTS, EMPLOYERS, CLIENTS, RENT_MERCHANT, STIPEND_SOURCE

    everything = MERCHANTS + EMPLOYERS + CLIENTS + [
        RENT_MERCHANT, STIPEND_SOURCE, {"name": "Transfer", "category": "TRANSFER", "mcc": "0000"}
    ]
    merchants = {m["name"].replace(" ", "_").upper(): m for m in everything}

    categories = sorted({m["category"] for m in merchants.values()} | {"OTHER"})
    load_data(
        pd.DataFrame({"category_id": categories, "category_name": categories, "budget_amount": 0}),
        "dim_category", conflict_columns=["category_id"],
    )
    load_data(
        pd.DataFrame([
            {"merchant_id": mid, "merchant_name": m["name"], "category": m["category"], "mcc_code": m["mcc"]}
            for mid, m in merchants.items()
        ]),
        "dim_merchant", conflict_columns=["merchant_id"],
    )
