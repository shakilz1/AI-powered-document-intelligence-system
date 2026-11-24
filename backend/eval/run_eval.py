"""Run the labeled dataset through the real API and report measured metrics.

    python -m eval.run_eval --dataset eval/dataset --out eval/results

Uses a fresh database, uploads every file via POST /api/documents (processing runs inline
so timing is exact), reads results via GET /api/documents/{id}, and compares to labels.json.
Writes results/metrics.json and results/evaluation_report.md.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

FIELDS = ["invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"]


def _setup_env(tmp: Path) -> None:
    os.environ["DATABASE_URL"] = f"sqlite:///{(tmp / 'eval.db').as_posix()}"
    os.environ["STORAGE_DIR"] = str(tmp / "uploads")
    os.environ["PROCESS_INLINE"] = "true"
    os.environ.pop("API_KEY", None)


def _eq(field, pred, gt) -> bool:
    from app.pipeline.normalize import norm_invoice_number, norm_name

    if gt is None:
        return pred in (None, "")
    if pred in (None, ""):
        return False
    if field in ("subtotal", "tax", "total"):
        return abs(float(pred) - float(gt)) <= 0.011
    if field == "invoice_number":
        return norm_invoice_number(pred) == norm_invoice_number(gt)
    if field in ("supplier_name", "customer_name"):
        return norm_name(pred) == norm_name(gt)
    return str(pred).strip().upper() == str(gt).strip().upper()


def _match(preds: list[dict], gts: list[dict]) -> list[tuple[dict, dict | None]]:
    from app.pipeline.normalize import norm_invoice_number

    by_no = {}
    for p in preds:
        by_no.setdefault(norm_invoice_number(p.get("invoice_number")), []).append(p)
    used, pairs = set(), []
    for i, g in enumerate(gts):
        cand = next((p for p in by_no.get(norm_invoice_number(g["invoice_number"]), []) if id(p) not in used), None)
        if cand is None and i < len(preds) and id(preds[i]) not in used:
            cand = preds[i]  # positional fallback (e.g. invoice number mis-read)
        if cand is not None:
            used.add(id(cand))
        pairs.append((g, cand))
    return pairs


def _pct(a, b):
    return round(100.0 * a / b, 2) if b else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(Path(__file__).parent / "dataset"))
    ap.add_argument("--out", default=str(Path(__file__).parent / "results"))
    args = ap.parse_args()
    dataset, out = Path(args.dataset), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    labels = json.loads((dataset / "labels.json").read_text(encoding="utf-8"))

    tmp = Path(tempfile.mkdtemp(prefix="docintel_eval_"))
    _setup_env(tmp)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app
    from app.pipeline.ocr import engine_name

    mode = (f"llm ({settings.provider} {settings.active_model}) + rules cross-check" if settings.text_llm_available
            else "rule-based (no LLM key set)")
    print("extraction mode:", mode)

    field_ok = defaultdict(int)
    field_total = defaultdict(int)
    cat_ok = defaultdict(lambda: defaultdict(int))
    cat_total = defaultdict(lambda: defaultdict(int))
    exact_ok = exact_total = 0
    li_count_ok = li_total = li_amount_ok = li_amount_total = 0
    missed_invoices = extra_invoices = 0
    doc_rows = []
    review_tp = review_fp = review_fn = review_tn = 0
    flag_hits = flag_expected = 0
    errors_detail = []

    with TestClient(app) as client:
        t_all = time.perf_counter()
        uploaded = []
        for lab in labels:
            path = dataset / lab["file"]
            t0 = time.perf_counter()
            with open(path, "rb") as fh:
                r = client.post("/api/documents", files=[("files", (lab["file"], fh, "application/octet-stream"))])
            wall_ms = (time.perf_counter() - t0) * 1000
            r.raise_for_status()
            acc = r.json()["accepted"]
            doc_id = acc[0]["id"] if acc else None
            uploaded.append((lab, doc_id, wall_ms))
            print(f"  {lab['file']:<34} {wall_ms:8.0f} ms")
        total_wall = time.perf_counter() - t_all

        # Read results after everything is processed (cross-document checks are then complete).
        for lab, doc_id, wall_ms in uploaded:
            d = client.get(f"/api/documents/{doc_id}").json() if doc_id else {"status": "rejected", "invoices": []}
            ok = d["status"] in ("completed", "needs_review")
            doc_rows.append({"file": lab["file"], "category": lab["category"], "expect_fail": lab["expect_fail"],
                             "status": d["status"], "error": d.get("error"), "pages": d.get("page_count", 0),
                             "processing_ms": d.get("processing_ms"), "wall_ms": round(wall_ms),
                             "ocr_used": d.get("ocr_used"), "ocr_confidence": d.get("ocr_confidence"),
                             "invoices_found": len(d.get("invoices", [])), "invoices_expected": len(lab["invoices"])})
            if lab["expect_fail"] or not ok:
                if not lab["expect_fail"]:
                    missed_invoices += len(lab["invoices"])
                    for g in lab["invoices"]:
                        for f in FIELDS:
                            field_total[f] += 1; cat_total[lab["category"]][f] += 1
                        exact_total += 1
                continue
            preds = d["invoices"]
            pairs = _match(preds, lab["invoices"])
            extra_invoices += max(0, len(preds) - len(lab["invoices"]))
            for g, p in pairs:
                all_ok = True
                for f in FIELDS:
                    field_total[f] += 1; cat_total[lab["category"]][f] += 1
                    good = p is not None and _eq(f, p.get(f), g.get(f))
                    if good:
                        field_ok[f] += 1; cat_ok[lab["category"]][f] += 1
                    else:
                        all_ok = False
                        if len(errors_detail) < 60:
                            errors_detail.append({"file": lab["file"], "invoice": g["invoice_number"], "field": f,
                                                  "expected": g.get(f), "got": None if p is None else p.get(f)})
                exact_total += 1; exact_ok += all_ok
                if p is None:
                    missed_invoices += 1
                    continue
                li_total += 1
                li_count_ok += len(p["line_items"]) == len(g["line_items"])
                for gi, pi in zip(g["line_items"], p["line_items"]):
                    li_amount_total += 1
                    li_amount_ok += pi.get("amount") is not None and abs(pi["amount"] - gi["amount"]) <= 0.011
                li_amount_total += max(0, len(g["line_items"]) - len(p["line_items"]))
                # validation: expected flags vs raised issues
                exp = lab["expected_flags"].get(g["invoice_number"], [])
                codes = {i["code"] for i in p["issues"]}
                for c in exp:
                    flag_expected += 1; flag_hits += c in codes
                # A record "should" be reviewed if the document contains an injected error OR the
                # extraction got any header field wrong (a human must catch that too).
                should_review = bool(exp) or not all_ok
                if p["needs_review"] and should_review:
                    review_tp += 1
                elif p["needs_review"]:
                    review_fp += 1
                elif should_review:
                    review_fn += 1
                else:
                    review_tn += 1

        stats = client.get("/api/stats").json()

    valid = [r for r in doc_rows if not r["expect_fail"]]
    corrupt = [r for r in doc_rows if r["expect_fail"]]
    succeeded = [r for r in valid if r["status"] in ("completed", "needs_review")]
    proc = [r["processing_ms"] for r in succeeded if r["processing_ms"] is not None]
    per_page = [r["processing_ms"] / max(1, r["pages"]) for r in succeeded if r["processing_ms"] and r["pages"]]
    by_cat_time = defaultdict(list)
    for r in succeeded:
        if r["processing_ms"] is not None:
            by_cat_time[r["category"]].append(r["processing_ms"] / max(1, r["pages"]))
    large = next((r for r in doc_rows if r["category"] == "pdf_large"), None)

    metrics = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "extraction_mode": mode,
        "ocr_engine": engine_name(),
        "dataset": {"files": len(labels), "valid_files": len(valid), "corrupted_files": len(corrupt),
                    "labeled_invoices": sum(len(l["invoices"]) for l in labels),
                    "pages": sum(r["pages"] or 0 for r in doc_rows)},
        "document_success_rate_pct": _pct(len(succeeded), len(valid)),
        "document_failure_rate_pct": _pct(len(valid) - len(succeeded), len(valid)),
        "corrupted_correctly_rejected_pct": _pct(sum(r["status"] in ("failed", "rejected") for r in corrupt), len(corrupt)),
        "field_accuracy_pct": {f: _pct(field_ok[f], field_total[f]) for f in FIELDS},
        "overall_field_accuracy_pct": _pct(sum(field_ok.values()), sum(field_total.values())),
        "invoice_exact_match_pct": _pct(exact_ok, exact_total),
        "invoice_detection": {"missed": missed_invoices, "extra": extra_invoices},
        "line_items": {"count_correct_pct": _pct(li_count_ok, li_total), "amount_accuracy_pct": _pct(li_amount_ok, li_amount_total)},
        "accuracy_by_category_pct": {c: _pct(sum(cat_ok[c].values()), sum(cat_total[c].values())) for c in sorted(cat_total)},
        "validation": {
            "expected_flags_detected_pct": _pct(flag_hits, flag_expected),
            "review_precision_pct": _pct(review_tp, review_tp + review_fp),
            "review_recall_pct": _pct(review_tp, review_tp + review_fn),
            "false_review_rate_pct": _pct(review_fp, review_fp + review_tn),
            "confusion": {"tp": review_tp, "fp": review_fp, "fn": review_fn, "tn": review_tn},
        },
        "timing": {
            "total_wall_seconds": round(total_wall, 2),
            "doc_ms_mean": round(statistics.mean(proc), 1) if proc else None,
            "doc_ms_median": round(statistics.median(proc), 1) if proc else None,
            "doc_ms_p95": round(sorted(proc)[int(0.95 * (len(proc) - 1))], 1) if proc else None,
            "page_ms_mean": round(statistics.mean(per_page), 1) if per_page else None,
            "page_ms_by_category": {c: round(statistics.mean(v), 1) for c, v in sorted(by_cat_time.items())},
            "large_pdf": {"pages": large["pages"], "processing_ms": large["processing_ms"],
                          "pages_per_second": round(large["pages"] / (large["processing_ms"] / 1000), 1)} if large and large["processing_ms"] else None,
        },
        "api_stats": stats,
        "documents": doc_rows,
        "sample_errors": errors_detail,
    }
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    (out / "evaluation_report.md").write_text(render_report(metrics), encoding="utf-8")
    print(json.dumps({k: v for k, v in metrics.items() if k not in ("documents", "sample_errors", "api_stats")}, indent=2))


def render_report(m: dict) -> str:
    L = []
    a = L.append
    a("# Evaluation report\n")
    a(f"Run: {m['run_at']}  \nExtraction mode: **{m['extraction_mode']}**  \nOCR engine: {m['ocr_engine']}\n")
    ds = m["dataset"]
    a(f"Dataset: {ds['files']} files ({ds['valid_files']} valid, {ds['corrupted_files']} deliberately corrupted), "
      f"{ds['labeled_invoices']} labeled invoices, {ds['pages']} pages/sheets. Generated by `eval/generate_dataset.py` "
      "(synthetic, seeded) — see the README for what that does and does not prove.\n")
    a("## Headline metrics\n")
    a("| Metric | Value |\n|---|---|")
    a(f"| Document success rate (valid files) | {m['document_success_rate_pct']}% |")
    a(f"| Document failure rate (valid files) | {m['document_failure_rate_pct']}% |")
    a(f"| Corrupted files correctly rejected | {m['corrupted_correctly_rejected_pct']}% |")
    a(f"| Overall field accuracy (8 header fields) | {m['overall_field_accuracy_pct']}% |")
    a(f"| Invoices with all 8 fields correct | {m['invoice_exact_match_pct']}% |")
    a(f"| Line-item count correct | {m['line_items']['count_correct_pct']}% |")
    a(f"| Line-item amount accuracy | {m['line_items']['amount_accuracy_pct']}% |")
    a(f"| Invoices missed / extra | {m['invoice_detection']['missed']} / {m['invoice_detection']['extra']} |")
    t = m["timing"]
    a(f"| Mean / median / p95 processing time per document | {t['doc_ms_mean']} / {t['doc_ms_median']} / {t['doc_ms_p95']} ms |")
    a(f"| Mean processing time per page | {t['page_ms_mean']} ms |")
    if t.get("large_pdf"):
        a(f"| Large PDF ({t['large_pdf']['pages']} pages) | {t['large_pdf']['processing_ms']} ms ({t['large_pdf']['pages_per_second']} pages/s) |")
    a(f"| Total wall time for the whole dataset | {t['total_wall_seconds']} s |\n")
    a("## Field accuracy\n")
    a("| Field | Accuracy |\n|---|---|")
    for f, v in m["field_accuracy_pct"].items():
        a(f"| {f} | {v}% |")
    a("\n## Accuracy by input category\n")
    a("| Category | Field accuracy | Mean ms / page |\n|---|---|---|")
    for c, v in m["accuracy_by_category_pct"].items():
        a(f"| {c} | {v}% | {t['page_ms_by_category'].get(c, '-')} |")
    v = m["validation"]
    a("\n## Validation\n")
    a(f"- Injected errors detected (expected issue codes raised): **{v['expected_flags_detected_pct']}%**")
    a(f"- Manual-review flag precision: {v['review_precision_pct']}%, recall: {v['review_recall_pct']}%, "
      f"false review rate on clean invoices: {v['false_review_rate_pct']}%  (confusion {v['confusion']})")
    a("- \"Should be reviewed\" = the invoice carries an injected error, or extraction got at least one header field wrong.\n")
    a("## Per-document results\n")
    a("| File | Category | Status | Pages | OCR conf | Invoices found/expected | ms |\n|---|---|---|---|---|---|---|")
    for r in m["documents"]:
        a(f"| {r['file']} | {r['category']} | {r['status']} | {r['pages']} | {r['ocr_confidence'] or '-'} | "
          f"{r['invoices_found']}/{r['invoices_expected']} | {r['processing_ms']} |")
    if m["sample_errors"]:
        a("\n## Sample extraction errors (first 60)\n")
        a("| File | Invoice | Field | Expected | Got |\n|---|---|---|---|---|")
        for e in m["sample_errors"]:
            a(f"| {e['file']} | {e['invoice']} | {e['field']} | {e['expected']} | {e['got']} |")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
