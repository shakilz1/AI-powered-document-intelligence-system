# AI-Powered Document Intelligence System

Upload invoices as **PDF (digital or scanned), JPG, PNG, TIFF, XLSX, XLS or CSV**; the system extracts
text (text layer or OCR), pulls out structured invoice data (number, date, supplier, customer,
subtotal, tax, total, currency, line items), validates it, detects duplicates, cross-verifies PDFs
against spreadsheets, flags records for manual review, and makes everything searchable through a
REST API and a React web interface.

| | |
|---|---|
| Architecture, design decisions, DB schema | [docs/architecture.md](docs/architecture.md) |
| Evaluation report (measured) | [backend/eval/results/evaluation_report.md](backend/eval/results/evaluation_report.md) · held-out: [results_holdout](backend/eval/results_holdout/evaluation_report.md) · local LLM: [results_ollama](backend/eval/results_ollama/evaluation_report.md) |
| Test results | [backend/test-results/pytest_output.txt](backend/test-results/pytest_output.txt) (127 passed) |
| AI prompts & AI-assistance disclosure | [docs/AI_DISCLOSURE.md](docs/AI_DISCLOSURE.md) |
| Walkthrough script for the recording | [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md) |

## Measured results

Labeled synthetic dataset: 53 files, 199 invoices, 165 pages — digital/multi-invoice/100-page PDFs,
scanned PDFs, noisy/blurred/rotated/low-res images, multi-sheet XLSX, XLS, comma and
semicolon/decimal-comma CSV, 5 corrupted files, injected validation errors.

| Metric | Rules only, seed 42 | Rules only, held-out seed 2027 | Local LLM (Ollama qwen3.5:4b), seed 42 |
|---|---|---|---|
| Document success rate (valid files) | 100 % | 100 % | 100 % |
| Corrupted files correctly rejected | 100 % | 100 % | 100 % |
| Header-field accuracy (8 fields) | 99.81 % | 99.87 % | **100 %** |
| Invoices with all 8 fields correct | 98.49 % | 98.99 % | **100 %** |
| Line-item amount accuracy | 99.56 % | 98.92 % | 100 % |
| Injected errors detected | 100 % | 100 % | 100 % |
| Review-flag recall / precision | 100 % / 90 % | 100 % / 72.7 % | 100 % / 85.7 % |
| Time per page — digital PDF / scanned PDF / image | 11–51 ms / 4.9 s / 4.4 s | | 9.5 s / 15.9 s / 16.0 s |
| 100-page PDF | 1.1 s (93 pages/s) | | 974 s (0.1 pages/s) |
| Whole dataset | | | 27 min |

Reports: [rules](backend/eval/results/evaluation_report.md) ·
[held-out](backend/eval/results_holdout/evaluation_report.md) ·
[local LLM](backend/eval/results_ollama/evaluation_report.md).

In rules-only mode every wrong value came from OCR of degraded images and was flagged for review. In the
local-LLM run the model's values are the ones stored (the rules only lower confidence when they
disagree): 135 invoices were extracted from text by the model, 19 from images (vision) and 45 spreadsheet
rows by the deterministic parser; none fell back to the rules. The one false review flag was a blurred
photo where the rules misread the customer and the model was right. The cost is speed: on this
laptop's CPU the model needs about 10–16 s per page, against milliseconds for the rules.

