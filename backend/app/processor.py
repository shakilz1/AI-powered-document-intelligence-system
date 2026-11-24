"""End-to-end processing of one document: load -> extract -> validate -> index.

Runs inside a background worker thread. Every stage is recorded in `processing_events`
(processing history) and progress is written to the document row so the UI can poll it.
"""
from __future__ import annotations

import logging
import time
import traceback
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import embeddings
from .db import session_scope
from .models import Document, Embedding, Invoice, LineItem, ProcessingEvent, ValidationIssue
from .pipeline import extractor, loaders
from .pipeline.crossverify import cross_verify
from .pipeline.llm import model_from_notes
from .pipeline.normalize import norm_invoice_number, norm_name
from .pipeline.ocr import OCRUnavailable
from .pipeline.types import ExtractedInvoice
from .pipeline.validation import Issue, needs_review, validate_invoice

log = logging.getLogger(__name__)


class _Progress:
    """Throttled progress writer (separate short transactions so the UI sees live progress)."""

    def __init__(self, doc_id: str, lo: float, hi: float, stage: str):
        self.doc_id, self.lo, self.hi, self.stage = doc_id, lo, hi, stage
        self._last = 0.0

    def __call__(self, frac: float, msg: str = "") -> None:
        now = time.monotonic()
        if now - self._last < 0.4 and frac < 1.0:
            return
        self._last = now
        with session_scope() as db:
            doc = db.get(Document, self.doc_id)
            if doc:
                doc.progress = self.lo + (self.hi - self.lo) * max(0.0, min(1.0, frac))
                doc.stage = f"{self.stage}: {msg}" if msg else self.stage


def _event(db: Session, doc_id: str, stage: str, status: str, message: str | None = None, duration_ms: int | None = None):
    db.add(ProcessingEvent(document_id=doc_id, stage=stage, status=status, message=message, duration_ms=duration_ms))


def _set(doc_id: str, **fields):
    with session_scope() as db:
        doc = db.get(Document, doc_id)
        for k, v in fields.items():
            setattr(doc, k, v)


# ------------------------------------------------------------------ validation (also used after corrections)

def invoice_as_validation_dict(inv: Invoice) -> dict:
    return {
        "invoice_number": inv.invoice_number, "invoice_date": inv.invoice_date, "supplier_name": inv.supplier_name,
        "customer_name": inv.customer_name, "subtotal": inv.subtotal, "tax": inv.tax, "total": inv.total,
        "currency": inv.currency, "confidence": inv.confidence or {}, "uncertain_fields": inv.uncertain_fields or [],
        "line_items": [{"description": li.description, "quantity": li.quantity, "unit_price": li.unit_price,
                        "discount_pct": li.discount_pct, "amount": li.amount} for li in inv.line_items],
    }


def _order_key(inv: Invoice):
    ts = inv.created_at.replace(tzinfo=None) if inv.created_at else None  # SQLite returns naive datetimes
    return (ts.isoformat() if ts else "", inv.page_start or 0, inv.id)


def find_duplicates(db: Session, inv: Invoice) -> list[Issue]:
    if not inv.invoice_number_norm:
        return []
    others = db.execute(
        select(Invoice).where(Invoice.invoice_number_norm == inv.invoice_number_norm, Invoice.id != inv.id)
    ).scalars().all()
    issues = []
    my_class = _source_class(inv)
    for o in others:
        same_supplier = (not o.supplier_name or not inv.supplier_name or norm_name(o.supplier_name) == norm_name(inv.supplier_name))
        if not same_supplier:
            continue
        where = "same document" if o.document_id == inv.document_id else f"document '{o.document.filename}'"
        if _source_class(o) != my_class:
            # Same invoice in a PDF/image and in a spreadsheet: that's a cross-reference, not a duplicate.
            # Automatically cross-verify the values instead (PDF <-> Excel verification).
            diffs = [m["field"] for m in cross_verify([_cv(inv)], [_cv(o)])["matches"][0]["differences"]]
            if diffs:
                issues.append(Issue("cross_check_mismatch", "error",
                                    f"Differs from the same invoice in {where} on: {', '.join(diffs)}", diffs[0]))
            else:
                issues.append(Issue("cross_check_ok", "info", f"Matches the same invoice in {where}", "invoice_number"))
            continue
        # Only flag the later-created record (the first one seen is the "original").
        if _order_key(o) > _order_key(inv):
            continue
        issues.append(Issue("duplicate_invoice", "error",
                            f"Duplicate of invoice {o.invoice_number} in {where} (total {o.total})", "invoice_number"))
    return issues


