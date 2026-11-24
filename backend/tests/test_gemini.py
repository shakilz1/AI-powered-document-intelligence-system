"""Gemini provider (free alternative to Claude) — HTTP mocked, no network."""
import base64
import json

import pytest

from app.config import settings
from app.pipeline import extractor, llm
from app.pipeline.loaders import LoadedDocument, PageText

OUT = {"invoice_number": "5029", "invoice_date": "2026-08-12", "supplier_name": "Burak Electric Co",
       "customer_name": "Electro Mech", "subtotal": None, "tax": None, "total": 2700.0, "currency": "BDT",
       "line_items": [{"description": "Push button", "quantity": 3, "unit_price": 900, "discount_pct": None, "amount": 2700}],
       "uncertain_fields": ["customer_name"], "notes": ["handwritten"]}


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.text = json.dumps(data)

    def json(self):
        return self._data


def ok(data=OUT):
    return Resp(200, {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(data)}]}}]})


@pytest.fixture
def gemini_on(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setattr(settings, "llm_vision", True)
    monkeypatch.setattr(settings, "llm_provider", "auto")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    calls = []

    def install(*responses):
        it = iter(responses)

        def fake(model, body):
            calls.append((model, json.loads(json.dumps(body))))
            return next(it)
        monkeypatch.setattr(llm, "_gemini_post", fake)
    monkeypatch.setattr(llm, "GEMINI_RETRY_WAITS", ())  # no sleeping in tests
    return install, calls


def test_provider_selection(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setattr(settings, "llm_provider", "auto")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)  # a local model in .env would be picked by auto
    assert settings.provider is None and not settings.llm_available
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert settings.provider == "gemini" and settings.llm_available
    assert not settings.text_llm_available  # free tier: images only by default
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert settings.provider == "anthropic"  # Claude preferred when both are set
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    assert settings.provider == "gemini"


def test_gemini_vision_request_and_mapping(gemini_on):
    install, calls = gemini_on
    install(ok())
    doc = LoadedDocument("image", pages=[PageText(1, "ocr", "garbled ocr", 0.5, b"\xff\xd8jpegbytes")])
    (inv,) = extractor.extract_document(doc)
    assert inv.method == "llm-vision" and inv.total == 2700.0 and inv.line_items[0]["quantity"] == 3
    assert "customer_name" in inv.uncertain_fields
    model, body = calls[0]
    assert model == settings.gemini_model
    parts = body["contents"][0]["parts"]
    assert parts[0]["inlineData"] == {"mimeType": "image/jpeg", "data": base64.b64encode(b"\xff\xd8jpegbytes").decode()}
    assert "garbled ocr" in parts[-1]["text"]
    assert body["generationConfig"]["responseJsonSchema"] == llm.INVOICE_SCHEMA
    assert body["systemInstruction"]["parts"][0]["text"] == llm.SYSTEM_PROMPT


def test_gemini_quota_falls_back_to_lite_model(gemini_on):
    install, calls = gemini_on
    install(Resp(429, {"error": {"message": "quota exceeded"}}), ok())
    inv = llm.extract_llm_images([b"\xff\xd8x"], "")
    assert [c[0] for c in calls] == [settings.gemini_model, settings.gemini_fallback_model]
    assert settings.gemini_fallback_model in inv.notes[-1]


def test_gemini_schema_rejected_retries_plain_json(gemini_on):
    install, calls = gemini_on
    fenced = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "```json\n" + json.dumps(OUT) + "\n```"}]}}]}
    install(Resp(400, {"error": {"message": "Unknown name \"responseJsonSchema\""}}), Resp(200, fenced))
    inv = llm.extract_llm_images([b"\xff\xd8x"], "")
    assert inv.total == 2700.0
    assert "responseJsonSchema" not in calls[1][1]["generationConfig"]


@pytest.mark.parametrize("resp,msg", [
    (Resp(400, {"error": {"message": "API key not valid. Please pass a valid API key."}}), "GEMINI_API_KEY"),
    (Resp(200, {"promptFeedback": {"blockReason": "SAFETY"}}), "blocked"),
    (Resp(200, {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]}), "truncated"),
    (Resp(502, {"error": {"message": "bad gateway"}}), "502"),
])
def test_gemini_errors_raise_llmerror(gemini_on, resp, msg):
    install, _ = gemini_on
    install(resp)
    with pytest.raises(llm.LLMError, match=msg):
        llm.extract_llm_images([b"\xff\xd8x"], "")


def test_gemini_failure_falls_back_to_rules(gemini_on):
    install, _ = gemini_on
    install(Resp(429, {"error": {"message": "quota"}}), Resp(429, {"error": {"message": "quota"}}))
    doc = LoadedDocument("image", pages=[PageText(1, "ocr", "Invoice No: A-1\nTotal: 100.00 USD", 0.9, b"\xff\xd8x")])
    (inv,) = extractor.extract_document(doc)
    assert inv.method == "rules" and inv.total == 100.0
    assert any("Vision extraction unavailable" in n for n in inv.notes)


def test_typed_text_not_sent_to_gemini_by_default(gemini_on):
    install, calls = gemini_on
    install()  # any call would raise StopIteration
    inv = extractor.extract_text_segment("Invoice No: A-1\nTotal: 100.00 USD", None)
    assert inv.method == "rules" and calls == []


def test_gemini_retries_busy_model_before_fallback(gemini_on, monkeypatch):
    install, calls = gemini_on
    monkeypatch.setattr(llm, "GEMINI_RETRY_WAITS", (0, 0))
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    busy = Resp(503, {"error": {"message": "high demand"}})
    install(busy, busy, ok())
    inv = llm.extract_llm_images([b"jpeg"], "")
    assert inv.total == 2700.0 and [c[0] for c in calls] == [settings.gemini_model] * 3
