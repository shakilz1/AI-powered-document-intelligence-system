"""Task 3: validation rules + PDF<->Excel cross-verification."""
from app.pipeline.crossverify import cross_verify
from app.pipeline.validation import needs_review, validate_invoice


def good():
    return {"invoice_number": "A-1", "invoice_date": "2025-01-10", "supplier_name": "Acme", "customer_name": "Contoso",
            "subtotal": 100.0, "tax": 20.0, "total": 120.0, "currency": "USD", "confidence": {}, "uncertain_fields": [],
            "line_items": [{"description": "x", "quantity": 2, "unit_price": 25.0, "amount": 50.0},
                           {"description": "y", "quantity": 1, "unit_price": 50.0, "amount": 50.0}]}


def codes(inv):
    return {i.code for i in validate_invoice(inv)}


def test_clean_invoice_passes():
    issues = validate_invoice(good())
    assert issues == [] and not needs_review(issues)


def test_total_mismatch():
    inv = good(); inv["total"] = 121.0
    assert "total_mismatch" in codes(inv) and needs_review(validate_invoice(inv))


def test_line_items_inconsistent_with_subtotal_and_line_math():
    inv = good(); inv["line_items"][0]["amount"] = 60.0
    c = codes(inv)
    assert {"line_items_mismatch", "line_item_math"} <= c


def test_small_errors_are_not_hidden_by_relative_tolerance():
    inv = good()
    inv.update(subtotal=29308.02, tax=2930.80, total=32238.82 + 10, line_items=[])
    assert "total_mismatch" in codes(inv)


def test_missing_fields():
    inv = good(); inv["customer_name"] = None; inv["invoice_number"] = None
    issues = [i for i in validate_invoice(inv) if i.code == "missing_field"]
    assert {i.field for i in issues} == {"customer_name", "invoice_number"}
    assert all(i.severity == "error" for i in issues)


def test_missing_tax_when_amounts_differ():
    inv = good(); inv["tax"] = None
    assert "tax_missing" in codes(inv)


def test_incorrect_numeric_values():
    inv = good(); inv["total"] = "12O.00"
    assert "invalid_number" in codes(inv)
    inv = good(); inv["tax"] = -5.0; inv["total"] = 95.0
    assert "negative_amount" in codes(inv)
    inv = good(); inv["tax"] = 90.0; inv["total"] = 190.0
    assert "implausible_tax_rate" in codes(inv)
    inv = good(); inv["total"] = 50.0
    assert "total_below_subtotal" in codes(inv)


def test_dates_currency_and_confidence():
    inv = good(); inv["invoice_date"] = "2999-01-01"
    assert "future_date" in codes(inv)
    inv = good(); inv["invoice_date"] = "2025-13-45"
    assert "invalid_date" in codes(inv)
    inv = good(); inv["currency"] = "XYZ"
    assert "unknown_currency" in codes(inv)
    inv = good(); inv["uncertain_fields"] = ["total"]
    assert needs_review(validate_invoice(inv))
    inv = good(); inv["confidence"] = {"supplier_name": 0.5}
    assert "low_confidence" in codes(inv)
    inv = good(); inv["customer_name"] = "ACME"
    assert "supplier_equals_customer" in codes(inv)


def test_cross_verify_pdf_vs_excel():
    left = [dict(good(), id="p1"), dict(good(), id="p2", invoice_number="A-2", total=500.0),
            dict(good(), id="p3", invoice_number="A-3", total=777.0)]
    right = [dict(good(), id="x1", supplier_name="ACME Inc."),  # same after name normalisation
             dict(good(), id="x2", invoice_number="a2", total=550.0),
             dict(good(), id="x9", invoice_number="Z-9")]
    rep = cross_verify(left, right)
    s = rep["summary"]
    assert (s["matched"], s["consistent"], s["with_differences"], s["unmatched_left"], s["unmatched_right"]) == (2, 1, 1, 1, 1)
    diff = next(m for m in rep["matches"] if not m["consistent"])
    assert diff["differences"] == [{"field": "total", "left": 500.0, "right": 550.0}]


# ---- business rules applied after extraction (processor._to_row -> ExtractedInvoice.apply_defaults)
from app.pipeline.types import ExtractedInvoice as _EI


def test_missing_tax_becomes_zero_and_subtotal_from_lines():
    e = _EI(invoice_number="5029", total=26250.0, line_items=[{"amount": 900}, {"amount": 25350.0}])
    e.apply_defaults()
    assert e.tax == 0.0 and e.subtotal == 26250.0
    assert e.derived_fields == ["tax", "subtotal"]
    assert any("tax set to 0" in n for n in e.notes) and any("sum of 2 line amounts" in n for n in e.notes)
    issues = validate_invoice({**e.to_dict(), "invoice_date": "2026-11-02", "supplier_name": "S",
                               "customer_name": "C", "currency": "BDT"})
    assert not [i for i in issues if i.code in ("missing_field", "total_mismatch", "tax_missing")]


def test_printed_values_are_never_overwritten():
    e = _EI(total=110.0, subtotal=100.0, tax=10.0, line_items=[{"amount": 50}, {"amount": 40}])
    e.apply_defaults()
    assert (e.subtotal, e.tax, e.derived_fields) == (100.0, 10.0, [])


def test_subtotal_not_derived_when_a_line_amount_is_missing():
    e = _EI(total=100.0, line_items=[{"amount": 50}, {"amount": None}])
    e.apply_defaults()
    assert e.subtotal is None and e.tax == 0.0


def test_derived_subtotal_still_flags_misread_lines():
    # Lines misread (sum 23,250) vs printed total 26,250 with no tax: still flagged.
    e = _EI(total=26250.0, line_items=[{"amount": 20000}, {"amount": 3250}])
    e.apply_defaults()
    issues = validate_invoice(e.to_dict())
    assert any(i.code == "total_mismatch" for i in issues)


def test_empty_invoice_gets_no_defaults():
    e = _EI()
    e.apply_defaults()
    assert e.tax is None and e.derived_fields == []
