import numpy as np
import pytest

from webapp.services.alert_threshold import MIN_HISTORY, PERSONAL_CUTOFF_CAP, effective_threshold


def test_too_little_history_keeps_the_users_own_sensitivity():
    assert effective_threshold([0.99] * (MIN_HISTORY - 1), 0.8) == (0.8, "global")
    assert effective_threshold([], 0.8) == (0.8, "global")


def test_noisy_history_raises_the_cutoff_above_sensitivity():
    rng = np.random.default_rng(0)
    noisy = rng.uniform(0.7, 0.95, 500)           # normal life already scores high for the global model
    cutoff, source = effective_threshold(noisy, 0.8)
    assert source == "personal" and 0.8 < cutoff <= PERSONAL_CUTOFF_CAP


def test_tame_history_never_lowers_the_cutoff():
    tame = np.random.default_rng(1).uniform(0.05, 0.4, 500)
    assert effective_threshold(tame, 0.8) == (0.8, "global")


def test_cutoff_is_capped_so_extreme_scores_always_alert():
    cutoff, _ = effective_threshold([0.999] * 500, 0.5)
    assert cutoff == PERSONAL_CUTOFF_CAP and 0.99 > cutoff


def test_nan_scores_are_ignored():
    assert effective_threshold([float("nan")] * 500, 0.8) == (0.8, "global")


@pytest.mark.parametrize("sens", [0.0, 0.5, 1.0])
def test_never_below_sensitivity(sens):
    cutoff, _ = effective_threshold(np.random.default_rng(2).uniform(0, 1, 400), sens)
    assert cutoff >= sens
