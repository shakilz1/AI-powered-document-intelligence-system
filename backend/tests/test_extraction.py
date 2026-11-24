"""Task 2: AI information extraction (rules, LLM path with mocked Claude, grounding, fallbacks)."""
import json
from types import SimpleNamespace

import pytest

from app.config import settings
from app.pipeline import extractor, llm
from app.pipeline.loaders import PageText, load_document
from app.pipeline.rules import extract_rules
from eval import synth

SAMPLE = """Globex Corporation   INVOICE
Invoice No: GX-2025-0042
Invoice Date: 2025-03-14
Due Date: 2025-04-13
Bill To:
Contoso Retail Group
Description   Qty   Unit Price   Amount
Widget large   2   50.00   100.00
Service fee   1   25.50   25.50
Subtotal:   125.50
VAT (20%):   25.10
Total (EUR):   150.60
"""


def test_rules_extract_all_fields():
    r = extract_rules(SAMPLE)
    assert r.invoice_number == "GX-2025-0042"
    assert r.invoice_date == "2025-03-14"  # not the due date
    assert r.supplier_name == "Globex Corporation"
    assert r.customer_name == "Contoso Retail Group"
    assert (r.subtotal, r.tax, r.total, r.currency) == (125.5, 25.1, 150.6, "EUR")
    assert r.line_items == [
        {"description": "Widget large", "quantity": 2.0, "unit_price": 50.0, "discount_pct": None, "amount": 100.0},
        {"description": "Service fee", "quantity": 1.0, "unit_price": 25.5, "discount_pct": None, "amount": 25.5},
    ]


def test_rules_never_invent_missing_values():
    r = extract_rules("Some letter\nInvoice No: X-77\nThank you")
    assert r.invoice_number == "X-77"
    for f in ("invoice_date", "customer_name", "subtotal", "tax", "total", "currency"):
        assert getattr(r, f) is None, f
        assert r.confidence[f] == 0.0


def test_rules_ocr_text_without_spaces():
    text = "TAX INVOICE\nFrom:   Bill To:\nPioneerFoodsInc   ContosoRetailGroup\nInvoiceNumber:INV-2025-00051   Date:January31,2025\nSubtotal:   $2,827.44\nSalesTax(10%):   $282.74\nTotal(USD):   $3,110.18"
    r = extract_rules(text, ocr_confidence=0.95)
    assert r.invoice_number == "INV-2025-00051"
    assert r.invoice_date == "2025-01-31"
    assert r.supplier_name == "Pioneer Foods Inc" and r.customer_name == "Contoso Retail Group"
    assert (r.subtotal, r.tax, r.total) == (2827.44, 282.74, 3110.18)


def test_ambiguous_date_marked_uncertain():
    r = extract_rules("Invoice No: A-1\nInvoice Date: 03/04/2025\nTotal: 10.00")
    assert "invoice_date" in r.uncertain_fields


@pytest.mark.parametrize("layout", ["A", "B", "C"])
def test_pdf_layouts_end_to_end(tmp_path, rng, make_invoice, layout):
    inv = make_invoice()
    p = tmp_path / "x.pdf"
    synth.render_pdf([inv], p, rng, [layout])
    (r,) = extractor.extract_document(load_document(p))
    for f in ("invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"):
        assert getattr(r, f) == inv[f], f
    assert len(r.line_items) == len(inv["line_items"])


def test_segmentation_splits_invoices_and_keeps_continuation_pages():
    pages = [
        PageText(1, "text", "Invoice No: A-001\nTotal: 10.00"),
        PageText(2, "text", "continued items...\nPage 2 of 2"),
        PageText(3, "text", "Invoice No: B-002\nTotal: 20.00"),
        PageText(4, "text", ""),
    ]
    segs = extractor.segment_pages(pages)
    assert [[p.page_number for p in s] for s in segs] == [[1, 2], [3, 4]]


def test_segmentation_splits_unlabeled_bills():
    """Scanned bundle of local bills: numbers have no 'Invoice No' label, but each page is a complete
    document (own BILL/CHALLAN title, own closing total, often 'Page 1 of 1')."""
    bill = "SQUARE Electric Co.\nBILL\nDate:{d}\n{n}\nItem Qty. Unit Unit Price Sub Total\nCT 2.00 SET 37,000.00 74000.00\nTotal   {t}\n"
    pages = [
        PageText(1, "ocr", bill.format(d="02-04-2026", n="38111", t="87000.00")),                       # no "Page 1 of 1"
        PageText(2, "ocr", bill.format(d="30-03-2026", n="38044", t="74000.00") + "Page 1 of 1"),
        PageText(3, "ocr", bill.format(d="11-01-2026", n="36967", t="52500.00") + "Page 1 of 1"),
        PageText(4, "ocr", bill.format(d="28-03-2026", n="38022", t="250000.00")),
        PageText(5, "ocr", "SQUARE Electric Co.\nCHALLAN\nDate: 07-04-2026\n38180\nItem Quantity Unit\nRELAY PCS\nPage 1 of 1"),
    ]
    assert [[p.page_number for p in s] for s in extractor.segment_pages(pages)] == [[1], [2], [3], [4], [5]]


