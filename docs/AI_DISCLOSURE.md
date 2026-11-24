# AI prompts and disclosure of AI-assisted development

## 1. AI used inside the product

**Models (all use the same system prompt and JSON schema):**

| Provider | Model | When |
|---|---|---|
| Anthropic Claude (official `anthropic` SDK, Messages API) | `claude-opus-5-5` by default (`LLM_MODEL`; `claude-haiku-5-5` is cheaper for volume), effort `low` | `ANTHROPIC_API_KEY` set |
| Google Gemini (REST, free tier) | `gemini-flash-latest`, falling back to `gemini-flash-lite-latest` when busy or out of quota | `GEMINI_API_KEY` set |
| Local model via Ollama | `qwen3.5:4b` by default (`OLLAMA_MODEL`), runs on this PC | `LLM_PROVIDER=ollama`, or the *Local LLM only* switch in the UI |

Photos/scans can use a different provider than typed text (`VISION_PROVIDER`, e.g. local model for
typed documents + Gemini for handwriting). Each extracted invoice records which model produced it.

**Where:** `backend/app/pipeline/llm.py`. Without any LLM configured, the deterministic rule extractor
(`rules.py`, `spreadsheet.py`) is used.

**Request shape (Ollama):** `POST /api/chat` with `format` = `INVOICE_SCHEMA` (structured output),
temperature 0, `think: false`, `num_ctx` 16384; page images go in the message's `images` list.

**Request shape (Claude):**
- `output_config.format` = JSON schema (`INVOICE_SCHEMA`): every field nullable, `additionalProperties: false`,
  so the response is guaranteed to be valid JSON of the right shape and "not present" is expressible.
- Server-side refusal fallback (`betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`),
  with automatic retry on an SDK that does not know the parameter.
- `stop_reason` is checked (`refusal`, `max_tokens`) before the content is used.
- Segments longer than `LLM_MAX_CHARS` are **not truncated**: the rule extractor is used and the reason is recorded.

### System prompt (verbatim)

```
You extract invoice data from business documents: text produced by PDF parsing or OCR, or
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
  ambiguous). notes: short remarks about problems you noticed (e.g. "two different totals printed").
```

### User message template

```
<document>
{page text of one invoice segment}
</document>

Extract the invoice.
```

### Vision message (photos, scans, handwriting)

For pages that needed OCR, the page image (JPEG, max 1600 px) goes to the vision provider as an image
(several pages: up to `LLM_MAX_IMAGES`), followed by this text:

```
The image(s) above are one invoice / bill (may be a phone photo, a scan, or handwritten). Read the values
from the image itself. Machine OCR of the same page is given below only as a hint; it is often wrong for
handwriting.
<ocr_hint>
{local OCR text}
</ocr_hint>

Extract the invoice.
```

Vision results skip the text-grounding check (handwritten values are not in the OCR text), so they get a
lower base confidence (0.88). They are still compared with confident rule values and fully validated
(line maths with discounts, totals, duplicates). If the vision call fails, the text LLM and then the rules
take over, and a note says why.

### Guard-rails around the model output (`extractor.py`)

1. **Grounding:** every returned value must be findable in the source text (amount in any common
   formatting, invoice number after normalisation, ≥60 % of name tokens). Otherwise → uncertain,
   confidence ≤ 0.4.
2. **Second opinion:** the rule extractor runs on the same text; if it found a confident value that
   disagrees, the field is marked uncertain with both values in the notes.
3. **Failure fallback:** any API error, refusal, truncation or invalid JSON → rule-based result + a note.
4. **Business rules, not model output:** a missing tax becomes 0 and a missing subtotal becomes the sum of
   the line amounts. The prompt still tells the model *not* to compute these; the system does it afterwards,
   records them in `derived_fields`, and the UI labels them.
5. **Validation** then runs on the merged result as for any other source.

### Honest status of the LLM path

- **Local model (Ollama qwen3.5:4b): measured.** Full evaluation on the synthetic set:
  100 % field accuracy on 199 invoices, ~10–16 s per page on CPU/iGPU
  (`backend/eval/results_ollama/`). On a real handwritten bill it misread several values; the validation
  rules flagged it for review.
- **Gemini: tried on real photos** (3 phone photos, including handwriting), not on the full evaluation set
  because of free-tier quotas.
- **Claude: not run against the real API.** Covered by automated tests with a mocked client (mapping,
  grounding, hallucination flagging, refusal/truncation/invalid-JSON handling, fallback). To measure it,
  set `ANTHROPIC_API_KEY` and re-run `python -m eval.run_eval` (≈200 calls).
- The rule-based numbers in `backend/eval/results/` and `results_holdout/` use no LLM.

## 2. AI-assisted development

This project was developed with the help of **Claude (Anthropic) running in Claude Code**, an AI coding
assistant. The assistant wrote the code, tests, synthetic dataset generator, evaluation harness and
documentation, ran the test-suite and evaluation, and iterated on failures it observed (for example
fixing OCR text without spaces, a too-lenient money tolerance, a labeling bug in the dataset, and CPU
oversubscription with parallel OCR workers). In later sessions it added the Gemini and local Ollama
providers, hybrid routing, the *Local LLM only* switch, model names in the UI and the tax/subtotal rules,
measured the local model, and tried prompt changes for handwriting (reverted when they didn't help).

The task brief (the assignment PDF) was given to the assistant together with the instruction to
complete it and document everything. Everything should be reviewed by the candidate before submission;
the candidate is responsible for understanding and being able to explain every part.
