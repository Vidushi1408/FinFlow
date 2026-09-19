import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "webapp" / "templates"
APP_JS = Path(__file__).resolve().parent.parent / "webapp" / "static" / "js" / "app.js"

# Fields that can carry attacker-controlled text (uploaded CSV merchant names,
# user-typed goal/category names, etc.). Numeric/enum fields are not listed.
UNTRUSTED_FIELDS = [
    "merchant_id", "category_id", "category_name", "transaction_id", "account_id",
    "goal_id", "name", "institution", "account_type", "account_number_masked",
]
RAW_INTERPOLATION = re.compile(r"\$\{\s*(?:[a-z]\w*)\.(" + "|".join(UNTRUSTED_FIELDS) + r")\s*\}")


def test_escape_html_helper_is_defined():
    assert "function escapeHtml" in APP_JS.read_text()


def test_no_template_interpolates_untrusted_fields_unescaped():
    offenders = []
    for path in sorted(TEMPLATES.glob("*.html")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            # innerText assignments are safe; only HTML-string building matters.
            if "innerText" in line and "innerHTML" not in line:
                continue
            if RAW_INTERPOLATION.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "Unescaped untrusted fields:\n" + "\n".join(offenders)
