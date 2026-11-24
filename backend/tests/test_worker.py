"""Background processing: asynchronous queue, de-duplication of jobs, restart recovery."""
import hashlib
import time
import uuid

from app.config import settings
from app.db import session_scope
from app.models import Document
from app.worker import JobQueue
from eval import synth


def _make_doc(tmp_path, rng, make_invoice, status="queued"):
    p = tmp_path / f"{uuid.uuid4().hex}.pdf"
    synth.render_pdf([make_invoice("WRK")], p, rng)
    data = p.read_bytes()
    with session_scope() as db:
        d = Document(filename=p.name, file_type="pdf", size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                     storage_path=str(p), status=status)
        db.add(d)
        db.flush()
        return d.id


def _wait(doc_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        with session_scope() as db:
            st = db.get(Document, doc_id).status
        if st not in ("queued", "processing"):
            return st
        time.sleep(0.2)
    raise AssertionError("timeout")


def test_async_thread_queue_processes_in_background(tmp_path, rng, make_invoice, monkeypatch):
    monkeypatch.setattr(settings, "process_inline", False)
    q = JobQueue(2, "thread")
    doc_id = _make_doc(tmp_path, rng, make_invoice)
    assert q.submit(doc_id) is True
    assert q.submit(doc_id) is False or q.inflight() == 0  # same doc is never queued twice concurrently
    assert _wait(doc_id) == "completed"
    q.shutdown()


def test_recover_requeues_unfinished_jobs(tmp_path, rng, make_invoice, monkeypatch):
    monkeypatch.setattr(settings, "process_inline", False)
    stuck = _make_doc(tmp_path, rng, make_invoice, status="processing")  # e.g. server crashed mid-job
    q = JobQueue(1, "thread")
    assert q.recover() >= 1
    assert _wait(stuck) in ("completed", "needs_review")  # processed (may be a duplicate of the previous test's invoice)
    q.shutdown()
