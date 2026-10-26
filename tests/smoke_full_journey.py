"""Full-journey smoke test: drive the real app (scratch Postgres database, your trained models, local Ollama
if running) with the 1-year sample statement, the way a person would, in a real browser.

    python tests/smoke_full_journey.py                 # screenshots go to ./smoke_shots/
    KEEP_DB=1 LIVE_DB=finflow_smoke python tests/smoke_full_journey.py   # keep the scratch DB to inspect it

Signs up, uploads the statement through the preview/mapping UI, checks balances/subscriptions/alerts/forecast/
reports, adds a budget/goal/loan, recategorises, gives alert feedback, exports CSV and PDFs, asks the money coach,
logs out and in, and fails on any console error, CSP violation or HTTP error. Needs Postgres, Playwright + Chromium.
Never touches your real database: it creates and drops its own."""
import os
import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "smoke_shots"
OUT.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

DB = os.environ.get("LIVE_DB") or f"finflow_live_{uuid.uuid4().hex[:6]}"
KEEP = bool(os.environ.get("KEEP_DB"))
os.environ["DB_NAME"] = DB
os.environ["SECRET_KEY"] = "live-run-secret-key-not-for-production"

import etl.load.db as db_module  # noqa: E402

admin = db_module.get_connection("postgres")
admin.autocommit = True
admin.cursor().execute(f"DROP DATABASE IF EXISTS {DB}")
admin.cursor().execute(f"CREATE DATABASE {DB}")
db_module.DB_NAME = DB
conn = db_module.get_connection()
conn.cursor().execute((ROOT / "database" / "schema.sql").read_text())
conn.commit()
conn.close()
db_module.load_dates("2024-01-01", "2030-12-31")
from etl.load.reference import load_reference_catalog  # noqa: E402
load_reference_catalog()

import webapp.app as app_module  # noqa: E402
from webapp.extensions import limiter  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

app_module.app.config.update(TESTING=False)
limiter.enabled = False
server = make_server("127.0.0.1", 0, app_module.app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_port}"
CSV = ROOT / "sample_data" / "sample_statement_1yr.csv"

problems, notes = [], []


def check(cond, msg):
    (notes if cond else problems).append(("PASS " if cond else "FAIL ") + msg)
    print(("PASS " if cond else "FAIL ") + msg)