def _source_class(inv: Invoice) -> str:
    ft = inv.document.file_type if inv.document else ""
    return "spreadsheet" if ft in ("xlsx", "xls", "csv") else "document"


def _cv(inv: Invoice) -> dict:
    d = invoice_as_validation_dict(inv)
    d["id"] = inv.id
    return d


def revalidate(db: Session, inv: Invoice) -> None:
    db.execute(delete(ValidationIssue).where(ValidationIssue.invoice_id == inv.id))
    issues = validate_invoice(invoice_as_validation_dict(inv)) + find_duplicates(db, inv)
    inv.issues = [ValidationIssue(code=i.code, severity=i.severity, field=i.field, message=i.message) for i in issues]
    inv.needs_review = needs_review(issues)


def index_invoice(db: Session, inv: Invoice) -> None:
    content = embeddings.invoice_content(inv)
    backend, vecs = embeddings.embed([content])
    vec = vecs[0].astype("float32")
    if inv.embedding is None:
        inv.embedding = Embedding(backend=backend, dim=int(vec.shape[0]), vector=vec.tobytes(), content=content[:4000])
    else:
        inv.embedding.backend, inv.embedding.dim, inv.embedding.vector, inv.embedding.content = backend, int(vec.shape[0]), vec.tobytes(), content[:4000]


def refresh_document_status(doc: Document) -> None:
    if doc.status in ("failed", "queued", "processing"):
        return
    doc.status = "needs_review" if (not doc.invoices or any(i.needs_review for i in doc.invoices)) else "completed"


def _to_row(doc_id: str, e: ExtractedInvoice) -> Invoice:
    e.apply_defaults()  # tax 0 when absent, subtotal from line amounts when absent
    inv = Invoice(
        document_id=doc_id, page_start=e.page_start, page_end=e.page_end,
        invoice_number=e.invoice_number, invoice_number_norm=norm_invoice_number(e.invoice_number),
        invoice_date=e.invoice_date, supplier_name=e.supplier_name, customer_name=e.customer_name,
        subtotal=e.subtotal, tax=e.tax, total=e.total, currency=e.currency,
        confidence=e.confidence, uncertain_fields=e.uncertain_fields, extraction_method=e.method,
        raw_extraction=e.to_dict(), source_text=e.source_text[:200_000],
    )
    inv.line_items = [
        LineItem(position=i, description=(li.get("description") or None) and str(li["description"])[:512],
                 quantity=li.get("quantity"), unit_price=li.get("unit_price"), discount_pct=li.get("discount_pct"),
                 amount=li.get("amount"))
        for i, li in enumerate(e.line_items)
    ]
    return inv


# ------------------------------------------------------------------ main entry

