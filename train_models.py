import sys
import pandas as pd
from etl.load.db import get_engine
from ml.fraud_model import train_fraud_model, predict_fraud, save_fraud_metrics
from ml.cashflow_model import train_cashflow_model
from sklearn.metrics import precision_score, recall_score, f1_score, roc_auc_score, confusion_matrix

from logging_config import logger

# Accounts built from a user's own imported bank statement have no fraud labels at all.
UNLABELLED_INSTITUTIONS = ('UPLOADED_STATEMENT',)


def labelled_rows(df):
    """Rows whose is_fraud is genuine ground truth (generated data). Imported-statement rows are
    excluded from evaluation: they carry no labels, and scoring the model against anything it wrote
    itself would just measure how well it agrees with itself."""
    if 'institution' not in df.columns:
        return df
    return df[~df['institution'].isin(UNLABELLED_INSTITUTIONS)]


def load_data_from_db():
    logger.info("Loading data from database")
    engine = get_engine()

    # Query transactions
    query = """
    SELECT
        f.transaction_id, f.account_id, f.merchant_id, f.category_id,
        f.date_id, f.amount, f.transaction_type, f.is_fraud, f.is_recurring,
        f.transaction_ts, a.institution
    FROM fact_transactions f
    LEFT JOIN dim_account a ON a.account_id = f.account_id
    ORDER BY f.transaction_ts ASC
    """
    df = pd.read_sql(query, engine)
    logger.info(f"Loaded {len(df)} transactions")
    return df

def main():
    df = load_data_from_db()
    if len(df) == 0:
        logger.error("No data available to train models.")
        return

    # Chronological 80/20 Split
    split_idx = int(len(df) * 0.8)
    train_df = df.iloc[:split_idx].copy()
    test_df = df.iloc[split_idx:].copy()

    logger.info(f"Train size: {len(train_df)}, Test size: {len(test_df)}")

    logger.info("Training fraud model")
    # Train on train_df
    fraud_model, fraud_metadata = train_fraud_model(train_df)

    logger.info("Evaluating fraud model")
    # Evaluate on the labelled part of test_df. is_fraud is the ground-truth label, used ONLY for
    # this offline evaluation -- never to filter what gets scored at inference.
    eval_df = labelled_rows(test_df)
    if eval_df.empty:
        logger.warning("No labelled rows in the holdout set; skipping fraud evaluation metrics")
        logger.info("Training cash flow model")
        train_cashflow_model(df)
        return
    logger.info(f"Evaluating on {len(eval_df)} labelled holdout rows ({len(test_df) - len(eval_df)} unlabelled imported rows excluded)")
    y_test = eval_df['is_fraud'].astype(int)

    scored = predict_fraud(eval_df, model=fraud_model, metadata=fraud_metadata)
    y_prob = scored['fraud_probability']
    # The alerting decision uses the threshold calibrated at training time,
    # not an arbitrary probability cutoff.
    y_pred = scored['is_flagged'].astype(int)

    precision = precision_score(y_test, y_pred, zero_division=0)
    recall = recall_score(y_test, y_pred, zero_division=0)
    f1 = f1_score(y_test, y_pred, zero_division=0)
    tn, fp, fn, tp = confusion_matrix(y_test, y_pred, labels=[0, 1]).ravel()
    # Handle cases where the test set might not have both classes (very unlikely with 10k rows but safe practice)
    try:
        auc = roc_auc_score(y_test, y_prob)
    except ValueError:
        auc = 0.5

    metrics = {
        'precision': float(precision),
        'recall': float(recall),
        'f1': float(f1),
        'auroc': float(auc),
        'confusion_matrix': {'tn': int(tn), 'fp': int(fp), 'fn': int(fn), 'tp': int(tp)},
        'holdout_rows': int(len(eval_df))
    }
    save_fraud_metrics(metrics)

    logger.info(
        f"Fraud model metrics (holdout): precision={precision:.4f} recall={recall:.4f} "
        f"f1={f1:.4f} auroc={auc:.4f} confusion_matrix=TP={tp},FP={fp},FN={fn},TN={tn}"
    )

    logger.info("Training cash flow model")
    # For cashflow, we usually train on the full historical data up to "today"
    # because time series needs the most recent data for rolling windows
    train_cashflow_model(df)

    logger.info("All models trained and evaluated successfully")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Model training failed")
        sys.exit(1)
