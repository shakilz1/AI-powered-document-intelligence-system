"""Task 5 — keyword filtering + semantic (vector) search + hybrid ranking."""
from __future__ import annotations

import threading

import numpy as np
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import embeddings
from .models import Document, Embedding, Invoice

_cache_lock = threading.Lock()
_cache: dict = {"key": None, "ids": [], "matrix": None, "backend": None}


def keyword_query(db: Session, *, q: str | None = None, supplier: str | None = None, customer: str | None = None,
                  invoice_number: str | None = None, currency: str | None = None, date_from: str | None = None,
                  date_to: str | None = None, min_total: float | None = None, max_total: float | None = None,
                  needs_review: bool | None = None, document_id: str | None = None):
    stmt = select(Invoice).join(Document)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(
            func.lower(Invoice.invoice_number).like(like),
            func.lower(Invoice.supplier_name).like(like),
            func.lower(Invoice.customer_name).like(like),
            func.lower(Invoice.source_text).like(like),
            func.lower(Document.filename).like(like),
        ))
    if supplier:
        stmt = stmt.where(func.lower(Invoice.supplier_name).like(f"%{supplier.lower()}%"))
    if customer:
        stmt = stmt.where(func.lower(Invoice.customer_name).like(f"%{customer.lower()}%"))
    if invoice_number:
        stmt = stmt.where(func.lower(Invoice.invoice_number).like(f"%{invoice_number.lower()}%"))
    if currency:
        stmt = stmt.where(Invoice.currency == currency.upper())
    if date_from:
        stmt = stmt.where(Invoice.invoice_date >= date_from)
    if date_to:
        stmt = stmt.where(Invoice.invoice_date <= date_to)
    if min_total is not None:
        stmt = stmt.where(Invoice.total >= min_total)
    if max_total is not None:
        stmt = stmt.where(Invoice.total <= max_total)
    if needs_review is not None:
        stmt = stmt.where(Invoice.needs_review == needs_review)
    if document_id:
        stmt = stmt.where(Invoice.document_id == document_id)
    return stmt


def _matrix(db: Session):
    """Load all vectors into one matrix (cached until the embedding table changes)."""
    key = db.execute(select(func.count(Embedding.invoice_id), func.max(Invoice.updated_at)).join(Invoice)).one()
    with _cache_lock:
        if _cache["key"] == key and _cache["matrix"] is not None:
            return _cache["ids"], _cache["matrix"], _cache["backend"]
    rows = db.execute(select(Embedding.invoice_id, Embedding.vector, Embedding.dim, Embedding.backend)).all()
    backend = embeddings.backend_name()
    rows = [r for r in rows if r.backend == backend] or rows
    ids = [r.invoice_id for r in rows]
    mat = np.stack([np.frombuffer(r.vector, dtype=np.float32) for r in rows]) if rows else np.zeros((0, 1), np.float32)
    with _cache_lock:
        _cache.update(key=key, ids=ids, matrix=mat, backend=rows[0].backend if rows else backend)
    return ids, mat, _cache["backend"]


def semantic_search(db: Session, query: str, limit: int = 20) -> list[tuple[str, float]]:
    ids, mat, backend = _matrix(db)
    if not ids:
        return []
    _, qv = embeddings.embed([query])
    if qv.shape[1] != mat.shape[1]:
        return []
    scores = mat @ qv[0]
    order = np.argsort(-scores)[:limit]
    return [(ids[i], float(scores[i])) for i in order if scores[i] > 0.05]


def hybrid_search(db: Session, query: str, limit: int = 20) -> list[tuple[str, float]]:
    """Reciprocal-rank fusion of keyword hits and semantic hits."""
    sem = semantic_search(db, query, limit * 2)
    kw_ids = [inv.id for inv in db.execute(keyword_query(db, q=query).limit(limit * 2)).scalars()]
    fused: dict[str, float] = {}
    for rank, (i, _) in enumerate(sem):
        fused[i] = fused.get(i, 0) + 1 / (60 + rank)
    for rank, i in enumerate(kw_ids):
        fused[i] = fused.get(i, 0) + 1 / (60 + rank)
    return sorted(fused.items(), key=lambda kv: -kv[1])[:limit]