**Caveat:** the data is synthetic (generated from 3 layouts), so these numbers show that the pipeline
works end to end. They are not a prediction for real-world invoices. Real supplier layouts are more
varied, and on a real handwritten bill the 4B local model misread several amounts (see "Local option:
Ollama" below); those cases are caught by the validation rules and sent to review.

## Quick start (Windows / macOS / Linux)

Requirements: Python 3.11+ and Node 18+. No system OCR binary, Docker or database server needed.

From the project root (the folder with this README):

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
```

Optional: copy `.env.example` to `backend/.env` and set `ANTHROPIC_API_KEY` to enable Claude extraction, or
`GEMINI_API_KEY` for the free Gemini option (see "Free option: Google Gemini" below), or `LLM_PROVIDER=ollama`
for a local model (see "Local option: Ollama"). Without any of these, the app runs with the rule-based extractor.

Build the frontend once (it is then served by the backend at http://localhost:8000):

```bash
cd frontend
npm install
npm run build
```

Run the server:

```bash
cd backend
python -m uvicorn app.main:app --port 8000
```

Open http://localhost:8000 (UI) or http://localhost:8000/docs (interactive API docs).

For frontend development with hot reload: run the backend as above, then `npm run dev` in `frontend/`
and open http://localhost:5173 (API calls are proxied to :8000).

### Load demo data

```bash
cd backend
python -m eval.generate_dataset
python scripts/upload_samples.py
```

### Tests and evaluation

```bash
cd backend
python -m pytest -v
python -m eval.generate_dataset --seed 42
python -m eval.run_eval
```

The evaluation writes `eval/results/metrics.json` and `eval/results/evaluation_report.md`.

### Production-like stack

`docker-compose.yml` runs PostgreSQL + API + frontend (`docker compose up --build`). It is provided
but **was not run** during development because Docker was not installed on the machine.

## REST API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/documents` | Upload one or many files (multipart `files`). Returns 202 + ids; processing runs in the background |
| GET | `/api/documents` | List documents (`status`, `q`, paging) with progress |
| GET | `/api/documents/{id}` | Document detail: invoices, issues, processing history |
| GET | `/api/documents/{id}/status` | Status, progress (0–1), current stage, error |
| GET | `/api/documents/{id}/results` | Extracted structured JSON |
| GET | `/api/documents/{id}/text` | Extracted text per page (source text/OCR + confidence) |
| GET | `/api/documents/{id}/file` | Original file |
| POST | `/api/documents/{id}/reprocess` | Re-run processing |
| POST | `/api/jobs/reprocess-failed` | Re-queue every failed document |
| DELETE | `/api/documents/{id}` | Delete a document and its results |
| GET | `/api/invoices` | Keyword filtering: `q, supplier, customer, invoice_number, currency, date_from, date_to, min_total, max_total, needs_review, document_id` |
| GET | `/api/invoices/{id}` | Invoice with source text, notes, correction history |
| PATCH | `/api/invoices/{id}` | Manual correction (any field, line items, `mark_reviewed`, `corrected_by`), re-validates, audit-logged |
| GET | `/api/search?q=&mode=keyword\|semantic\|hybrid` | Search |
| POST | `/api/crossverify` | `{left_document_ids, right_document_ids}`: PDF↔PDF / PDF↔Excel comparison report |
| GET | `/api/stats`, `/api/health` | Dashboard numbers, health/config |

Shape of an extraction result (`GET /api/documents/{id}/results`, shortened, values illustrative):

```json
{
  "invoice_number": "INV-2025-00001", "invoice_date": "2025-08-03",
  "supplier_name": "Northwind Traders", "customer_name": "Coho Winery",
  "subtotal": 15500.41, "tax": 2325.06, "total": 17825.47, "currency": "USD",
  "line_items": [{"description": "Office chair ergonomic", "quantity": 12, "unit_price": 812.5, "amount": 9750.0}],
  "confidence": {"invoice_number": 0.9, "supplier_name": 0.75, "currency": 0.95},
  "uncertain_fields": [], "needs_review": false,
  "issues": []
}
```

Missing values are `null`, never guessed. Unsure values are listed in `uncertain_fields` and flag the
invoice for review.

## Project layout

```
backend/
  app/
    main.py            REST API, security middleware, serves the built UI
    config.py          settings (env / .env), LLM provider routing, Local LLM only switch
    db.py, models.py   SQLAlchemy engine + schema
    worker.py          background job queue (process pool, restart recovery)
    processor.py       load -> extract -> validate -> index, processing history
    search.py          keyword filters, vector search, hybrid ranking
    embeddings.py      fastembed or hashing embeddings
    pipeline/
      loaders.py       file sniffing, PDF text/OCR, images, XLSX/XLS/CSV
      ocr.py           RapidOCR + pre-processing variants
      rules.py         deterministic invoice extractor
      spreadsheet.py   table/column mapping extractor
      llm.py           LLM extraction: Claude, Gemini or local Ollama (structured JSON)
      extractor.py     segmentation, LLM+rules merge, grounding
      validation.py    validation rules
      crossverify.py   PDF<->PDF / PDF<->Excel comparison
      normalize.py     money/date/currency/name normalisation
  tests/               127 automated tests
  eval/                synthetic labeled dataset generator + evaluation runner + results
  scripts/             demo upload script
frontend/              React (Vite) UI
docs/                  architecture, AI disclosure, walkthrough script
```

## Real-world images, handwriting and vision (Claude, free Gemini or local Ollama)

Tested on 3 real phone photos of Bangladeshi invoices/bills:

- **Printed invoice:** works without an API key. All 3 line items come through (quantity "26Pcs", unit price,
  13 % discount, amount), along with the total in BDT and the date ("1-Sop-26" was misread by OCR and is corrected
  to 2026-09-01), and the totals reconcile. The invoice number and customer were garbled by OCR and are flagged
  for review.
- **Handwritten bills / forms:** the local OCR engine only reads printed text, so handwritten items come out
  as noise and the rule-based extractor can't recover them. With `ANTHROPIC_API_KEY` set, photos and scanned
  pages are sent **as images** to Claude (`LLM_VISION=true`), which reads handwriting. The OCR text goes along
  only as a hint. This path is unit-tested with a mocked client but **has not been run against the real API yet.**

### Free option: Google Gemini

If you don't want to pay for the Anthropic API, the same vision path can use Google Gemini's free tier:

1. Go to https://aistudio.google.com/apikey, sign in with a Google account and click **Create API key**.
2. Put it in `backend/.env`: `GEMINI_API_KEY=your-key`
3. Restart the server. `/api/health` should show `"llm_provider": "gemini"`.

How it behaves:
- `LLM_PROVIDER=auto` (default) uses Claude when `ANTHROPIC_API_KEY` is set, otherwise Gemini.
- Only photos/scans are sent to Gemini (`GEMINI_TEXT=false`) because the free quota is small (tens to a few
  hundred requests a day, depending on the model). Typed PDFs and spreadsheets keep using the rule-based extractor.
- If the main model (`gemini-flash-latest`) is out of quota, it retries with `gemini-flash-lite-latest`. If that
  also fails, the document falls back to the rules and the note "Vision extraction unavailable: …" is shown.
- Gemini results go through the same checks as Claude: JSON schema, rules cross-check, validation, review flags.
- **Privacy:** on the free tier Google may use the content you send to improve its products. That's fine for
  sample bills; don't use it for confidential documents.
- Busy responses (HTTP 503/429, common on the free tier) are retried after 3 s, 8 s and 20 s.
- Tested with a real free-tier key on the 3 real photos: all three were read via vision; the printed invoice
  came out fully correct, and the handwritten bills were extracted, with inconsistencies flagged for review.
- Implemented in `backend/app/pipeline/llm.py` (plain REST via httpx, no new dependency), tests in
  `backend/tests/test_gemini.py` (HTTP mocked).

### Local option: Ollama (no key, nothing leaves the machine)

The same extraction (text and vision) can run on a local model served by [Ollama](https://ollama.com):

1. Install Ollama and pull a model: `ollama pull qwen3.5:4b` (3.3 GB, reads images).
2. In `backend/.env`: `LLM_PROVIDER=ollama` and `OLLAMA_MODEL=qwen3.5:4b`.
3. Restart the server. `/api/health` should show `"llm_provider": "ollama"`.

How it behaves:
- Requests use Ollama's structured outputs (`format` = the invoice JSON schema), temperature 0, thinking off.
- Typed PDFs and spreadsheets go to the model as well (`OLLAMA_TEXT=true`), since there is no quota to save.
- If Ollama is not running or the call fails, the document falls back to the rule-based extractor with a note.
- Results go through the same grounding, rules cross-check and validation as the cloud models.
- Measured on this laptop (CPU only, no GPU) with `qwen3.5:4b`: about 15 s per typed invoice and 15–20 s per
  photo. A typed test invoice came out fully correct. On the handwritten Burak Electric bill the 4B model
  misread several handwritten amounts (e.g. 1800 → 180) and the total; the line-item and total checks
  flag it for review. A bigger model (e.g. `qwen3.5:9b` or larger, with a GPU) reads handwriting better.
- Implemented in `backend/app/pipeline/llm.py::_ollama_call` (plain REST via httpx), tests in
  `backend/tests/test_ollama.py` (HTTP mocked).

### Hybrid: local model for typed documents, Gemini for photos

Small local models are good on typed text but weak on handwriting. `VISION_PROVIDER` sends only
photos/scans to a different provider, while typed PDFs and spreadsheets stay on the local model:

```
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen3.5:4b
VISION_PROVIDER=gemini      # needs GEMINI_API_KEY
```

`/api/health` shows both (`llm_provider` and `vision_provider`). If the vision call fails (quota, outage),
the photo falls back to the local text model and then to the rules. Measured on the handwritten Burak
Electric bill (correct total 26,250, dated 02/11/26):

| | qwen3.5:4b (local) | Hybrid (Gemini Flash-Lite for the photo) |
|---|---|---|
| Bill number / date / total | 5028 / 2026-08-12 (photo stamp) / 26,400 | **5029 / 2026-11-02 / 26,250** |
| Line amounts correct | 10 / 10 | 9 / 10 |

Neither is perfect. In both cases the line-item and total checks flag the bill for review. Trade-off: in
hybrid mode photos leave the machine (on the free tier Google may use them), and typed documents don't.

### "Local LLM only" switch (in the UI)

The top bar has a **Local LLM only** switch. When it's on, every document (typed and photos) goes to the
local Ollama model and nothing leaves the machine, whatever `.env` says. When it's off, the `.env` setup
applies (e.g. hybrid). Under the switch the UI shows which model reads typed documents and which reads photos,
and it warns if Ollama isn't running or the model isn't pulled.

- The switch applies to documents processed from then on. Use **Reprocess** to re-read older ones.
- It's stored in `backend/data/runtime_settings.json`, so it survives restarts, and the background worker
  processes pick it up without a restart.
- API: `GET /api/settings/llm` (current routing + Ollama status), `PUT /api/settings/llm` with
  `{"local_only": true|false}`.
- Each invoice card and the processing history name the model that read the document (e.g. `qwen3.5:4b`,
  `gemini-flash-lite-latest`).

### Filled-in values: tax and subtotal

Two business rules run after extraction, before validation. The UI labels each filled-in value, so it's
never presented as if it had been read from the document:

- **No tax on the document → tax = 0** (labeled "not on document → 0").
- **No subtotal on the document → subtotal = sum of the line-item amounts**, only when every line has an
  amount (labeled "= sum of amounts").

Printed values are never overwritten. The checks still compare subtotal + tax with the printed total, so
a misread line or an untaxed total that doesn't add up is still flagged for review. The filled-in fields
are listed per invoice in the API (`derived_fields`). Implemented in
`backend/app/pipeline/types.py::apply_defaults`.

### Calculations while you correct an invoice

In the invoice editor, values that depend on what you change are recalculated as you type:

- changing **quantity, unit price or discount %** sets the line **amount** = quantity × unit price × (1 − discount %);
- changing any line sets the **subtotal** = sum of the line amounts;
- changing the lines, the subtotal or the tax sets the **total** = subtotal + tax.

Recalculated values are highlighted and labeled "recalculated"; nothing is saved until you click
*Save corrections*. A value you type directly into a line amount or the total is kept as typed.
The **Recalculate** button redoes every line, the subtotal and the total in one click (useful for an
invoice saved earlier with a stale amount). The API does the same for line items sent with quantity and
unit price but no amount (`PATCH /api/invoices/{id}`).

## Known limitations / next steps

- The evaluation numbers come from synthetic data (rules only, and with a local 4B model). Next step: label 100–200 real invoices
  and measure the Claude path on them.
- The default embedding backend is lexical. Install `fastembed` for true semantic search, or use pgvector/Qdrant at scale.
- The in-process job queue scales to one machine. For several hosts, run `process_document` in
  Celery/RQ workers backed by Redis.
- OCR takes about 4–5 s per page on CPU. A GPU build of onnxruntime, or lowering `OCR_DPI`, speeds it up.
- `xlrd` (used for .xls) is deprecated in pandas. `python-calamine` is the drop-in replacement.
- There are no user accounts or roles yet (API-key auth only), no rate limiting, and no malware scanning of uploads.
