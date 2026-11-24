"""Task 4/5: REST API, database, search, corrections, reprocessing, security, failure scenarios."""
import copy
import io

import pytest

from app.config import settings
from eval import synth


def _pdf_bytes(invs, rng, layouts=("A",)):
    buf = io.BytesIO()
    synth.render_pdf(invs, buf, rng, list(layouts))
    return buf.getvalue()


def _upload(client, *files):
    r = client.post("/api/documents", files=[("files", f) for f in files])
    assert r.status_code == 202, r.text
    return r.json()


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_upload_multiple_and_get_results(client, rng, make_invoice, tmp_path):
    inv1, inv2 = make_invoice("API"), make_invoice("API")
    csv_path = tmp_path / "x.csv"
    synth.render_csv([inv2], csv_path)
    res = _upload(client, ("a.pdf", _pdf_bytes([inv1], rng), "application/pdf"),
                  ("b.csv", csv_path.read_bytes(), "text/csv"))
    assert len(res["accepted"]) == 2 and res["rejected"] == []
    pdf_id, csv_id = (a["id"] for a in res["accepted"])

    st = client.get(f"/api/documents/{pdf_id}/status").json()
    assert st["status"] == "completed" and st["progress"] == 1.0

    doc = client.get(f"/api/documents/{pdf_id}").json()
    assert doc["invoice_count"] == 1 and doc["page_count"] == 1
    assert [e["stage"] for e in doc["events"]][:2] == ["processing", "load"]  # processing history
    inv = doc["invoices"][0]
    assert inv["invoice_number"] == inv1["invoice_number"] and inv["total"] == inv1["total"]
    assert len(inv["line_items"]) == len(inv1["line_items"])

    results = client.get(f"/api/documents/{csv_id}/results").json()
    assert results["invoices"][0]["invoice_number"] == inv2["invoice_number"]
    text = client.get(f"/api/documents/{pdf_id}/text").json()
    assert inv1["invoice_number"] in text["pages"][0]["text"]
    assert client.get(f"/api/documents/{pdf_id}/file").status_code == 200

    listing = client.get("/api/documents").json()
    assert listing["total"] >= 2


def test_unsupported_and_corrupted_files(client, rng):
    res = _upload(client, ("evil.exe", b"MZ....", "application/octet-stream"),
                  ("broken.pdf", b"%PDF-1.7 garbage that ends abruptly", "application/pdf"))
    assert res["rejected"][0]["filename"] == "evil.exe"
    (acc,) = res["accepted"]
    d = client.get(f"/api/documents/{acc['id']}").json()
    assert d["status"] == "failed" and d["error"]
    assert "\\" not in d["error"] and "/uploads" not in d["error"]  # no server paths leaked


def test_path_traversal_filename_is_sanitised(client, rng, make_invoice):
    res = _upload(client, ("../../etc/passwd.pdf", _pdf_bytes([make_invoice()], rng), "application/pdf"))
    assert res["accepted"][0]["filename"] == "passwd.pdf"


def test_oversized_upload_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    res = _upload(client, ("big.csv", b"a,b\n1,2\n", "text/csv"))
    assert res["accepted"] == [] and "exceeds" in res["rejected"][0]["error"]


def test_too_many_files(client, monkeypatch):
    monkeypatch.setattr(settings, "max_files_per_request", 1)
    r = client.post("/api/documents", files=[("files", ("a.csv", b"a", "text/csv")), ("files", ("b.csv", b"b", "text/csv"))])
    assert r.status_code == 413


def test_manual_correction_revalidates_and_audits(client, rng, make_invoice):
    inv = make_invoice("COR")
    inv["total"] = round(inv["total"] + 5, 2)  # printed total is wrong
    res = _upload(client, ("wrong.pdf", _pdf_bytes([inv], rng), "application/pdf"))
    doc = client.get(f"/api/documents/{res['accepted'][0]['id']}").json()
    assert doc["status"] == "needs_review"
    inv_row = doc["invoices"][0]
    assert any(i["code"] == "total_mismatch" for i in inv_row["issues"])

    fixed = round(inv["subtotal"] + inv["tax"], 2)
    r = client.patch(f"/api/invoices/{inv_row['id']}", json={"total": fixed, "corrected_by": "tester", "mark_reviewed": True})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == fixed and body["needs_review"] is False and body["reviewed"] is True
    assert body["corrections"][0]["field"] == "total" and body["corrections"][0]["corrected_by"] == "tester"
    assert client.get(f"/api/documents/{doc['id']}").json()["status"] == "completed"

    bad = client.patch(f"/api/invoices/{inv_row['id']}", json={"invoice_date": "not a date"})
    assert bad.status_code == 422
    bad = client.patch(f"/api/invoices/{inv_row['id']}", json={"currency": "ZZZ"})
    assert bad.status_code == 422


