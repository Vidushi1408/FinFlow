"""Real-browser tests of the main flows (Chromium via Playwright) against the real app, served from a thread
and pointed at a throwaway Postgres. Skipped cleanly when Playwright, its browser, or Postgres is missing.
Install locally with: pip install playwright && playwright install chromium"""
import threading
import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

sync_api = pytest.importorskip("playwright.sync_api")

import etl.load.db as db_module  # noqa: E402
import webapp.app as app_module  # noqa: E402
import webapp.routes.reports as reports_module  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402
from webapp.extensions import limiter  # noqa: E402


@pytest.fixture(scope="module")
def base_url(pg_db):
    url = f"postgresql://{db_module.DB_USER}:{db_module.DB_PASSWORD}@{db_module.DB_HOST}:{db_module.DB_PORT}/{pg_db}"
    engine = create_engine(url)
    original_session = app_module.SessionLocal
    app_module.SessionLocal = sessionmaker(bind=engine)
    app_module.app.config.update(TESTING=False, SECRET_KEY="e2e-secret-key", WTF_CSRF_ENABLED=True)
    limiter.enabled = False
    server = make_server("127.0.0.1", 0, app_module.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    limiter.enabled = True
    app_module.SessionLocal = original_session
    engine.dispose()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # browser binary not installed
            pytest.skip(f"Chromium not available for Playwright: {e}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    page = context.new_page()
    yield page
    context.close()


def _sign_up_and_load_demo(page, base_url):
    page.goto(f"{base_url}/signup")
    page.fill("#name", "E2E Tester")
    page.fill("#email", f"e2e-{uuid.uuid4().hex[:8]}@example.com")
    page.fill("#password", "e2e-password-1")
    page.fill("#confirm_password", "e2e-password-1")
    page.click("input[type=submit]")
    page.wait_for_url("**/onboarding")
    page.select_option("#persona", "salaried")
    page.click("input[value='Load demo data']")
    page.wait_for_url(f"{base_url}/")


def test_signup_demo_data_dashboard_and_coach_card(page, base_url):
    _sign_up_and_load_demo(page, base_url)
    sync_api.expect(page.locator("#dashboardContent")).to_be_visible(timeout=15000)
    assert page.inner_text("#healthScore") not in ("--", "")
    sync_api.expect(page.locator("#coachTop .badge").first).to_be_visible(timeout=15000)


def test_transactions_page_filters_and_exports(page, base_url):
    _sign_up_and_load_demo(page, base_url)
    page.goto(f"{base_url}/transactions")
    sync_api.expect(page.locator("#tableWrap")).to_be_visible(timeout=15000)
    page.select_option("#fType", "CREDIT")
    page.fill("#fSearch", "")
    page.keyboard.press("Tab")
    page.wait_for_timeout(800)
    with page.expect_download() as dl:
        page.click("#exportLink")
    header = open(dl.value.path(), encoding="utf-8").readline().strip()
    assert header == "date,account,merchant,category,type,amount,recurring"


def test_report_chat_chip_gets_an_answer_and_affordability_uses_rules(page, base_url, monkeypatch):
    monkeypatch.setattr(reports_module, "_call_ollama", lambda messages: "Trim dining a little.")
    _sign_up_and_load_demo(page, base_url)
    page.goto(f"{base_url}/reports")
    sync_api.expect(page.locator("#adviceList .badge").first).to_be_visible(timeout=15000)

    page.click("text=Where can I cut spending?")
    sync_api.expect(page.locator("#chatLog")).to_contain_text("Trim dining a little.", timeout=15000)

    page.click("text=Can I afford a ₹20,000 purchase next month?")
    sync_api.expect(page.locator("#chatLog")).to_contain_text("₹20,000 purchase", timeout=15000)


def test_password_reset_page_is_reachable_from_login(page, base_url):
    page.goto(f"{base_url}/login")
    page.click("text=Forgot your password?")
    page.wait_for_url("**/forgot-password")
    sync_api.expect(page.locator("form")).to_be_visible()


def test_every_page_runs_under_the_enforced_csp_and_buttons_still_work(page, base_url):
    """The CSP is enforced (no inline handlers, nonce'd scripts). Any blocked script or handler shows up
    as a console error, so walk every page, click the delegated buttons, and demand a clean console."""
    violations = []
    page.on("console", lambda m: violations.append(m.text) if "Content Security Policy" in m.text or "Refused to" in m.text else None)
    page.on("pageerror", lambda e: violations.append(str(e)))

    _sign_up_and_load_demo(page, base_url)
    for path in ("/", "/transactions", "/budgets", "/subscriptions", "/alerts", "/goals", "/whatif",
                 "/reports", "/about-models", "/settings"):
        page.goto(f"{base_url}{path}")
        page.wait_for_load_state("networkidle")

    # Delegated handlers actually fire: add a loan on the Goals page, then delete it.
    page.goto(f"{base_url}/goals")
    page.fill("#loanName", "CSP loan")
    page.fill("#loanBalance", "50000")
    page.fill("#loanRate", "14")
    page.fill("#loanEmi", "2500")
    page.click("text=Add loan")
    sync_api.expect(page.locator("#loanList")).to_contain_text("CSP loan", timeout=10000)
    page.click("#loanList button[data-click=deleteLoanClick]")
    sync_api.expect(page.locator("#loanList")).not_to_contain_text("CSP loan", timeout=10000)

    assert violations == [], violations
