"""Background job queue for OCR/AI processing.

Default: an in-process thread pool whose state lives in the database (document.status), so
jobs survive restarts — on startup, documents left in `queued`/`processing` are re-queued.
For horizontal scale, the same `process_document(doc_id)` function can be run by a
Celery/RQ/BullMQ worker fleet reading from Redis; the API only needs `queue.submit(doc_id)`.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from sqlalchemy import select

from .config import settings
from .db import session_scope
from .models import Document
from .processor import process_document

log = logging.getLogger(__name__)


class JobQueue:
    """OCR is CPU-bound, so by default jobs run in separate *processes* (no GIL contention with
    the API threads). WORKER_MODE=thread uses threads instead (lighter, fine for LLM-bound work)."""

    def __init__(self, workers: int, mode: str = "process"):
        self._workers, self._mode = workers, mode
        self._pool = None
        self._inflight: set[str] = set()
        self._lock = threading.Lock()

    def _get_pool(self):
        if self._pool is None:
            if self._mode == "process":
                import multiprocessing as mp

                self._pool = ProcessPoolExecutor(max_workers=self._workers, mp_context=mp.get_context("spawn"))
            else:
                self._pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="docworker")
        return self._pool

    def submit(self, doc_id: str) -> bool:
        with self._lock:
            if doc_id in self._inflight:
                return False
            self._inflight.add(doc_id)
        if settings.process_inline:  # tests / evaluation: run synchronously
            try:
                process_document(doc_id)
            finally:
                self._done(doc_id)
            return True
        try:
            fut = self._get_pool().submit(process_document, doc_id)
        except Exception:  # broken pool (e.g. a worker was killed) -> recreate once
            log.exception("worker pool broken; recreating")
            self._pool = None
            fut = self._get_pool().submit(process_document, doc_id)
        fut.add_done_callback(lambda f, d=doc_id: self._finished(f, d))
        return True

    def _finished(self, fut, doc_id: str) -> None:
        exc = fut.exception()
        if exc is not None:  # process_document records its own failures; this catches worker crashes
            log.error("worker crashed on %s: %s", doc_id, exc)
            from .processor import _fail
            import time

            _fail(doc_id, f"Worker crashed: {exc.__class__.__name__}", time.perf_counter())
            if isinstance(exc, BrokenProcessPool):
                self._pool = None
        self._done(doc_id)

    def _done(self, doc_id: str) -> None:
        with self._lock:
            self._inflight.discard(doc_id)

    def inflight(self) -> int:
        with self._lock:
            return len(self._inflight)

    def recover(self) -> int:
        with session_scope() as db:
            ids = db.execute(select(Document.id).where(Document.status.in_(["queued", "processing"]))).scalars().all()
            for d in db.execute(select(Document).where(Document.id.in_(ids))).scalars():
                d.status = "queued"
        for i in ids:
            self.submit(i)
        if ids:
            log.info("re-queued %d unfinished document(s)", len(ids))
        return len(ids)

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)


queue = JobQueue(settings.worker_threads, settings.worker_mode)
