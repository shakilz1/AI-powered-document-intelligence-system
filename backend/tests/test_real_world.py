"""Real-world layouts (based on OCR output of actual Bangladeshi invoices) and the Claude vision path."""
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from app.config import settings
from app.pipeline import extractor, llm
from app.pipeline.loaders import PageText, load_document
from app.pipeline.normalize import detect_currency, parse_date
from app.pipeline.rules import extract_rules, parse_item_row
from app.pipeline.validation import validate_invoice
from eval import synth

# OCR text of a real printed invoice (company names kept, layout as RapidOCR produced it)
PRINTED = """AUTOMATIONENGINEERING&CONTROLSLTD.
RntNo.   eNlo   260901   Dated   1-Sop-26
Nawabpur Sales Center
INVOICE
Paty   ElectromechAutomation&EngineeringLtd.
Descriptioaof Goods   Quantity   Rate   per   Disc.%   Amount
SIGNAL LAMP-X-ER   26Pcs   881.00   Pcs   13%   19,928.22
TYPE:DR22D0L-Q4R(RED),
220V
SIGNAL LAMP.X-ER   11Pcs   881.00   Pcs   13%   8,431.17
SIGNALLAMP-X-ER   11Pcs   929.00   Pcs   13%   8,890.53
Electro Mech Automation &   In Partnership with the Future   Total   48Pc5   Tk37,249.92
"""


def test_printed_real_invoice_line_items_with_units_and_discount():
    r = extract_rules(PRINTED, ocr_confidence=0.9)
    assert r.total == 37249.92 and r.currency == "BDT" and r.invoice_date == "2026-09-01"
    assert [(li["quantity"], li["unit_price"], li["discount_pct"], li["amount"]) for li in r.line_items] == [
        (26.0, 881.0, 13.0, 19928.22), (11.0, 881.0, 13.0, 8431.17), (11.0, 929.0, 13.0, 8890.53)]
    codes = {i.code for i in validate_invoice(r.to_dict())}
    assert "line_item_math" not in codes and "line_items_mismatch" not in codes  # discount understood


@pytest.mark.parametrize("row,expected", [
    ("Widget   2   50.00   100.00", ("Widget", 2.0, 50.0, None, 100.0)),
    ("Cable 4 core   10 Pcs   120.00   1,200.00", ("Cable 4 core", 10.0, 120.0, None, 1200.0)),
    ("Steel bolts M8 (box of 100)   17   357.05   6,069.85", ("Steel bolts M8 (box of 100)", 17.0, 357.05, None, 6069.85)),
    ("Breaker   5Nos   Tk900   10%   Tk4,050", ("Breaker", 5.0, 900.0, 10.0, 4050.0)),
])
def test_parse_item_row(row, expected):
    r = parse_item_row(row)
    assert (r["description"], r["quantity"], r["unit_price"], r["discount_pct"], r["amount"]) == expected


def test_parse_item_row_rejects_non_rows():
    assert parse_item_row("TYPE:DR22D0L-Q4R(RED),") is None
    assert parse_item_row("220V") is None


def test_dates_currency_and_letterhead_fixes():
    assert parse_date("1-Sop-26")[0] == "2026-09-01"  # OCR-misread month
    assert parse_date("Date926") == (None, False)  # garbage year rejected
    assert detect_currency("Total Tk37,249.92")[0] == "BDT"
    r = extract_rules("Date：12 August 2025\nbec   Burak Electric Co   +8801751112449\nBill No.   5029\n")
    assert r.supplier_name == "Burak Electric Co"  # logo fragment "bec" skipped
    assert r.invoice_date == "2025-08-12"  # full-width colon normalised


def test_discount_math_validation():
    inv = {"invoice_number": "A", "invoice_date": "2025-01-01", "supplier_name": "S", "customer_name": "C",
           "total": 87.0, "currency": "BDT", "subtotal": 87.0, "tax": 0.0, "confidence": {}, "uncertain_fields": [],
           "line_items": [{"description": "x", "quantity": 1, "unit_price": 100.0, "discount_pct": 13, "amount": 87.0}]}
    assert not any(i.code == "line_item_math" for i in validate_invoice(inv))
    inv["line_items"][0]["amount"] = 100.0
    assert any(i.code == "line_item_math" for i in validate_invoice(inv))


# ---------------------------------------------------------------- vision (Claude mocked)

@pytest.fixture
def llm_on(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setattr(settings, "llm_vision", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


VISION_OUT = {"invoice_number": "5029", "invoice_date": "2026-08-12", "supplier_name": "Burak Electric Co",
              "customer_name": "Electro Mech Automation & Engineering Ltd", "subtotal": None, "tax": None,
              "total": 2700.0, "currency": "BDT",
              "line_items": [{"description": "Push button", "quantity": 3, "unit_price": 900, "discount_pct": None, "amount": 2700}],
              "uncertain_fields": ["invoice_date"], "notes": ["handwritten"]}


def test_images_kept_and_sent_to_claude_vision(tmp_path, rng, make_invoice, monkeypatch, llm_on):
    p = tmp_path / "photo.jpg"
    synth.render_image(make_invoice(), p, rng, "clean")
    doc = load_document(p)
    assert doc.pages[0].image and doc.pages[0].image[:2] == b"\xff\xd8"  # JPEG kept for vision
    assert max(Image.open(io.BytesIO(doc.pages[0].image)).size) <= 1600  # downscaled

    sent = {}

    def fake_call(content):
        sent["content"] = content
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(VISION_OUT))])

    monkeypatch.setattr(llm, "_call", fake_call)
    (inv,) = extractor.extract_document(doc)
    assert inv.method == "llm-vision"
    assert [b["type"] for b in sent["content"]] == ["image", "text"]
    assert sent["content"][0]["source"]["media_type"] == "image/jpeg"
    # handwriting values are not in the OCR text, but vision results are not "ungrounded"-penalised
    assert inv.total == 2700.0 and inv.line_items[0]["quantity"] == 3
    assert "invoice_date" in inv.uncertain_fields  # model's own uncertainty is kept


def test_vision_failure_falls_back(monkeypatch, llm_on):
    def boom(images, text):
        raise llm.LLMError("LLM authentication failed: check ANTHROPIC_API_KEY")

    monkeypatch.setattr(llm, "extract_llm_images", boom)
    monkeypatch.setattr(llm, "extract_llm", lambda text: (_ for _ in ()).throw(llm.LLMError("no key")))
    from app.pipeline.loaders import LoadedDocument

    doc = LoadedDocument("image", pages=[PageText(1, "ocr", PRINTED, 0.9, b"\xff\xd8fake")])
    (inv,) = extractor.extract_document(doc)
    assert inv.method == "rules" and inv.total == 37249.92
    assert any("Vision extraction unavailable" in n for n in inv.notes)


def test_no_images_kept_without_api_key(tmp_path, rng, make_invoice):
    p = tmp_path / "photo.png"
    synth.render_image(make_invoice(), p, rng, "clean")
    assert load_document(p).pages[0].image is None  # nothing retained when vision can't be used
