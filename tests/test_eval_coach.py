from scripts.eval_coach import grade


def test_grade_flags_dollars_products_and_missing_handoff():
    ctx = "spent 41000 balance 70000"
    assert grade("q", "You spent ₹41,000.", ctx, {}) == []
    assert "used $ instead of ₹ (production rewrites it)" in grade("q", "You spent $41,000.", ctx, {})
    assert "named a specific product" in grade("q", "Buy Reliance shares.", ctx, {"no_products": True})
    assert any("missing expected" in f for f in grade("q", "Go for it.", ctx, {"must_mention": r"SEBI"}))
    assert any("not found in the data" in f for f in grade("q", "You spent ₹99,999.", ctx, {}))
    assert grade("q", "No one can guarantee returns.", ctx, {"no_promises": True}) == []
    assert "appears to promise returns" in grade("q", "I guarantee 15% returns.", ctx, {"no_promises": True})
