"""Task 1: document processing — PDFs, OCR, images, corrupted files."""
import pytest
from PIL import Image

from app.pipeline import loaders
from app.pipeline.loaders import DocumentLoadError, load_document, sniff_type
from eval import synth


def test_digital_pdf_uses_text_layer(tmp_path, rng, make_invoice):
    inv = make_invoice()
    p = tmp_path / "a.pdf"
    synth.render_pdf([inv], p, rng, ["A"])
    doc = load_document(p)
    assert doc.file_type == "pdf" and len(doc.pages) == 1
    assert doc.pages[0].source == "text" and not doc.ocr_used
    assert inv["invoice_number"] in doc.pages[0].text


def test_multi_page_pdf(tmp_path, rng, make_invoice):
    invs = [make_invoice() for _ in range(4)]
    p = tmp_path / "multi.pdf"
    synth.render_pdf(invs, p, rng, ["A", "B", "C"])
    doc = load_document(p)
    assert len(doc.pages) == 4
    for i, inv in enumerate(invs):
        assert inv["invoice_number"] in doc.pages[i].text


def test_scanned_pdf_falls_back_to_ocr(tmp_path, rng, make_invoice):
    inv = make_invoice()
    p = tmp_path / "scan.pdf"
    synth.render_scanned_pdf([inv], p, rng, "clean")
    doc = load_document(p)
    assert doc.pages[0].source == "ocr" and doc.ocr_used
    assert doc.ocr_confidence and doc.ocr_confidence > 0.8
    assert inv["invoice_number"].replace(" ", "") in doc.pages[0].text.replace(" ", "")


@pytest.mark.parametrize("ext,quality", [(".png", "clean"), (".jpg", "lowres"), (".png", "noisy")])
def test_image_ocr(tmp_path, rng, make_invoice, ext, quality):
    inv = make_invoice()
    p = tmp_path / f"img{ext}"
    synth.render_image(inv, p, rng, quality)
    doc = load_document(p)
    assert doc.file_type == "image" and doc.ocr_used
    assert inv["invoice_number"] in doc.pages[0].text.replace(" ", "")


def test_multi_frame_tiff(tmp_path, rng, make_invoice):
    frames = []
    for _ in range(2):
        q = tmp_path / "f.png"
        synth.render_image(make_invoice(), q, rng, "clean")
        frames.append(Image.open(q).convert("L"))
    p = tmp_path / "multi.tiff"
    frames[0].save(p, save_all=True, append_images=frames[1:])
    doc = load_document(p)
    assert len(doc.pages) == 2


def test_ocr_preprocessing_variants():
    from app.pipeline.ocr import preprocess

    img = Image.new("RGB", (300, 200), "white")
    for v in ("standard", "denoise", "binarize"):
        out = preprocess(img, v)
        assert max(out.size) >= 1400  # small images are upscaled for OCR


@pytest.mark.parametrize("kind,name", [
    ("truncated_pdf", "t.pdf"), ("random_png", "n.png"), ("empty_csv", "e.csv"),
    ("broken_xlsx", "b.xlsx"), ("fake_pdf", "f.pdf"),
])
def test_corrupted_files_raise_clean_errors(tmp_path, rng, kind, name):
    p = tmp_path / name
    synth.write_corrupted(p, kind, rng)
    with pytest.raises(DocumentLoadError):
        load_document(p)


def test_password_protected_pdf(tmp_path, rng, make_invoice):
    import pymupdf

    src = tmp_path / "plain.pdf"
    synth.render_pdf([make_invoice()], src, rng)
    d = pymupdf.open(src)
    enc = tmp_path / "locked.pdf"
    d.save(enc, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="secret")
    with pytest.raises(DocumentLoadError, match="password"):
        load_document(enc)


def test_sniff_by_content_not_extension(tmp_path, rng, make_invoice):
    p = tmp_path / "really_a_pdf.png"
    synth.render_pdf([make_invoice()], p, rng)
    assert sniff_type(p) == "pdf"
    doc = load_document(p)
    assert any("does not match" in w for w in doc.warnings)


def test_one_bad_page_does_not_fail_document(tmp_path, rng, make_invoice, monkeypatch):
    p = tmp_path / "m.pdf"
    synth.render_pdf([make_invoice(), make_invoice()], p, rng)
    real = loaders._pdf_page_text
    calls = {"n": 0}

    def flaky(page):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("damaged page stream")
        return real(page)

    monkeypatch.setattr(loaders, "_pdf_page_text", flaky)
    doc = load_document(p)
    assert len(doc.pages) == 2 and doc.pages[0].text == "" and doc.pages[1].text
    assert any("Page 1" in w for w in doc.warnings)
