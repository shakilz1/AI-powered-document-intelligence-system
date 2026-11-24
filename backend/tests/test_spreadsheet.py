"""Task 1/2: spreadsheet & CSV parsing and structured extraction."""
import pandas as pd

from app.pipeline import extractor
from app.pipeline.loaders import load_document
from eval import synth


def _extract(path):
    return extractor.extract_document(load_document(path))


def test_xlsx_multiple_sheets_title_row_and_grouping(tmp_path, make_invoice):
    a, b = [make_invoice() for _ in range(3)], [make_invoice() for _ in range(2)]
    p = tmp_path / "ledger.xlsx"
    synth.render_xlsx(a, b, p)
    doc = load_document(p)
    assert {n for n, _ in doc.tables} == {"Line Items", "Summary"}  # empty "Notes" sheet skipped
    assert any("empty" in w for w in doc.warnings)
    res = extractor.extract_document(doc)
    assert [r.invoice_number for r in res] == [i["invoice_number"] for i in a + b]
    first = res[0]
    assert first.method == "spreadsheet"
    assert len(first.line_items) == len(a[0]["line_items"])  # rows grouped by invoice number
    assert first.total == a[0]["total"] and first.currency == a[0]["currency"]
    assert res[-1].line_items == []  # summary sheet has no line items — none invented


def test_xls_legacy(tmp_path, make_invoice):
    invs = [make_invoice() for _ in range(3)]
    p = tmp_path / "legacy.xls"
    synth.render_xls(invs, p)
    res = _extract(p)
    assert len(res) == 3 and res[1].supplier_name == invs[1]["supplier_name"]
    assert res[1].tax == invs[1]["tax"]


def test_csv_semicolon_decimal_comma_dayfirst(tmp_path, make_invoice):
    invs = [make_invoice() for _ in range(4)]
    p = tmp_path / "eu.csv"
    synth.render_csv(invs, p, "semicolon")
    res = _extract(p)
    assert len(res) == 4
    for r, g in zip(res, invs):
        assert r.total == g["total"] and r.subtotal == g["subtotal"]
        assert r.invoice_date == g["invoice_date"]


def test_key_value_sheet_falls_back_to_text_rules(tmp_path):
    df = pd.DataFrame([["Invoice Number", "KV-001"], ["Invoice Date", "2025-02-03"], ["Supplier", "Acme Ltd"],
                       ["Customer", "Contoso"], ["Subtotal", 100], ["Tax", 20], ["Total", 120], ["Currency", "EUR"]])
    p = tmp_path / "kv.xlsx"
    df.to_excel(p, header=False, index=False)
    res = _extract(p)
    assert len(res) == 1
    r = res[0]
    assert (r.invoice_number, r.supplier_name, r.customer_name, r.total, r.currency) == ("KV-001", "Acme Ltd", "Contoso", 120.0, "EUR")


def test_non_numeric_amount_is_flagged_not_guessed(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("Invoice No,Supplier,Customer,Total\nB-1,Acme,Contoso,TBD\n", encoding="utf-8")
    r = _extract(p)[0]
    assert r.total is None and "total" in r.uncertain_fields
