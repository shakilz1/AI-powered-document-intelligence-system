"""Text embeddings for semantic search.

Backends:
  * fastembed  — real sentence embeddings (BAAI/bge-small-en-v1.5, ONNX, ~130 MB download on first
                 use). Enabled automatically when `pip install fastembed` is present.
  * hashing    — dependency-free fallback: signed feature hashing of words, word bigrams and
                 character 4-grams, L2-normalised (1024 dims). Captures fuzzy/lexical similarity
                 (typos, partial names, word order) but not synonyms. Always available offline.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading

import numpy as np

from .config import settings

log = logging.getLogger(__name__)
_DIM = 1024
_lock = threading.Lock()
_fastembed_model = None
SYNONYMS = {  # light query expansion for the hashing backend
    "vendor": "supplier", "seller": "supplier", "client": "customer", "buyer": "customer",
    "vat": "tax", "gst": "tax", "amount": "total", "bill": "invoice",
}


def _h(token: str) -> tuple[int, float]:
    d = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    n = int.from_bytes(d, "little")
    return n % _DIM, (1.0 if (n >> 63) & 1 else -1.0)


def _hash_embed(text: str) -> np.ndarray:
    v = np.zeros(_DIM, dtype=np.float32)
    words = [SYNONYMS.get(w, w) for w in re.findall(r"[a-z0-9]+", text.lower())]
    for w in words:
        i, s = _h("w:" + w)
        v[i] += s * 2.0
    for a, b in zip(words, words[1:]):
        i, s = _h(f"b:{a}_{b}")
        v[i] += s
    joined = " " + " ".join(words) + " "
    for k in range(len(joined) - 3):
        i, s = _h("c:" + joined[k:k + 4])
        v[i] += s * 0.5
    n = np.linalg.norm(v)
    return v / n if n else v


def backend_name() -> str:
    pref = settings.embedding_backend
    if pref == "hashing":
        return "hashing"
    try:
        import fastembed  # noqa: F401

        return "fastembed"
    except Exception:
        if pref == "fastembed":
            log.warning("EMBEDDING_BACKEND=fastembed but fastembed is not installed; using hashing")
        return "hashing"


def embed(texts: list[str]) -> tuple[str, np.ndarray]:
    name = backend_name()
    if name == "fastembed":
        global _fastembed_model
        try:
            with _lock:
                if _fastembed_model is None:
                    from fastembed import TextEmbedding

                    _fastembed_model = TextEmbedding("BAAI/bge-small-en-v1.5")
            vecs = np.array(list(_fastembed_model.embed(texts)), dtype=np.float32)
            vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
            return name, vecs
        except Exception as exc:  # model download failure etc.
            log.warning("fastembed failed (%s); falling back to hashing embeddings", exc)
    return "hashing", np.stack([_hash_embed(t) for t in texts]) if texts else np.zeros((0, _DIM), np.float32)


def invoice_content(inv) -> str:
    """Text that represents an invoice for semantic search."""
    parts = [
        f"invoice {inv.invoice_number or ''}",
        f"supplier {inv.supplier_name or ''}",
        f"customer {inv.customer_name or ''}",
        f"date {inv.invoice_date or ''}",
        f"total {inv.total if inv.total is not None else ''} {inv.currency or ''}",
    ]
    parts += [li.description or "" for li in inv.line_items][:50]
    # Structured fields only: raw page text (addresses, boilerplate) dilutes the vector and is
    # already covered by keyword search over source_text.
    return " | ".join(parts)
