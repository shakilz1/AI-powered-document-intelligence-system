"""Task 2 — LLM extraction with Claude (Anthropic Messages API, structured outputs), Google Gemini
(free tier, REST generateContent with a JSON schema) or a local model served by Ollama (e.g. qwen3.5).

The model receives only the document text and must return JSON matching INVOICE_SCHEMA.
Every field is nullable so the model can say "not present" instead of inventing a value,
and it must list fields it is unsure about. Results are afterwards *grounded* against the
source text (see extractor.py) so a value that does not appear in the document is flagged.
"""
from __future__ import annotations

import base64
import json
import logging
import time

from ..config import settings
from .types import ExtractedInvoice

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You extract invoice data from business documents: text produced by PDF parsing or OCR, or
images of the document (scans, phone photos, handwritten bills, delivery/received forms).

Rules:
- Extract ONLY values that are explicitly present in the document. Never guess, compute or invent a value.
  If a field is not present, return null. Do not calculate a missing subtotal, tax or total.
- invoice_date: convert to ISO format YYYY-MM-DD. If day/month order is ambiguous (e.g. 03/04/2024) and
  nothing in the document disambiguates it, still convert using the most likely order but list
  "invoice_date" in uncertain_fields.
- Amounts: plain numbers without currency symbols or thousands separators (1,234.50 -> 1234.5).
  "tax" is the total tax amount (VAT/GST/sales tax), not the rate.
- currency: ISO 4217 code (USD, EUR, GBP, INR, BDT, ...). A bare "$" with no other hint -> "USD" and list
  "currency" in uncertain_fields.
- supplier_name is the party issuing the invoice (seller/vendor); customer_name is the party billed.
- line_items: one entry per line of the item table, with description, quantity, unit_price, discount_pct
  (a per-line discount percentage such as "13%" -> 13), amount (null for any column that is absent).
  quantity is the number only ("26 Pcs" -> 26). Do not include subtotal/tax/total rows as line items.
- Handwriting: read it carefully. If a handwritten value is illegible, return null (or your best reading)
  and list the field in uncertain_fields; mention unreadable line items in notes.
- "Tk", "Taka" or "৳" means BDT.
- OCR text may contain recognition errors (O vs 0, l vs 1). Correct a character only when the context makes
  the intended value certain; otherwise keep the value and mark the field uncertain.