def process_document(doc_id: str) -> None:
    t_start = time.perf_counter()
    with session_scope() as db:
        doc = db.get(Document, doc_id)
        if doc is None:
            return
        doc.status, doc.progress, doc.stage, doc.error = "processing", 0.0, "loading", None
        doc.attempts = (doc.attempts or 0) + 1
        # Reprocessing: drop previous results.
        for inv in list(doc.invoices):
            db.delete(inv)
        for p in list(doc.pages):
            db.delete(p)
        _event(db, doc_id, "processing", "started", f"attempt {doc.attempts}")
        path = Path(doc.storage_path)
        dup_of = db.execute(select(Document.filename).where(Document.sha256 == doc.sha256, Document.id != doc.id)).scalars().first()
        if dup_of:
            _event(db, doc_id, "upload", "warning", f"Identical file content already uploaded as '{dup_of}'")

    try:
        # 1. Load (text layer / OCR / spreadsheet parsing)
        t0 = time.perf_counter()
        loaded = loaders.load_document(path, _Progress(doc_id, 0.0, 0.6, "reading"))
        with session_scope() as db:
            doc = db.get(Document, doc_id)
            from .models import DocumentPage

            doc.pages = [DocumentPage(page_number=p.page_number, source=p.source, text=p.text, ocr_confidence=p.ocr_confidence)
                         for p in loaded.pages]
            doc.file_type, doc.page_count = loaded.file_type, len(loaded.pages)
            doc.ocr_used, doc.ocr_confidence = loaded.ocr_used, loaded.ocr_confidence
            ocr_pages = sum(p.source == "ocr" for p in loaded.pages)
            _event(db, doc_id, "load", "ok",
                   f"{loaded.file_type}: {len(loaded.pages)} page(s)/sheet(s), OCR on {ocr_pages}"
                   + (f", mean OCR confidence {loaded.ocr_confidence:.2f}" if loaded.ocr_confidence else ""),
                   int((time.perf_counter() - t0) * 1000))
            for w in loaded.warnings[:50]:
                _event(db, doc_id, "load", "warning", w)

        # 2. Extraction
        t0 = time.perf_counter()
        extracted = extractor.extract_document(loaded, _Progress(doc_id, 0.6, 0.9, "extracting"))
        methods = sorted({e.method for e in extracted})
        models = sorted({m for e in extracted if (m := model_from_notes(e.notes))})
        with session_scope() as db:
            doc = db.get(Document, doc_id)
            doc.extraction_method = ",".join(methods) if methods else None
            notes = [n for e in extracted for n in e.notes if "fallback" in n.lower() or "unavailable" in n.lower()]
            via = (doc.extraction_method or "-") + (f" ({', '.join(models)})" if models else "")
            _event(db, doc_id, "extract", "ok" if extracted else "warning",
                   f"{len(extracted)} invoice(s) extracted via {via}" if extracted else "No invoice data found",
                   int((time.perf_counter() - t0) * 1000))
            for n in notes[:5]:
                _event(db, doc_id, "extract", "warning", n)

        # 3. Persist, validate, de-duplicate, index
        t0 = time.perf_counter()
        _set(doc_id, stage="validating", progress=0.92)
        with session_scope() as db:
            doc = db.get(Document, doc_id)
            rows = [_to_row(doc_id, e) for e in extracted]
            db.add_all(rows)
            db.flush()
            for inv in rows:
                revalidate(db, inv)
                index_invoice(db, inv)
            # Existing invoices with the same number in other documents get re-checked too, so the
            # cross-reference / duplicate information is visible on both sides.
            norms = {r.invoice_number_norm for r in rows if r.invoice_number_norm}
            if norms:
                related = db.execute(select(Invoice).where(Invoice.invoice_number_norm.in_(norms),
                                                           Invoice.document_id != doc_id)).scalars().all()
                for o in related:
                    revalidate(db, o)
                    refresh_document_status(o.document)
            n_review = sum(r.needs_review for r in rows)
            _event(db, doc_id, "validate", "ok" if not n_review else "warning",
                   f"{n_review}/{len(rows)} invoice(s) flagged for manual review", int((time.perf_counter() - t0) * 1000))
            doc.status = "needs_review" if (not rows or n_review) else "completed"
            doc.progress, doc.stage = 1.0, "done"
            doc.processing_ms = int((time.perf_counter() - t_start) * 1000)
            _event(db, doc_id, "processing", "ok", f"finished in {doc.processing_ms} ms", doc.processing_ms)
    except (loaders.DocumentLoadError, OCRUnavailable) as exc:
        _fail(doc_id, str(exc), t_start)
    except Exception as exc:  # unexpected: keep the trace in the history for debugging
        log.exception("processing failed for %s", doc_id)
        _fail(doc_id, f"Internal error: {exc.__class__.__name__}: {exc}", t_start, traceback.format_exc(limit=5))


def _fail(doc_id: str, message: str, t_start: float, trace: str | None = None) -> None:
    import re

    # Never leak server file-system paths to API clients.
    message = re.sub(r"'?[A-Za-z]:\\[^'\s]+'?|'?/[^'\s]*/[^'\s]+'?", "<file>", message)
    with session_scope() as db:
        doc = db.get(Document, doc_id)
        doc.status, doc.error, doc.stage = "failed", message, "failed"
        doc.processing_ms = int((time.perf_counter() - t_start) * 1000)
        _event(db, doc_id, "processing", "error", message + (f"\n{trace}" if trace else ""))
