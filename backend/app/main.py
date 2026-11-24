"""Task 4 — REST API (FastAPI). Interactive docs at /docs when the server runs."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import embeddings, search
from .config import BASE_DIR, settings
from .db import get_db, init_db
from .models import Correction, Document, Invoice, LineItem
from .pipeline import ocr as ocr_mod
from .pipeline.crossverify import cross_verify
from .pipeline.loaders import SUPPORTED_EXTENSIONS
from .pipeline.normalize import ISO_CURRENCIES, norm_invoice_number, parse_date
from .processor import index_invoice, refresh_document_status, revalidate
from .serializers import document_to_dict, invoice_to_dict
from .worker import queue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("docintel")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    queue.recover()
    log.info("LLM extraction: %s (text: %s %s; photos/scans: %s %s); OCR engine: %s; embeddings: %s",
             "enabled" if settings.llm_available else "disabled (rule-based)", settings.provider, settings.active_model,
             settings.vision_provider, settings.vision_model, ocr_mod.engine_name(), embeddings.backend_name())
    yield
    queue.shutdown()


app = FastAPI(title="Document Intelligence API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])


# ------------------------------------------------------------------ security

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    if settings.api_key and path.startswith("/api") and path != "/api/health" and request.method != "OPTIONS":
        supplied = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(supplied.encode(), settings.api_key.encode()):
            return JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _safe_name(name: str) -> str:
    name = Path(name or "upload").name  # strip any client-side path (path traversal)
    name = re.sub(r"[^\w.\- ()]", "_", name)[:200]
    return name or "upload"


# ------------------------------------------------------------------ schemas

class LineItemIn(BaseModel):
    description: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    discount_pct: float | None = None
    amount: float | None = None


class InvoicePatch(BaseModel):
    invoice_number: str | None = None
    invoice_date: str | None = None
    supplier_name: str | None = None
    customer_name: str | None = None
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None
    currency: str | None = None
    line_items: list[LineItemIn] | None = None
    corrected_by: str | None = Field(default=None, max_length=128)
    mark_reviewed: bool = False


class CrossVerifyIn(BaseModel):
    left_document_ids: list[str] = Field(min_length=1)
    right_document_ids: list[str] = Field(min_length=1)


# ------------------------------------------------------------------ endpoints

class LLMSettingsIn(BaseModel):
    local_only: bool


def _llm_settings() -> dict:
    import httpx

    try:
        r = httpx.get(f"{settings.ollama_url}/api/tags", timeout=1.5)
        names = [m.get("name") for m in r.json().get("models", [])] if r.status_code == 200 else []
        ollama = {"running": r.status_code == 200, "model_installed": settings.ollama_model in names}
    except (httpx.HTTPError, ValueError):
        ollama = {"running": False, "model_installed": False}
    return {"local_only": settings.local_only, "local_model": settings.ollama_model, "ollama": ollama,
            "text_provider": settings.provider, "text_model": settings.active_model,
            "vision_provider": settings.vision_provider, "vision_model": settings.vision_model}


@app.get("/api/settings/llm")
def get_llm_settings():
    """Which model reads typed documents and which reads photos, plus the local-only switch."""
    return _llm_settings()


@app.put("/api/settings/llm")
def put_llm_settings(body: LLMSettingsIn):
    """local_only=true: every document (typed and photos) goes to the local Ollama model; nothing leaves
    the machine. Applies to documents processed from now on (reprocess older ones to re-read them)."""
    settings.set_local_only(body.local_only)
    log.info("LLM switch: local_only=%s -> text %s, photos %s", body.local_only, settings.provider, settings.vision_provider)
    return _llm_settings()


@app.get("/api/health")
def health():
    return {"status": "ok", "llm": settings.llm_available, "llm_provider": settings.provider,
            "llm_model": settings.active_model, "llm_text": settings.text_llm_available,
            "vision_provider": settings.vision_provider, "vision_model": settings.vision_model,
            "local_only": settings.local_only,
            "ocr_engine": ocr_mod.engine_name(), "embedding_backend": embeddings.backend_name(),
            "queue_inflight": queue.inflight()}


@app.post("/api/documents", status_code=202)
async def upload_documents(files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    if not files:
        raise HTTPException(400, "No files uploaded")
    if len(files) > settings.max_files_per_request:
        raise HTTPException(413, f"Too many files (max {settings.max_files_per_request} per request)")
    accepted, rejected = [], []
    limit = settings.max_upload_mb * 1024 * 1024
    for f in files:
        name = _safe_name(f.filename or "upload")
        ext = Path(name).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            rejected.append({"filename": name, "error": f"Unsupported file type '{ext}'"})
            continue
        doc_id = uuid.uuid4().hex
        dest = settings.storage_dir / f"{doc_id}{ext}"  # never use the client filename on disk
        h, size = hashlib.sha256(), 0
        too_big = False
        with open(dest, "wb") as out:
            while chunk := await f.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    too_big = True
                    break
                h.update(chunk)
                out.write(chunk)
        if too_big:
            dest.unlink(missing_ok=True)
            rejected.append({"filename": name, "error": f"File exceeds {settings.max_upload_mb} MB"})
            continue
        doc = Document(id=doc_id, filename=name, file_type=ext.lstrip("."), mime_type=f.content_type, size_bytes=size,
                       sha256=h.hexdigest(), storage_path=str(dest), status="queued")
        db.add(doc)
        accepted.append(doc)
    db.commit()
    for d in accepted:
        queue.submit(d.id)
    return {"accepted": [{"id": d.id, "filename": d.filename, "status": "queued"} for d in accepted], "rejected": rejected}


@app.get("/api/documents")
def list_documents(status: str | None = None, q: str | None = None, limit: int = Query(100, le=500), offset: int = 0,
                   db: Session = Depends(get_db)):
    stmt = select(Document)
    if status:
        stmt = stmt.where(Document.status == status)
    if q:
        stmt = stmt.where(func.lower(Document.filename).like(f"%{q.lower()}%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    docs = db.execute(stmt.order_by(Document.created_at.desc()).limit(limit).offset(offset)).scalars().all()
    return {"total": total, "items": [document_to_dict(d) for d in docs]}


def _doc(db: Session, doc_id: str) -> Document:
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    return doc


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str, db: Session = Depends(get_db)):
    return document_to_dict(_doc(db, doc_id), full=True)


@app.get("/api/documents/{doc_id}/status")
def get_status(doc_id: str, db: Session = Depends(get_db)):
    d = _doc(db, doc_id)
    return {"id": d.id, "status": d.status, "progress": d.progress, "stage": d.stage, "error": d.error}


@app.get("/api/documents/{doc_id}/results")
def get_results(doc_id: str, db: Session = Depends(get_db)):
    d = _doc(db, doc_id)
    return {"document_id": d.id, "status": d.status, "invoices": [invoice_to_dict(i) for i in d.invoices]}


@app.get("/api/documents/{doc_id}/text")
def get_text(doc_id: str, db: Session = Depends(get_db)):
    d = _doc(db, doc_id)
    return {"pages": [{"page": p.page_number, "source": p.source, "ocr_confidence": p.ocr_confidence, "text": p.text} for p in d.pages]}


@app.get("/api/documents/{doc_id}/file")
def get_file(doc_id: str, db: Session = Depends(get_db)):
    d = _doc(db, doc_id)
    p = Path(d.storage_path).resolve()
    if not p.is_file() or settings.storage_dir.resolve() not in p.parents:
        raise HTTPException(404, "File not available")
    return FileResponse(p, filename=d.filename)


@app.post("/api/documents/{doc_id}/reprocess", status_code=202)
def reprocess(doc_id: str, db: Session = Depends(get_db)):
    d = _doc(db, doc_id)
    if d.status in ("queued", "processing"):
        raise HTTPException(409, "Document is already being processed")
    d.status, d.progress, d.stage = "queued", 0.0, "queued"
    db.commit()
    queue.submit(d.id)
    return {"id": d.id, "status": "queued"}


@app.post("/api/jobs/reprocess-failed", status_code=202)
def reprocess_failed(db: Session = Depends(get_db)):
    docs = db.execute(select(Document).where(Document.status == "failed")).scalars().all()
    for d in docs:
        d.status, d.progress, d.stage = "queued", 0.0, "queued"
    db.commit()
    for d in docs:
        queue.submit(d.id)
    return {"requeued": [d.id for d in docs]}


@app.delete("/api/documents/{doc_id}", status_code=204)
def delete_document(doc_id: str, db: Session = Depends(get_db)):
    d = _doc(db, doc_id)
    if d.status == "processing":
        raise HTTPException(409, "Document is being processed")
    Path(d.storage_path).unlink(missing_ok=True)
    db.delete(d)
    db.commit()


@app.get("/api/invoices")
def list_invoices(q: str | None = None, supplier: str | None = None, customer: str | None = None,
                  invoice_number: str | None = None, currency: str | None = None, date_from: str | None = None,
                  date_to: str | None = None, min_total: float | None = None, max_total: float | None = None,
                  needs_review: bool | None = None, document_id: str | None = None,
                  limit: int = Query(100, le=1000), offset: int = 0, db: Session = Depends(get_db)):
    stmt = search.keyword_query(db, q=q, supplier=supplier, customer=customer, invoice_number=invoice_number,
                                currency=currency, date_from=date_from, date_to=date_to, min_total=min_total,
                                max_total=max_total, needs_review=needs_review, document_id=document_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(stmt.order_by(Invoice.invoice_date.desc(), Invoice.created_at.desc()).limit(limit).offset(offset)).scalars().all()
    return {"total": total, "items": [invoice_to_dict(i) for i in rows]}


def _inv(db: Session, inv_id: str) -> Invoice:
    inv = db.get(Invoice, inv_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    return inv


@app.get("/api/invoices/{inv_id}")
def get_invoice(inv_id: str, db: Session = Depends(get_db)):
    return invoice_to_dict(_inv(db, inv_id), full=True)


@app.patch("/api/invoices/{inv_id}")
def correct_invoice(inv_id: str, patch: InvoicePatch, db: Session = Depends(get_db)):
    inv = _inv(db, inv_id)
    data: dict[str, Any] = patch.model_dump(exclude_unset=True)
    who = data.pop("corrected_by", None) or "user"
    mark = data.pop("mark_reviewed", False)
    if "invoice_date" in data and data["invoice_date"]:
        iso, _ = parse_date(data["invoice_date"])
        if not iso:
            raise HTTPException(422, "invoice_date must be a valid date (YYYY-MM-DD)")
        data["invoice_date"] = iso
    if "currency" in data and data["currency"]:
        data["currency"] = data["currency"].strip().upper()
        if data["currency"] not in ISO_CURRENCIES:
            raise HTTPException(422, f"Unknown currency code '{data['currency']}'")
    uncertain = list(inv.uncertain_fields or [])
    conf = dict(inv.confidence or {})
    for field, new in data.items():
        if field == "line_items":
            for li in new or []:  # API clients may send qty + price only: amount = qty × price − discount
                q, u, d = li.get("quantity"), li.get("unit_price"), li.get("discount_pct") or 0
                if li.get("amount") is None and q is not None and u is not None:
                    li["amount"] = round(q * u * (1 - d / 100), 2)
            old = [(li.description, li.quantity, li.unit_price, li.amount) for li in inv.line_items]
            inv.line_items = [LineItem(position=i, **li) for i, li in enumerate(new or [])]
            db.add(Correction(invoice=inv, field="line_items", old_value=str(old), new_value=str(new), corrected_by=who))
            continue
        old = getattr(inv, field)
        if old == new:
            continue
        setattr(inv, field, new)
        if field == "invoice_number":
            inv.invoice_number_norm = norm_invoice_number(new)
        db.add(Correction(invoice=inv, field=field, old_value=None if old is None else str(old),
                          new_value=None if new is None else str(new), corrected_by=who))
        if field in uncertain:
            uncertain.remove(field)
        conf[field] = 1.0  # human-verified
    if mark:
        uncertain = []
        conf = {k: (1.0 if getattr(inv, k, None) not in (None, "") else v) for k, v in conf.items()}
    inv.uncertain_fields, inv.confidence = uncertain, conf
    db.flush()
    revalidate(db, inv)
    if mark:
        inv.reviewed = True
        inv.needs_review = any(i.severity == "error" for i in inv.issues)
    index_invoice(db, inv)
    refresh_document_status(inv.document)
    db.commit()
    db.refresh(inv)
    return invoice_to_dict(inv, full=True)


@app.get("/api/search")
def search_endpoint(q: str = Query(..., min_length=1, max_length=500), mode: str = Query("hybrid", pattern="^(keyword|semantic|hybrid)$"),
                    limit: int = Query(20, le=100), db: Session = Depends(get_db)):
    if mode == "keyword":
        rows = db.execute(search.keyword_query(db, q=q).limit(limit)).scalars().all()
        return {"mode": mode, "items": [{**invoice_to_dict(r), "score": None} for r in rows]}
    hits = search.semantic_search(db, q, limit) if mode == "semantic" else search.hybrid_search(db, q, limit)
    by_id = {i.id: i for i in db.execute(select(Invoice).where(Invoice.id.in_([h[0] for h in hits]))).scalars()}
    return {"mode": mode, "embedding_backend": embeddings.backend_name(),
            "items": [{**invoice_to_dict(by_id[i]), "score": round(s, 4)} for i, s in hits if i in by_id]}


@app.post("/api/crossverify")
def crossverify_endpoint(body: CrossVerifyIn, db: Session = Depends(get_db)):
    def load(ids):
        invs = db.execute(select(Invoice).where(Invoice.document_id.in_(ids))).scalars().all()
        missing = set(ids) - {d for (d,) in db.execute(select(Document.id).where(Document.id.in_(ids)))}
        if missing:
            raise HTTPException(404, f"Unknown document id(s): {sorted(missing)}")
        return [invoice_to_dict(i) for i in invs]

    return cross_verify(load(body.left_document_ids), load(body.right_document_ids))


@app.get("/api/stats")
def stats(db: Session = Depends(get_db)):
    by_status = dict(db.execute(select(Document.status, func.count()).group_by(Document.status)).all())
    n_inv = db.scalar(select(func.count(Invoice.id))) or 0
    n_review = db.scalar(select(func.count(Invoice.id)).where(Invoice.needs_review.is_(True))) or 0
    avg_ms = db.scalar(select(func.avg(Document.processing_ms)).where(Document.processing_ms.is_not(None)))
    return {"documents": by_status, "invoices": n_inv, "invoices_needing_review": n_review,
            "avg_processing_ms": round(avg_ms or 0, 1)}


# ------------------------------------------------------------------ serve the built frontend (optional)

_dist = BASE_DIR.parent / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404)
        # no-cache: the browser must re-check index.html, so a rebuilt UI (new hashed asset names) shows up
        # immediately instead of an old cached version. The hashed /assets files can still be cached.
        return FileResponse(_dist / "index.html", headers={"Cache-Control": "no-cache"})