- uncertain_fields: names of header fields whose value you are not confident about (illegible, conflicting,
  ambiguous). notes: short remarks about problems you noticed (e.g. "two different totals printed")."""

_num = {"type": ["number", "null"]}
_str = {"type": ["string", "null"]}

INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": _str,
        "invoice_date": _str,
        "supplier_name": _str,
        "customer_name": _str,
        "subtotal": _num,
        "tax": _num,
        "total": _num,
        "currency": _str,
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"description": _str, "quantity": _num, "unit_price": _num, "discount_pct": _num, "amount": _num},
                "required": ["description", "quantity", "unit_price", "discount_pct", "amount"],
                "additionalProperties": False,
            },
        },
        "uncertain_fields": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total",
                 "currency", "line_items", "uncertain_fields", "notes"],
    "additionalProperties": False,
}


class LLMError(RuntimeError):
    pass


_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(max_retries=3, timeout=120)
    return _client


def _call(content):
    """content: a string (document text) or a list of content blocks (images + text)."""
    import anthropic

    if isinstance(content, str):
        content = f"<document>\n{content}\n</document>\n\nExtract the invoice."
    client = _get_client()
    common = dict(
        model=settings.llm_model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": settings.llm_effort, "format": {"type": "json_schema", "schema": INVOICE_SCHEMA}},
    )
    try:
        # Server-side refusal fallback (routes to a fallback model if a safety classifier declines).
        return client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **common)
    except TypeError:  # older SDK without the `fallbacks` parameter
        return client.messages.create(**common)
    except anthropic.BadRequestError as exc:
        if "fallback" in str(exc).lower():
            return client.messages.create(**common)
        raise


def _anthropic_text(content) -> str:
    import anthropic

    try:
        resp = _call(content)
    except anthropic.AuthenticationError as exc:
        raise LLMError("LLM authentication failed: check ANTHROPIC_API_KEY") from exc
    except anthropic.RateLimitError as exc:
        raise LLMError(f"LLM rate limited: {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(f"LLM API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError(f"LLM connection error: {exc}") from exc

    if resp.stop_reason == "refusal":
        raise LLMError("LLM declined the request")
    if resp.stop_reason == "max_tokens":
        raise LLMError("LLM output truncated (max_tokens)")
    txt = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
    if not txt:
        raise LLMError("LLM returned no text block")
    return txt


# ------------------------------------------------------------------ Google Gemini (free tier)

GEMINI_RETRY_WAITS = (3, 8, 20)  # seconds
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _gemini_post(model: str, body: dict):
    import httpx

    try:
        return httpx.post(GEMINI_URL.format(model=model), json=body, timeout=180,
                          headers={"x-goog-api-key": settings.gemini_key() or ""})
    except httpx.HTTPError as exc:
        raise LLMError(f"Gemini connection error: {exc}") from exc


def _gemini_error(r) -> str:
    try:
        return r.json().get("error", {}).get("message", "")[:300]
    except ValueError:
        return r.text[:300]


def _gemini_call(parts: list[dict]) -> tuple[str, str]:
    """Returns (json text, model used). Falls back to the lite model when the main one is out of
    free quota (429) or not available (404); drops the schema if the API version rejects it."""
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": INVOICE_SCHEMA,
                             "maxOutputTokens": 16000},
    }
    models = [settings.gemini_model] + ([settings.gemini_fallback_model] if settings.gemini_fallback_model else [])
    last = ""
    for model in models:
        r = _gemini_post(model, body)
        for wait in GEMINI_RETRY_WAITS:  # free tier often answers 503 "high demand": back off and retry
            if r.status_code not in (429, 500, 503):
                break
            log.warning("Gemini %s: HTTP %s, retrying in %ss", model, r.status_code, wait)
            time.sleep(wait)
            r = _gemini_post(model, body)
        if r.status_code == 400 and "responseJsonSchema" in _gemini_error(r):
            # Older API surface: plain JSON mode, schema described in the prompt instead.
            body["generationConfig"].pop("responseJsonSchema")
            parts.append({"text": "Return JSON matching this schema:\n" + json.dumps(INVOICE_SCHEMA)})
            r = _gemini_post(model, body)
        if r.status_code in (429, 404, 500, 503):
            last = f"Gemini {model}: HTTP {r.status_code} {_gemini_error(r)}"
            log.warning("%s; trying next model", last)
            continue
        if r.status_code in (400, 401, 403) and any(k in _gemini_error(r).lower() for k in ("api key", "api_key", "permission")):
            raise LLMError("LLM authentication failed: check GEMINI_API_KEY")
        if r.status_code != 200:
            raise LLMError(f"Gemini API error {r.status_code}: {_gemini_error(r)}")
        data = r.json()
        if data.get("promptFeedback", {}).get("blockReason"):
            raise LLMError(f"Gemini blocked the request: {data['promptFeedback']['blockReason']}")
        cands = data.get("candidates") or []
        if not cands:
            raise LLMError("Gemini returned no candidates")
        reason = cands[0].get("finishReason")
        if reason == "MAX_TOKENS":
            raise LLMError("LLM output truncated (max_tokens)")
        txt = "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []) if not p.get("thought"))
        if not txt.strip():
            raise LLMError(f"Gemini returned no text (finishReason={reason})")
        return txt, model
    raise LLMError(f"LLM rate limited / unavailable: {last}")


def _gemini_parts(content) -> list[dict]:
    if isinstance(content, str):
        return [{"text": f"<document>\n{content}\n</document>\n\nExtract the invoice."}]
    parts = []
    for b in content:
        if b["type"] == "image":
            parts.append({"inlineData": {"mimeType": b["source"]["media_type"], "data": b["source"]["data"]}})
        else:
            parts.append({"text": b["text"]})
    return parts


# ------------------------------------------------------------------ Ollama (local model, e.g. qwen3.5)

def _ollama_post(body: dict):
    import httpx

    try:
        return httpx.post(f"{settings.ollama_url}/api/chat", json=body, timeout=settings.ollama_timeout)
    except httpx.TimeoutException as exc:
        raise LLMError(f"Ollama timed out after {settings.ollama_timeout:.0f}s (raise OLLAMA_TIMEOUT)") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"Ollama not reachable at {settings.ollama_url}: is Ollama running? ({exc})") from exc


def _ollama_call(content) -> tuple[str, str]:
    """Returns (json text, model). Uses Ollama's structured outputs (`format` = JSON schema), so the
    reply is constrained to INVOICE_SCHEMA. Images go in the message's `images` list (base64)."""
    model = settings.ollama_model
    if isinstance(content, str):
        user = {"role": "user", "content": f"<document>\n{content}\n</document>\n\nExtract the invoice."}
    else:
        texts, images = [], []
        for b in content:
            if b["type"] == "image":
                images.append(b["source"]["data"])
            else:
                texts.append(b["text"])
        user = {"role": "user", "content": "\n".join(texts), "images": images}
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, user],
        "format": INVOICE_SCHEMA,
        "stream": False,
        "think": settings.ollama_think,
        "keep_alive": "30m",
        "options": {"temperature": 0, "num_ctx": settings.ollama_num_ctx, "num_predict": 8000},
    }
    r = _ollama_post(body)
    if r.status_code == 400 and "think" in r.text.lower():  # model without a thinking switch
        body.pop("think")
        r = _ollama_post(body)
    if r.status_code == 404:
        raise LLMError(f"Ollama model '{model}' not found: run `ollama pull {model}`")
    if r.status_code != 200:
        try:
            err = r.json().get("error", "")
        except ValueError:
            err = r.text
        raise LLMError(f"Ollama error {r.status_code}: {str(err)[:300]}")
    data = r.json()
    if data.get("done_reason") == "length":
        raise LLMError("LLM output truncated (max_tokens)")
    txt = (data.get("message") or {}).get("content") or ""
    if not txt.strip():
        raise LLMError("Ollama returned no text")
    return txt, model


