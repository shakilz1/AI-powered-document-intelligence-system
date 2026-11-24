from __future__ import annotations

from .models import Document, Invoice
from .pipeline.llm import model_from_notes


def _iso(dt):
    return dt.isoformat() if dt else None


def invoice_to_dict(inv: Invoice, *, full: bool = False) -> dict:
    d = {
        "id": inv.id,
        "document_id": inv.document_id,
        "filename": inv.document.filename if inv.document else None,
        "page_start": inv.page_start,
        "page_end": inv.page_end,
        "invoice_number": inv.invoice_number,
        "invoice_date": inv.invoice_date,
        "supplier_name": inv.supplier_name,
        "customer_name": inv.customer_name,
        "subtotal": inv.subtotal,
        "tax": inv.tax,
        "total": inv.total,
        "currency": inv.currency,
        "line_items": [
            {"description": li.description, "quantity": li.quantity, "unit_price": li.unit_price,
             "discount_pct": li.discount_pct, "amount": li.amount}
            for li in inv.line_items
        ],
        "confidence": inv.confidence or {},
        "uncertain_fields": inv.uncertain_fields or [],
        "extraction_method": inv.extraction_method,
        "model": model_from_notes((inv.raw_extraction or {}).get("notes")),  # e.g. qwen3.5:4b, gemini-flash-latest
        "derived_fields": (inv.raw_extraction or {}).get("derived_fields", []),  # e.g. tax=0, subtotal=sum(lines)
        "needs_review": inv.needs_review,
        "reviewed": inv.reviewed,
        "issues": [
            {"code": i.code, "severity": i.severity, "field": i.field, "message": i.message} for i in inv.issues
        ],
        "updated_at": _iso(inv.updated_at),
    }
    if full:
        d["source_text"] = inv.source_text
        d["notes"] = (inv.raw_extraction or {}).get("notes", [])
        d["corrections"] = [
            {"field": c.field, "old_value": c.old_value, "new_value": c.new_value, "corrected_by": c.corrected_by,
             "created_at": _iso(c.created_at)}
            for c in inv.corrections
        ]
    return d


def document_to_dict(doc: Document, *, full: bool = False) -> dict:
    d = {
        "id": doc.id,
        "filename": doc.filename,
        "file_type": doc.file_type,
        "size_bytes": doc.size_bytes,
        "status": doc.status,
        "progress": round(doc.progress or 0, 3),
        "stage": doc.stage,
        "page_count": doc.page_count,
        "ocr_used": doc.ocr_used,
        "ocr_confidence": doc.ocr_confidence,
        "extraction_method": doc.extraction_method,
        "models": sorted({m for i in doc.invoices if (m := model_from_notes((i.raw_extraction or {}).get("notes")))}),
        "error": doc.error,
        "attempts": doc.attempts,
        "processing_ms": doc.processing_ms,
        "invoice_count": len(doc.invoices),
        "review_count": sum(1 for i in doc.invoices if i.needs_review),
        "created_at": _iso(doc.created_at),
        "updated_at": _iso(doc.updated_at),
    }
    if full:
        d["invoices"] = [invoice_to_dict(i) for i in sorted(doc.invoices, key=lambda x: (x.page_start or 0, x.created_at))]
        d["events"] = [
            {"stage": e.stage, "status": e.status, "message": e.message, "duration_ms": e.duration_ms,
             "created_at": _iso(e.created_at)}
            for e in doc.events
        ]
    return d