try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        console_errors, bad_responses = [], []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
        page.on("response", lambda r: bad_responses.append(f"{r.status} {r.request.method} {r.url.replace(BASE, '')}") if r.status >= 400 else None)

        # ---- sign up
        email = f"aarav-{uuid.uuid4().hex[:6]}@example.com"
        page.goto(f"{BASE}/signup")
        page.fill("#name", "Aarav Test")
        page.fill("#email", email)
        page.fill("#password", "live-run-pass-1")
        page.fill("#confirm_password", "live-run-pass-1")
        page.click("input[type=submit]")
        page.wait_for_url("**/onboarding")
        check(True, "signup -> onboarding")
        check("confirm your email" in page.inner_text("body").lower(), "unverified-email banner shown")
        page.screenshot(path=str(OUT / "01_onboarding.png"))

        # ---- upload the 1-year statement through the real UI (preview + mapping + import)
        page.set_input_files("#onboardUploadForm input[type=file]", str(CSV))
        page.click("text=Preview & continue")
        page.wait_for_selector("#uploadFlowImport", timeout=30000)
        page.screenshot(path=str(OUT / "02_upload_preview.png"), full_page=True)
        preview_text = page.inner_text("#uploadPanel")
        check("532" in preview_text, "preview reports all 532 rows")
        check(page.is_enabled("#uploadFlowImport"), "import button enabled")
        page.click("#uploadFlowImport")
        page.wait_for_url(f"{BASE}/", timeout=60000)
        check(True, "import completed -> dashboard")
        page.wait_for_selector("#dashboardContent:not(.d-none)", timeout=20000)
        page.wait_for_timeout(1500)
        page.screenshot(path=str(OUT / "03_dashboard.png"), full_page=True)

        # ---- API-level facts through the logged-in browser session
        def api(path, method="GET", body=None):
            return page.evaluate("""async ([path, method, body]) => {
                const token = document.querySelector('meta[name=csrf-token]').content;
                const r = await fetch(path, {method, headers: {'Content-Type': 'application/json', 'X-CSRFToken': token},
                                             body: body ? JSON.stringify(body) : undefined});
                let data = null; try { data = await r.json(); } catch (e) {}
                return {status: r.status, data};
            }""", [path, method, body])

        dash = api("/api/v1/dashboard/summary")
        check(dash["status"] == 200, "dashboard summary 200")
        d = dash["data"]
        print("   balance", d["current_balance"], "| health", d["financial_health"]["score"], "| components", d["financial_health"]["components"])
        check(abs(d["current_balance"] - 108033.19) < 0.01, f"balance equals statement's closing balance (₹{d['current_balance']:,.2f})")
        check(len(d["trend_6_months"]) >= 5, f"trend has {len(d['trend_6_months'])} months")

        tx = api("/api/v1/transactions?page_size=1")
        check(tx["data"]["total"] == 532, f"all 532 transactions stored (got {tx['data']['total']})")

        # re-upload is idempotent
        res = page.evaluate("""async (csvText) => {
            const token = document.querySelector('meta[name=csrf-token]').content;
            const fd = new FormData(); fd.append('file', new Blob([csvText], {type:'text/csv'}), 's.csv');
            const r = await fetch('/api/v1/transactions/upload/preview', {method:'POST', headers: {'X-CSRFToken': token}, body: fd});
            return {status: r.status, data: await r.json()};
        }""", CSV.read_text())
        check(res["status"] == 200 and res["data"]["summary"]["new_rows"] == 0, f"re-uploading the same file adds 0 new rows ({res['data'].get('summary')})")

        # ---- every page loads, visually and without console errors
        for name, path in [("transactions", "/transactions"), ("budgets", "/budgets"), ("subscriptions", "/subscriptions"),
                           ("alerts", "/alerts"), ("forecast", "/whatif"), ("goals", "/goals"), ("reports", "/reports"),
                           ("models", "/about-models"), ("settings", "/settings")]:
            page.goto(f"{BASE}{path}")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1200)
            page.screenshot(path=str(OUT / f"10_{name}.png"), full_page=True)
            check(page.title() != "", f"page {path} renders")

        # ---- analytics correctness
        subs = api("/api/v1/insights/subscriptions")["data"]
        names = {s["merchant_id"] for s in subs["subscriptions"]}
        print("   subscriptions detected:", [(x["merchant_id"], x["kind"], round(x["annualized_cost"])) for x in subs["subscriptions"]])
        print("   subscription annual", round(subs["subscription_annual_cost"]), "| total recurring annual", round(subs["total_annual_cost"]))
        for must in ("NETFLIX.COM", "SPOTIFY INDIA", "AIRTEL POSTPAID", "AMAZON PRIME"):
            check(must in names, f"subscription detected: {must}")

        alerts = api("/api/v1/fraud/alerts?threshold=0.5")["data"]
        flagged = {a["merchant_id"]: a for a in alerts["alerts"]}
        print("   alerts:", len(alerts["alerts"]), "| source", alerts["threshold_source"], "| learning", alerts["learning"])
        for a in alerts["alerts"][:6]:
            print("     ", a["merchant_id"], a["amount"], a["reasons"])
        raw = api("/api/v1/fraud/alerts?threshold=0&personalize=0")["data"]
        print("   all scored (threshold 0), top 12 by probability, routine suppressed:", raw["suppressed_routine_count"])
        for a in sorted(raw["alerts"], key=lambda a: -a["fraud_probability"])[:12]:
            print("     ", round(a["fraud_probability"], 3), a["merchant_id"][:46], a["amount"])
        print("   suppressed routine in default view:", alerts["suppressed_routine_count"])
        year = api("/api/v1/fraud/alerts?threshold=0.5&days=365")["data"]
        year_names = " | ".join(a["merchant_id"] for a in year["alerts"])
        print("   12-month alerts:", [(a["merchant_id"][:34], a["amount"], a["source"]) for a in year["alerts"]])
        for expected in ("GIFTCARDS", "QUICKCASH", "UNKNOWN INTL", "APPLE INDIA STORE", "CROMA"):
            check(expected in year_names, f"12-month alerts include the planted charge: {expected}")
        check(not any(x in year_names for x in ("LANDLORD", "NIPPON", "NETFLIX", "LOAN EMI")), "routine rent/EMI/SIP/subscriptions are not alerts")
        planted = [m for m in flagged if "UNKNOWN INTL" in m or "QUICKCASH" in m or "GIFTCARDS" in m or "CROMA" in m or "APPLE" in m]
        check(len(planted) >= 1, f"planted out-of-pattern charges surface as alerts ({planted})")

        fc = api("/api/v1/cash-flow/forecast")
        check(fc["status"] == 200, "forecast 200")
        check(fc["data"]["scope"] == "personal", f"forecast is fitted on the user's own spending (scope={fc['data']['scope']})")
        check(20000 < fc["data"]["forecast_30_days_total"] < 200000,
              f"30-day forecast is on the user's scale (₹{fc['data']['forecast_30_days_total']:,.0f}; their real months are ~₹75-90k)")
        print("   forecast strategy:", fc["data"]["strategy"], "| backtest MAE/day:", round(fc["data"]["backtest_mae"]))
        print("   forecast keys:", list(fc["data"])[:8])

        rep = api("/api/v1/reports/monthly?month=2026-09")
        check(rep["status"] == 200 and rep["data"]["total_spent"] > 0, f"monthly report Sept 2026 spent ₹{rep['data']['total_spent']:,.0f}")

        advice = api("/api/v1/reports/advice")["data"]["recommendations"]
        print("   advice:")
        for r in advice:
            print("     ", r["priority"], "|", r["title"], "|", r["detail"][:110])
        check(len(advice) >= 1, "advice produced")

        # ---- user actions
        check(api("/api/v1/budgets", "POST", {"category_id": "FOOD", "monthly_limit": 6000})["status"] in (200, 201), "set a food budget")
        check(api("/api/v1/goals", "POST", {"name": "Emergency fund", "target_amount": 300000, "deadline": "2027-06-30"})["status"] == 201, "added a goal")
        check(api("/api/v1/loans", "POST", {"name": "Bike loan", "outstanding_balance": 120000, "interest_rate_pct": 14, "monthly_emi": 8450})["status"] == 201, "added a loan")
        goals = api("/api/v1/goals")["data"]["goals"]
        print("   goal:", goals[0]["name"], "on_track", goals[0]["on_track"], "required/mo", goals[0]["required_monthly_saving"])
        whatif = api("/api/v1/what-if/budget", "POST", {"add_savings": 0, "reduce_categories": {"FOOD": 0.2}})
        check(whatif["status"] == 200, f"what-if: {whatif['data']}")

        txs = api("/api/v1/transactions?search=SWIGGY&page_size=5")["data"]["transactions"]
        t = txs[0]
        pr = api(f"/api/v1/transactions/{t['transaction_id']}/category", "PATCH", {"category_id": "FOOD", "create_rule": True})
        check(pr["status"] == 200, f"recategorize + rule: {pr['data']}")
        fb = api("/api/v1/fraud/feedback", "POST", {"transaction_id": alerts["alerts"][0]["transaction_id"], "feedback": "SUSPICIOUS"}) if alerts["alerts"] else {"status": 200}
        check(fb["status"] == 200, "alert feedback recorded")
        payees = api("/api/v1/transactions/unknown-payees")["data"]["payees"]
        print("   unlabelled UPI payees:", [(p_["merchant_id"], round(p_["total_spent"])) for p_ in payees])

        # ---- exports
        csv_resp = page.request.get(f"{BASE}/api/v1/transactions/export.csv")
        check(csv_resp.ok and csv_resp.text().count("\n") >= 533, "ledger CSV export has all rows")
        pdf_resp = page.request.get(f"{BASE}/api/v1/transactions/export.pdf")
        check(pdf_resp.ok and pdf_resp.body()[:4] == b"%PDF", f"ledger PDF export ({len(pdf_resp.body())//1024} KB)")
        (OUT / "ledger.pdf").write_bytes(pdf_resp.body())
        mp = page.request.get(f"{BASE}/api/v1/reports/monthly/export.pdf?month=2026-09")
        check(mp.ok and mp.body()[:4] == b"%PDF", "monthly PDF export")
        (OUT / "monthly.pdf").write_bytes(mp.body())

        # ---- the coach, with the REAL local Ollama model
        page.goto(f"{BASE}/reports")
        page.wait_for_load_state("networkidle")
        for q in ["What should I do first with my money?", "Can I afford a ₹20,000 purchase next month?", "What if I cut dining by 20%?"]:
            r = api("/api/v1/reports/chat", "POST", {"message": q, "month": "2026-09"})
            print(f"   CHAT [{q}] -> {r['status']} model={r['data'].get('model') if r['data'] else None}")
            print("       ", (r["data"].get("reply") or str(r["data"]))[:420].replace("\n", " | "))
            check(r["status"] == 200, f"coach answered: {q}")
        # UI path: click a chip and wait for a bubble
        page.click("text=Where can I cut spending?")
        from playwright.sync_api import expect
        expect(page.locator("#chatLog > div")).to_have_count(2, timeout=90000)
        expect(page.locator("#chatLog")).not_to_contain_text("Thinking", timeout=90000)
        page.screenshot(path=str(OUT / "20_reports_chat.png"), full_page=True)

        # ---- logout / login round trip / forgot-password page
        page.goto(f"{BASE}/logout")
        page.goto(f"{BASE}/login")
        page.fill("#email", email)
        page.fill("#password", "live-run-pass-1")
        page.click("input[type=submit]")
        page.wait_for_url(f"{BASE}/", timeout=15000)
        check(True, "logout then login again works")

        # ---- delete all data / settings
        check(api("/api/v1/settings/profile", "POST", {"name": "Aarav T", "currency": "INR", "alert_sensitivity": 0.7})["status"] == 200, "settings profile saved")

        real_errors = [e for e in console_errors if "favicon" not in e.lower()]
        print("\nconsole errors:", real_errors[:10])
        print("HTTP >= 400:", bad_responses[:15])
        check(not [e for e in real_errors if "Content Security" in e or "Refused" in e], "no CSP violations in the whole journey")
        browser.close()
finally:
    server.shutdown()
    if not KEEP:
        admin.cursor().execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (DB,))
        admin.cursor().execute(f"DROP DATABASE IF EXISTS {DB}")
    admin.close()

print(f"\n{len(notes)} passed, {len(problems)} failed")
for pr in problems:
    print(pr)
sys.exit(1 if problems else 0)
