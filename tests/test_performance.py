"""
Guards against order-of-magnitude slowdowns (an accidental N+1, a cross join, loading far more than needed).
Measured on a laptop with 50,000 transactions for one user (roughly a decade of heavy use), the slowest endpoint
takes ~0.2 s and the rest well under that; the budget below is 10x the slowest, so it should not flake on a slow CI
runner but a real regression will trip it. Run `python scripts/benchmark_endpoints.py` for the detailed numbers.
"""
import pytest

import webapp.app as app_module
import webapp.auth as auth_module
from scripts.benchmark_endpoints import ENDPOINTS, logged_in_client, scratch_database, seed_user_with_history, time_endpoints

pytestmark = pytest.mark.integration

ROWS = 50_000
BUDGET_MS = 2000


def test_every_endpoint_stays_fast_with_50k_transactions(pg_db, monkeypatch):
    # restore the app globals that logged_in_client() overwrites once this test is done
    monkeypatch.setattr(app_module, "SessionLocal", app_module.SessionLocal)
    monkeypatch.setattr(auth_module.login_manager, "_user_callback", auth_module.login_manager._user_callback)

    with scratch_database():
        user_id, _ = seed_user_with_history(ROWS)
        results = time_endpoints(logged_in_client(user_id), repeats=1)

    assert len(results) == len(ENDPOINTS)
    assert all(status == 200 for _, status, _ in results), [(u, s) for _, s, u in results if s != 200]
    too_slow = [f"{ms:.0f} ms  {url}" for ms, _, url in results if ms > BUDGET_MS]
    assert not too_slow, f"over the {BUDGET_MS} ms budget with {ROWS:,} transactions:\n" + "\n".join(too_slow)
