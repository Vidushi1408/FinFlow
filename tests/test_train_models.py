import pandas as pd

from train_models import labelled_rows


def test_imported_statement_rows_are_excluded_from_evaluation():
    """They carry no fraud labels; scoring the model against anything it wrote itself would measure
    how well it agrees with itself, not how well it detects fraud."""
    df = pd.DataFrame({
        "transaction_id": ["gen-1", "gen-2", "real-1", "real-2"],
        "is_fraud": [False, True, True, False],
        "institution": ["HDFC", "SBI", "UPLOADED_STATEMENT", "UPLOADED_STATEMENT"],
    })
    assert list(labelled_rows(df)["transaction_id"]) == ["gen-1", "gen-2"]


def test_all_generated_data_is_kept():
    df = pd.DataFrame({"is_fraud": [False, True], "institution": ["HDFC", "Axis"]})
    assert len(labelled_rows(df)) == 2


def test_frames_without_an_institution_column_are_left_alone():
    df = pd.DataFrame({"is_fraud": [False, True]})
    assert len(labelled_rows(df)) == 2


def test_frame_with_only_imported_rows_has_nothing_to_evaluate():
    df = pd.DataFrame({"is_fraud": [True], "institution": ["UPLOADED_STATEMENT"]})
    assert labelled_rows(df).empty
