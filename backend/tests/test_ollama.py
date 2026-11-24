"""Ollama provider (local model, e.g. qwen3.5) — HTTP mocked, no network."""
import base64
import json

import httpx
import pytest

from app.config import settings
from app.pipeline import extractor, llm
from app.pipeline.loaders import LoadedDocument, PageText

OUT = {"invoice_number": "INV-7", "invoice_date": "2026-03-14", "supplier_name": "Northwind Traders",
       "customer_name": "Contoso", "subtotal": 125.0, "tax": 18.75, "total": 143.75, "currency": "BDT",
       "line_items": [{"description": "Widget", "quantity": 5, "unit_price": 25, "discount_pct": None, "amount": 125}],
       "uncertain_fields": [], "notes": []}


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.text = json.dumps(data)

    def json(self):
        return self._data


def ok(data=OUT, done_reason="stop"):
    return Resp(200, {"message": {"role": "assistant", "content": json.dumps(data)}, "done_reason": done_reason})


@pytest.fixture
def ollama_on(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setattr(settings, "llm_vision", True)
    monkeypatch.setattr(settings, "llm_provider", "ollama")
    monkeypatch.setattr(settings, "ollama_model", "qwen3.5:4b")
    monkeypatch.setattr(settings, "ollama_text", True)
    monkeypatch.setattr(settings, "vision_provider_setting", "")  # ignore a hybrid setting in the user's .env
    calls = []

    def install(*responses):
        it = iter(responses)

        def fake(body):
            calls.append(json.loads(json.dumps(body)))
            r = next(it)
            if isinstance(r, Exception):
                raise r
            return r
        monkeypatch.setattr(llm, "_ollama_post", fake)
    return install, calls


def test_provider_selection(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setattr(settings, "llm_provider", "ollama")
    assert settings.provider == "ollama"  # explicit choice wins over a cloud key
    assert settings.text_llm_available and settings.active_model == settings.ollama_model
    monkeypatch.setattr(settings, "llm_provider", "auto")
    assert settings.provider == "gemini"  # auto prefers a configured cloud key
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    assert settings.provider is None  # auto never picks the slow local model unless OLLAMA_MODEL is set
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3.5:4b")
    assert settings.provider == "ollama"


def test_text_request_uses_schema_and_maps_fields(ollama_on):
    install, calls = ollama_on
    install(ok())
    inv = llm.extract_llm("Invoice INV-7 ... Total Tk 143.75")
    body = calls[0]
    assert body["model"] == "qwen3.5:4b" and body["stream"] is False
    assert body["format"] == llm.INVOICE_SCHEMA  # structured output constrained to the schema
    assert body["options"]["temperature"] == 0 and body["think"] is False
    assert body["messages"][0]["role"] == "system" and "INV-7" in body["messages"][1]["content"]
    assert inv.method == "llm" and inv.total == 143.75 and inv.currency == "BDT"
    assert inv.line_items[0]["amount"] == 125 and "qwen3.5:4b" in inv.notes[-1]


def test_vision_request_sends_images(ollama_on):
    install, calls = ollama_on
    install(ok())
    inv = llm.extract_llm_images([b"\xff\xd8page1"], "ocr hint text")
    user = calls[0]["messages"][1]
    assert user["images"] == [base64.b64encode(b"\xff\xd8page1").decode()]
    assert "ocr hint text" in user["content"]
    assert inv.method == "llm-vision"


def test_typed_text_goes_to_ollama(ollama_on):
    install, calls = ollama_on
    install(ok())
    inv = extractor.extract_text_segment("Invoice No: INV-7\nTotal: 143.75 BDT", None)
    assert len(calls) == 1 and inv.total == 143.75


def test_think_param_rejected_is_retried_without_it(ollama_on):
    install, calls = ollama_on
    install(Resp(400, {"error": "model does not support thinking"}), ok())
    inv = llm.extract_llm("x")
    assert "think" in calls[0] and "think" not in calls[1] and inv.total == 143.75


@pytest.mark.parametrize("resp,msg", [
    (Resp(404, {"error": "model not found"}), "ollama pull"),
    (Resp(500, {"error": "out of memory"}), "out of memory"),
    (ok(done_reason="length"), "truncated"),
    (Resp(200, {"message": {"content": ""}}), "no text"),
    (Resp(200, {"message": {"content": "not json"}}), "invalid JSON"),
])
def test_errors_raise_llmerror(ollama_on, resp, msg):
    install, _ = ollama_on
    install(resp)
    with pytest.raises(llm.LLMError, match=msg):
        llm.extract_llm("x")


def test_server_down_message(monkeypatch):
    monkeypatch.setattr(settings, "ollama_url", "http://127.0.0.1:9")

    def boom(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(llm.LLMError, match="is Ollama running"):
        llm._ollama_post({})


def test_hybrid_images_to_gemini_text_to_ollama(ollama_on, monkeypatch):
    """VISION_PROVIDER=gemini: photos go to Gemini, typed text stays on the local model."""
    install, ollama_calls = ollama_on
    install(ok())
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    monkeypatch.setattr(settings, "vision_provider_setting", "gemini")
    gemini_calls = []

    def fake_gemini(model, body):
        gemini_calls.append(model)
        g = dict(OUT, total=999.0)
        return Resp(200, {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(g)}]}}]})
    monkeypatch.setattr(llm, "_gemini_post", fake_gemini)
    assert settings.provider == "ollama" and settings.vision_provider == "gemini"
    assert settings.vision_model == settings.gemini_model
    img = llm.extract_llm_images([b"\xff\xd8x"], "")
    txt = llm.extract_llm("Invoice INV-7")
    assert img.total == 999.0 and len(gemini_calls) == 1
    assert txt.total == 143.75 and len(ollama_calls) == 1