# ------------------------------------------------------------------ shared

def _parse_json(txt: str) -> dict:
    t = txt.strip()
    if t.startswith("```"):  # tolerate fenced output in plain-JSON mode
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    try:
        data = json.loads(t)
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("LLM returned JSON that is not an object")
    return data


def _run(content, source_text: str, method: str, provider: str | None) -> ExtractedInvoice:
    """content: document text, or a list of Anthropic-style content blocks (images + text)."""
    t0 = time.perf_counter()
    if provider == "gemini":
        txt, model = _gemini_call(_gemini_parts(content))
    elif provider == "ollama":
        txt, model = _ollama_call(content)
    else:
        txt, model = _anthropic_text(content), settings.llm_model
    data = _parse_json(txt)

    inv = ExtractedInvoice(method=method, source_text=source_text)
    for f in ("invoice_number", "invoice_date", "supplier_name", "customer_name", "currency"):
        v = data.get(f)
        setattr(inv, f, v.strip() if isinstance(v, str) and v.strip() else None)
    for f in ("subtotal", "tax", "total"):
        v = data.get(f)
        setattr(inv, f, round(float(v), 2) if isinstance(v, (int, float)) and not isinstance(v, bool) else None)
    inv.line_items = [
        {k: li.get(k) for k in ("description", "quantity", "unit_price", "discount_pct", "amount")}
        for li in data.get("line_items") or [] if isinstance(li, dict)
    ]
    inv.uncertain_fields = [f for f in data.get("uncertain_fields") or [] if isinstance(f, str)]
    inv.notes = [n for n in data.get("notes") or [] if isinstance(n, str)]
    inv.notes.append(f"{method} {model} {int((time.perf_counter() - t0) * 1000)}ms")
    return inv


def model_from_notes(notes) -> str | None:
    """The model that produced an invoice, from the "<method> <model> <ms>ms" note _run appends."""
    for n in reversed(notes or []):
        if isinstance(n, str):
            parts = n.split()
            if len(parts) == 3 and parts[0] in ("llm", "llm-vision") and parts[2].endswith("ms"):
                return parts[1]
    return None


def extract_llm(text: str) -> ExtractedInvoice:
    """Text-only extraction (digital PDFs, spreadsheets turned into text). Raises LLMError on failure."""
    if len(text) > settings.llm_max_chars:
        # Not silently truncated: caller falls back to rules and records why.
        raise LLMError(f"Segment too long for LLM extraction ({len(text)} chars > {settings.llm_max_chars})")
    return _run(text, text, "llm", settings.provider)


def extract_llm_images(images: list[bytes], ocr_text: str) -> ExtractedInvoice:
    """Vision extraction: the LLM (Claude or Gemini) reads the page images directly (photos, scans, handwriting).
    `images` are JPEG bytes (one per page). The local OCR text is passed as a hint only."""
    if not images:
        raise LLMError("No page images")
    if len(images) > settings.llm_max_images:
        raise LLMError(f"Too many pages for one vision call ({len(images)} > {settings.llm_max_images})")
    blocks: list[dict] = []
    for i, img in enumerate(images, 1):
        if len(images) > 1:
            blocks.append({"type": "text", "text": f"Page {i}:"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                   "data": base64.b64encode(img).decode("ascii")}})
    blocks.append({"type": "text", "text": (
        "The image(s) above are one invoice / bill (may be a phone photo, a scan, or handwritten). "
        "Read the values from the image itself. Machine OCR of the same page is given below only as a hint; "
        "it is often wrong for handwriting.\n<ocr_hint>\n" + ocr_text[:20000] + "\n</ocr_hint>\n\nExtract the invoice.")})
    return _run(blocks, ocr_text, "llm-vision", settings.vision_provider)