def test_duplicate_and_cross_reference_detection(client, rng, make_invoice, tmp_path):
    inv = make_invoice("DUP")
    _upload(client, ("orig.pdf", _pdf_bytes([inv], rng), "application/pdf"))
    res = _upload(client, ("copy.pdf", _pdf_bytes([copy.deepcopy(inv)], rng, ("B",)), "application/pdf"))
    d = client.get(f"/api/documents/{res['accepted'][0]['id']}").json()
    assert any(i["code"] == "duplicate_invoice" for i in d["invoices"][0]["issues"])
    assert d["status"] == "needs_review"

    # Same invoice in a spreadsheet with a different total -> cross-check mismatch (PDF <-> Excel)
    sheet = copy.deepcopy(inv); sheet["total"] = round(sheet["total"] + 100, 2)
    p = tmp_path / "ledger.csv"; synth.render_csv([sheet], p)
    res = _upload(client, ("ledger.csv", p.read_bytes(), "text/csv"))
    d = client.get(f"/api/documents/{res['accepted'][0]['id']}").json()
    codes = {i["code"] for i in d["invoices"][0]["issues"]}
    assert "cross_check_mismatch" in codes and "duplicate_invoice" not in codes


def test_crossverify_endpoint(client, rng, make_invoice, tmp_path):
    invs = [make_invoice("XVE") for _ in range(2)]
    left = _upload(client, ("l.pdf", _pdf_bytes(invs, rng, ("A", "C")), "application/pdf"))["accepted"][0]["id"]
    sheet = copy.deepcopy(invs); sheet[0]["tax"] = round(sheet[0]["tax"] + 1, 2)
    p = tmp_path / "r.csv"; synth.render_csv(sheet, p)
    right = _upload(client, ("r.csv", p.read_bytes(), "text/csv"))["accepted"][0]["id"]
    rep = client.post("/api/crossverify", json={"left_document_ids": [left], "right_document_ids": [right]}).json()
    assert rep["summary"]["matched"] == 2 and rep["summary"]["with_differences"] == 1
    assert client.post("/api/crossverify", json={"left_document_ids": ["nope"], "right_document_ids": [right]}).status_code == 404


def test_search_keyword_filters_semantic_hybrid(client, rng, make_invoice):
    inv = make_invoice("SRCH")
    inv["supplier_name"] = "Zephyr Quantum Devices"
    _upload(client, ("s.pdf", _pdf_bytes([inv], rng, ("B",)), "application/pdf"))

    r = client.get("/api/invoices", params={"supplier": "zephyr"}).json()
    assert r["total"] == 1 and r["items"][0]["supplier_name"] == "Zephyr Quantum Devices"
    r = client.get("/api/invoices", params={"min_total": inv["total"] - 0.01, "max_total": inv["total"] + 0.01,
                                            "invoice_number": "SRCH"}).json()
    assert r["total"] == 1
    r = client.get("/api/invoices", params={"currency": inv["currency"], "date_from": inv["invoice_date"],
                                            "date_to": inv["invoice_date"], "supplier": "zephyr"}).json()
    assert r["total"] == 1

    kw = client.get("/api/search", params={"q": "Zephyr", "mode": "keyword"}).json()
    assert kw["items"][0]["supplier_name"] == "Zephyr Quantum Devices"
    # semantic: typo + partial words still find it
    sem = client.get("/api/search", params={"q": "zephir quantum vendor", "mode": "semantic"}).json()
    assert sem["items"] and sem["items"][0]["supplier_name"] == "Zephyr Quantum Devices"
    hyb = client.get("/api/search", params={"q": "Zephyr Quantum", "mode": "hybrid"}).json()
    assert hyb["items"][0]["supplier_name"] == "Zephyr Quantum Devices"
    assert client.get("/api/search", params={"q": "x", "mode": "bogus"}).status_code == 422


def test_reprocess_single_and_failed_jobs(client, rng, make_invoice, monkeypatch):
    from app import processor

    real = processor.extractor.extract_document

    def explode(*a, **k):
        raise RuntimeError("transient OCR worker crash")

    monkeypatch.setattr(processor.extractor, "extract_document", explode)
    res = _upload(client, ("flaky.pdf", _pdf_bytes([make_invoice("RP")], rng), "application/pdf"))
    doc_id = res["accepted"][0]["id"]
    d = client.get(f"/api/documents/{doc_id}").json()
    assert d["status"] == "failed" and "transient" in d["error"]

    monkeypatch.setattr(processor.extractor, "extract_document", real)
    r = client.post("/api/jobs/reprocess-failed").json()
    assert doc_id in r["requeued"]
    d = client.get(f"/api/documents/{doc_id}").json()
    assert d["status"] == "completed" and d["attempts"] == 2 and d["invoice_count"] == 1

    r = client.post(f"/api/documents/{doc_id}/reprocess")
    assert r.status_code == 202
    assert client.get(f"/api/documents/{doc_id}").json()["attempts"] == 3


def test_not_found_and_delete(client, rng, make_invoice):
    assert client.get("/api/documents/doesnotexist").status_code == 404
    assert client.get("/api/invoices/doesnotexist").status_code == 404
    res = _upload(client, ("del.pdf", _pdf_bytes([make_invoice("DEL")], rng), "application/pdf"))
    doc_id = res["accepted"][0]["id"]
    assert client.delete(f"/api/documents/{doc_id}").status_code == 204
    assert client.get(f"/api/documents/{doc_id}").status_code == 404


def test_stats(client):
    s = client.get("/api/stats").json()
    assert s["invoices"] >= 1 and "documents" in s


def test_api_key_enforced(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "s3cret")
    assert client.get("/api/documents").status_code == 401
    assert client.get("/api/documents", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/documents", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get("/api/health").status_code == 200  # health stays open for load balancers
    r = client.get("/api/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