def test_vision_provider_without_key_falls_back_to_main(ollama_on, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setattr(settings, "vision_provider_setting", "gemini")
    assert settings.vision_provider == "ollama"


def test_failure_falls_back_to_rules(ollama_on):
    install, _ = ollama_on
    install(Resp(500, {"error": "crash"}), Resp(500, {"error": "crash"}))  # vision, then text LLM
    doc = LoadedDocument("image", pages=[PageText(1, "ocr", "Invoice No: A-1\nTotal: 100.00 USD", 0.9, b"\xff\xd8x")])
    (inv,) = extractor.extract_document(doc)
    assert inv.method == "rules" and inv.total == 100.0


def test_model_from_notes():
    assert llm.model_from_notes(["Currency is BDT.", "llm-vision gemini-flash-lite-latest 64453ms"]) == "gemini-flash-lite-latest"
    assert llm.model_from_notes(["llm qwen3.5:4b 15429ms"]) == "qwen3.5:4b"
    assert llm.model_from_notes(["LLM unavailable, rule-based fallback used: x"]) is None
    assert llm.model_from_notes(None) is None


def test_local_only_switch_overrides_hybrid(ollama_on, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "uploads")
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    monkeypatch.setattr(settings, "llm_provider", "auto")
    monkeypatch.setattr(settings, "vision_provider_setting", "gemini")
    assert settings.provider == "gemini" and not settings.local_only
    settings.set_local_only(True)
    assert settings.local_only and settings.provider == "ollama" and settings.vision_provider == "ollama"
    # Another process (a worker) reading the same file sees the switch too.
    from app.config import Settings
    other = Settings()
    other.storage_dir = tmp_path / "uploads"
    other.llm_enabled, other.ollama_model = True, "qwen3.5:4b"  # test env disables the LLM by default
    assert other.local_only and other.provider == "ollama"
    settings.set_local_only(False)
    assert settings.provider == "gemini" and settings.vision_provider == "gemini"


def test_llm_settings_api(client, monkeypatch):
    from app import main
    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setattr(settings, "ollama_model", "qwen3.5:4b")
    monkeypatch.setattr(main, "_llm_settings", main._llm_settings)

    class Down:
        def __call__(self, *a, **k):
            raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx, "get", Down())
    r = client.put("/api/settings/llm", json={"local_only": True})
    assert r.status_code == 200
    body = r.json()
    assert body["local_only"] is True and body["text_provider"] == "ollama" and body["vision_provider"] == "ollama"
    assert body["ollama"] == {"running": False, "model_installed": False}
    assert client.get("/api/health").json()["local_only"] is True
    assert client.put("/api/settings/llm", json={"local_only": False}).json()["local_only"] is False
    assert client.put("/api/settings/llm", json={}).status_code == 422
