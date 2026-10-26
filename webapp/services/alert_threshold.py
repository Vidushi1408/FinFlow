"""Per-user alert cutoff.

The fraud model is global: it scores every user against population-level patterns. A user whose normal
life looks unusual to that population (large irregular freelance payments, say) would see a stream of
alerts no matter how the global sensitivity is set. Once there is enough of their own history, the cutoff
is raised to just above what *their own* normal activity scores, so alerts stay rare for them too.

It only ever raises the cutoff above the user's chosen sensitivity, never lowers it, and is capped so a
transaction the model finds extremely unusual (probability above PERSONAL_CUTOFF_CAP) always alerts.
"""
import numpy as np

MIN_HISTORY = 200          # scored debit transactions needed before personalising
HISTORY_QUANTILE = 0.97    # alert only on what is rarer than ~3% of the user's own history
PERSONAL_CUTOFF_CAP = 0.97


def effective_threshold(history_probabilities, sensitivity):
    """(cutoff, source) where source is 'global' (the user's sensitivity) or 'personal' (raised)."""
    history = np.asarray(list(history_probabilities), dtype=float)
    history = history[~np.isnan(history)]
    if len(history) < MIN_HISTORY:
        return float(sensitivity), "global"
    personal = min(float(np.quantile(history, HISTORY_QUANTILE)), PERSONAL_CUTOFF_CAP)
    if personal > sensitivity:
        return personal, "personal"
    return float(sensitivity), "global"
