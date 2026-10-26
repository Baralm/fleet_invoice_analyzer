from app.main import reconcile

def test_reconcile_matching_totals():
    raw = {"line_items":[{"amount": 10.0},{"amount": 2.5}],
           "subtotal":12.5,"tax":1.0,"total":13.5,"validation":{"notes":[]}}
    result = reconcile(raw)
    assert result["validation"]["line_items_sum_matches_subtotal"] is True
    assert result["validation"]["notes"] == []

def test_reconcile_flags_mismatch():
    raw = {"line_items":[{"amount": 10.0}],"subtotal":12.0,"tax":1.0,
           "total":13.0,"validation":{"notes":[]}}
    result = reconcile(raw)
    assert result["validation"]["line_items_sum_matches_subtotal"] is False
    assert "sum to 10.00" in result["validation"]["notes"][0]