def test_segmentation_keeps_multipage_invoice_together():
    """Repeated header on page 2 but the total only on the last page: one invoice, not two."""
    pages = [
        PageText(1, "text", "TAX INVOICE\nAcme Ltd\nItem Qty Unit Price Sub Total\nWidget 1 10.00 10.00"),
        PageText(2, "text", "TAX INVOICE\nAcme Ltd\nItem Qty Unit Price Sub Total\nGadget 1 5.00 5.00\nTotal 15.00"),
        PageText(3, "text", "INVOICE\nOther Co\nTotal 99.00\nPage 2 of 2"),  # continuation wins over the title
    ]
    assert [[p.page_number for p in s] for s in extractor.segment_pages(pages)] == [[1, 2, 3]]


def test_scanned_invoice_extraction(tmp_path, rng, make_invoice):
    inv = make_invoice()
    p = tmp_path / "s.pdf"
    synth.render_scanned_pdf([inv], p, rng, "clean")
    (r,) = extractor.extract_document(load_document(p))
    assert r.invoice_number == inv["invoice_number"] and r.total == inv["total"]


# ---------------------------------------------------------------- LLM path (Claude mocked)

def _fake_response(payload: dict, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=json.dumps(payload))])


LLM_OK = {"invoice_number": "GX-2025-0042", "invoice_date": "2025-03-14", "supplier_name": "Globex Corporation",
          "customer_name": "Contoso Retail Group", "subtotal": 125.5, "tax": 25.1, "total": 150.6, "currency": "EUR",
          "line_items": [{"description": "Widget large", "quantity": 2, "unit_price": 50, "amount": 100}],
          "uncertain_fields": [], "notes": []}


@pytest.fixture
def llm_on(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def test_llm_result_mapped_and_confirmed_by_rules(monkeypatch, llm_on):
    monkeypatch.setattr(llm, "_call", lambda text: _fake_response(LLM_OK))
    r = extractor.extract_text_segment(SAMPLE, None)
    assert r.method == "llm" and r.total == 150.6 and r.uncertain_fields == []
    assert r.confidence["total"] > 0.9


def test_llm_hallucination_is_flagged_by_grounding(monkeypatch, llm_on):
    bad = dict(LLM_OK, total=999.99, customer_name="Imaginary Customer Plc")
    monkeypatch.setattr(llm, "_call", lambda text: _fake_response(bad))
    r = extractor.extract_text_segment(SAMPLE, None)
    assert "total" in r.uncertain_fields and "customer_name" in r.uncertain_fields
    assert r.confidence["total"] <= 0.6


def test_llm_failure_falls_back_to_rules(monkeypatch, llm_on):
    def boom(text):
        raise llm.LLMError("LLM connection error: timeout")

    monkeypatch.setattr(llm, "extract_llm", boom)
    r = extractor.extract_text_segment(SAMPLE, None)
    assert r.method == "rules" and r.total == 150.6
    assert any("fallback" in n for n in r.notes)


@pytest.mark.parametrize("resp,msg", [
    (_fake_response(LLM_OK, stop="refusal"), "declined"),
    (_fake_response(LLM_OK, stop="max_tokens"), "truncated"),
    (SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="not json")]), "invalid JSON"),
])
def test_llm_bad_responses_raise(monkeypatch, llm_on, resp, msg):
    monkeypatch.setattr(llm, "_call", lambda text: resp)
    with pytest.raises(llm.LLMError, match=msg):
        llm.extract_llm(SAMPLE)


def test_llm_refuses_oversized_segment_instead_of_truncating(monkeypatch, llm_on):
    monkeypatch.setattr(settings, "llm_max_chars", 100)
    with pytest.raises(llm.LLMError, match="too long"):
        llm.extract_llm("x" * 101)


def test_llm_schema_is_strict_and_nullable():
    s = llm.INVOICE_SCHEMA
    assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
    assert "null" in s["properties"]["total"]["type"]
