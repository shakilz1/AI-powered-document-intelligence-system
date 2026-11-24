# Walkthrough recording script (≈9–10 minutes)

The brief asks for a recorded walkthrough. It can't be produced automatically, so this is a script to
record it yourself (Windows: Win+G for Xbox Game Bar, or OBS). Times in brackets are speaking time;
pause the recording while long files process if you want a shorter video.

## Before recording

1. **Keep your real photos.** `backend/data/` holds the database *and* your uploaded files. Copy the two
   WhatsApp photos (the Burak Electric handwritten bill and the printed invoice 260901) somewhere safe,
   e.g. your Desktop, before the next step.
2. **Clean start:** stop the server and delete `backend/data/`. This also resets the *Local LLM only* switch to off.
3. **Test files:** in `backend/`, run `python -m eval.generate_dataset` once (creates `backend/eval/dataset/`).
4. **Ollama:** make sure Ollama is running (`ollama list` shows `qwen3.5:4b`). Send one small request first
   so the model is loaded and the first demo upload isn't slow.
5. **Start the server** in `backend/`: `python -m uvicorn app.main:app --port 8000`, then open
   http://localhost:8000. `backend/.env` should have `LLM_PROVIDER=ollama`, `OLLAMA_MODEL=qwen3.5:4b`,
   `VISION_PROVIDER=gemini` and your `GEMINI_API_KEY`.
6. Have `README.md`, `docs/architecture.md` and a terminal open in other windows.

## Script

1. **Intro (30 s).** The brief: thousands of invoices, receipts, scans and spreadsheets a day, entered by
   hand. Show the README links table and the architecture diagram in `docs/architecture.md`.

2. **Which AI reads what (45 s).** Point at the top bar.
   - Switch **off**: the line under it says *Typed: qwen3.5:4b (local) · Photos: gemini-flash-latest*.
     That's hybrid mode: typed documents stay on this PC, and photos/handwriting go to Gemini because the
     small local model misreads handwriting.
   - Turn **Local LLM only** on: it now says *All documents: qwen3.5:4b (local)*. Nothing leaves the
     machine. Mention it's stored in `backend/data/runtime_settings.json` and the background workers pick
     it up without a restart.
   - Leave it **on** for steps 3–7.

3. **Upload (1 min).** Drag a mix from `backend/eval/dataset/` onto the drop zone: `digital_A_00.pdf`,
   `multi_invoice_0.pdf`, `scanned_noisy_1.pdf`, `image_lowres_4.jpg`, `ledger_0.xlsx`, `legacy_0.xls`,
   `export_semicolon_2.csv`, `corrupt_truncated.pdf`. Point out the live progress bars. Spreadsheets and CSVs
   finish in milliseconds (deterministic parser, no AI); typed PDFs take ~10 s and photos ~15 s on the local
   model; `multi_invoice_0.pdf` takes ~50 s (5 invoices). When a file is done, the list shows
   *done · qwen3.5:4b*.

4. **Results (1.5 min).** Open `multi_invoice_0.pdf`: five invoices were split out of one PDF. Show:
   - *Model: qwen3.5:4b* in the document details and *llm · qwen3.5:4b* on each invoice card;
   - the fields with confidence colours and the line items;
   - the *Extracted text* tab, and the *Processing history* tab (*extracted via llm (qwen3.5:4b)*).
   Open `scanned_noisy_1.pdf` and show the OCR confidence.

5. **Failure handling (30 s).** `corrupt_truncated.pdf` failed with a clean error message. Click
   *Reprocess all failed* to show the reprocessing endpoint (it fails again, as it should).

6. **Validation and review (1.5 min).** Upload `err_total_mismatch.pdf`, `err_line_items.pdf`,
   `dup_original.pdf`, `dup_copy.pdf`. Open the *Review queue*. Show `total_mismatch`,
   `line_items_mismatch`/`line_item_math` and `duplicate_invoice`. Correct the total on the mismatch
   invoice, enter your name, click *Save & mark reviewed*. The error disappears and the document turns
   *Completed*. Mention the audit log (`corrections` table).

7. **Handwritten bill, local vs Gemini (1.5 min).** Upload the Burak Electric photo with the switch still **on**.
   - It's read by qwen3.5:4b. There's no tax and no subtotal on the bill, so the app shows
     *Tax · not on document → 0* and *Subtotal · = sum of amounts*. Edit one line amount (don't save):
     the subtotal updates live.
   - The small model misreads the total and the date (it takes the WhatsApp timestamp). The invoice is
     flagged *Needs review* because subtotal + tax ≠ total. That's the safety net.
   - Turn the switch **off** and click *Reprocess*. Now the photo goes to Gemini (the card shows the Gemini
     model). Bill number, date (2 Nov 2026) and total (26,250) come out right. Say honestly that it's still
     flagged when a line is misread: neither model is perfect on handwriting, and the checks catch it.
   - If Gemini answers "busy", the app retries, then uses `gemini-flash-lite-latest`, then falls back to
     the local model. The processing history shows which one ran.

8. **PDF ↔ Excel cross-verification (1 min).** Upload `xv_invoices.pdf` and `xv_ledger.csv`. One invoice
   shows `cross_check_mismatch` automatically. Then open the *Cross-verify* tab, choose the PDF on the left
   and the CSV on the right, and run it to see the field-level difference report.

9. **Search (45 s).** Keyword search for a supplier name. Semantic mode with a typo (e.g. "nortwind vendor").
   Filters: date range + min total + "needs review".

10. **API, tests and evaluation (1 min).** Show http://localhost:8000/docs, including
    `GET /api/settings/llm`. Show `backend/test-results/pytest_output.txt` (126 passed) or run `pytest -q`.
    Then the evaluation reports:
    - rules only: 99.81 % field accuracy (`backend/eval/results/evaluation_report.md`), held-out 99.87 %;
    - local LLM qwen3.5:4b: 100 % field accuracy on all 199 invoices, but ~10–16 s per page against
      milliseconds for the rules (`backend/eval/results_ollama/evaluation_report.md`).

11. **Close (30 s).** Limitations from the README: the evaluation data is synthetic (3 layouts); the small
    local model is weak on handwriting (hybrid or a bigger model helps); in hybrid mode photos are sent to
    Google; the Claude path is implemented and unit-tested but hasn't run against the real API.

## After recording

Put your saved photos back if you want them in the app (upload them again), and set the
*Local LLM only* switch the way you want it.
